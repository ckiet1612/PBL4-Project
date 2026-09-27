"""Admit checkpoint-for-pause jobs to the B13 queue and support B15 control.

The B13 queue predicate was ``state='QUEUED' AND desired_state='RUNNING' AND
recovery_intent IS NULL``. A job recovered while paused is queued with desired
PAUSED and ``CHECKPOINT_FOR_PAUSE`` (state-machines.md:15, 31, 35), so the
predicate becomes ``state='QUEUED'`` and a CHECK keeps every queued row in one
of the two dispatchable shapes. Indexes and trigger functions are recreated;
downgrade restores the original definitions and refuses while a
checkpoint-for-pause job is queued or an audit reason is longer than 128.

It also adds ``worker_incarnations.ready_checked_at`` (the latest heartbeat that
passed the READY checks, used to enable a DISABLED worker; B15-R07), backfills
``terminal_at`` of terminal jobs and recounts the retention of their swept
idempotency records from it (B15-R25). Downgrade drops the column and keeps the
backfill.

Recreating the queue indexes holds ACCESS EXCLUSIVE on ``jobs`` for the whole
upgrade (transactional DDL cannot build them concurrently), so it runs with the
API, coordinator and worker stopped.
"""

from alembic import op
from sqlalchemy import Column, DateTime, String

revision = "20260926_0019"
down_revision = "20260926_0018"
branch_labels = None
depends_on = None


_INDEXES = (
    ("ix_jobs_b13_head", ("tenant_id", "base_priority", "ready_sequence", "job_id")),
    (
        "ix_jobs_b13_submitter",
        ("tenant_id", "submitter_user_id", "base_priority", "ready_sequence", "job_id"),
    ),
    (
        "ix_jobs_b13_priority_age",
        ("tenant_id", "base_priority", "eligible_since", "ready_sequence", "job_id"),
    ),
    (
        "ix_jobs_b13_submitter_age",
        (
            "tenant_id",
            "submitter_user_id",
            "base_priority",
            "eligible_since",
            "ready_sequence",
            "job_id",
        ),
    ),
)

# Frozen copy of schema_v16.RECOVERY_EVENT_TYPES for the partial recovery-event index.
RECOVERY_EVENT_TYPES = (
    "ATTEMPT_FAILED",
    "ATTEMPT_FENCED",
    "ATTEMPT_LOST",
    "LEASE_REVOKED",
    "ALLOCATION_RELEASED",
    "RETRY_READY",
    "RETRY_BLOCKED",
    "PAUSE_ABORTED",
    "CHECKPOINT_CORRUPT",
    "CHECKPOINT_INCOMPATIBLE",
    "CHECKPOINT_REJECTED",
    "CHECKPOINT_RESTORE_SELECTED",
    "CHECKPOINT_FALLBACK_TO_INPUT",
    "CHECKPOINT_RESTORE_UNAVAILABLE",
)
# Frozen copy of schema_v16.SWEPT_OPERATIONS for the partial retention-sweep index.
SWEPT_OPERATIONS = ("submitJob", "cancelJob", "pauseJob", "resumeJob", "retryFailedJob")


def _queued(widened: bool, alias: str = "") -> str:
    column = f"{alias}." if alias else ""
    if widened:
        return f"{column}state = 'QUEUED'"
    return (
        f"{column}state = 'QUEUED' AND {column}desired_state = 'RUNNING' "
        f"AND {column}recovery_intent IS NULL"
    )


def _functions(widened: bool) -> str:
    q, new_q, old_q, j_q = (_queued(widened, alias) for alias in ("", "NEW", "OLD", "j"))
    return f"""
        CREATE OR REPLACE FUNCTION nexa_b13_refresh_queue_head(p_tenant uuid, p_priority integer)
        RETURNS void LANGUAGE plpgsql AS $$
        DECLARE head jobs%ROWTYPE;
        DECLARE current_head queue_heads%ROWTYPE;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_tenant::text || ':' || p_priority::text, 130013)
            );
            SELECT * INTO head FROM jobs
            WHERE tenant_id = p_tenant AND base_priority = p_priority
              AND {q}
            ORDER BY ready_sequence, job_id LIMIT 1;
            IF NOT FOUND THEN
                DELETE FROM queue_heads
                WHERE tenant_id = p_tenant AND priority = p_priority;
            ELSE
                SELECT * INTO current_head FROM queue_heads
                WHERE tenant_id = p_tenant AND priority = p_priority;
                IF FOUND AND current_head.candidate_job_id = head.job_id
                   AND current_head.ready_sequence = head.ready_sequence
                   AND current_head.eligible_since IS NOT DISTINCT FROM head.eligible_since
                   AND current_head.retry_ready_at IS NOT DISTINCT FROM head.retry_ready_at
                THEN
                    RETURN;
                END IF;
                INSERT INTO queue_heads
                    (tenant_id, priority, candidate_job_id, ready_sequence,
                     eligible_since, retry_ready_at, rebuilt_at)
                VALUES
                    (p_tenant, p_priority, head.job_id, head.ready_sequence,
                     head.eligible_since, head.retry_ready_at, clock_timestamp())
                ON CONFLICT (tenant_id, priority) DO UPDATE SET
                    candidate_job_id = EXCLUDED.candidate_job_id,
                    ready_sequence = EXCLUDED.ready_sequence,
                    eligible_since = EXCLUDED.eligible_since,
                    retry_ready_at = EXCLUDED.retry_ready_at,
                    rebuilt_at = EXCLUDED.rebuilt_at;
            END IF;
        END $$;

        CREATE OR REPLACE FUNCTION nexa_b13_job_head_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE p integer;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF {new_q} THEN
                    PERFORM nexa_b13_refresh_queue_head(NEW.tenant_id, NEW.base_priority);
                END IF;
            ELSE
                IF OLD.state = NEW.state
                   AND OLD.desired_state = NEW.desired_state
                   AND OLD.recovery_intent IS NOT DISTINCT FROM NEW.recovery_intent
                   AND OLD.base_priority = NEW.base_priority
                   AND OLD.ready_sequence = NEW.ready_sequence
                   AND NOT EXISTS (
                       SELECT 1 FROM queue_heads
                       WHERE tenant_id = NEW.tenant_id
                         AND priority = NEW.base_priority
                         AND candidate_job_id = NEW.job_id
                   )
                THEN
                    RETURN NEW;
                END IF;
                FOR p IN
                    SELECT DISTINCT priority FROM (
                        VALUES (OLD.base_priority), (NEW.base_priority)
                    ) AS priorities(priority) ORDER BY priority
                LOOP
                    PERFORM nexa_b13_refresh_queue_head(NEW.tenant_id, p);
                END LOOP;
            END IF;
            RETURN NEW;
        END $$;

        CREATE OR REPLACE FUNCTION nexa_b13_refresh_queue_submitter(p_tenant uuid, p_user uuid)
        RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_tenant::text || ':' || p_user::text, 130014)
            );
            IF EXISTS (
                SELECT 1 FROM jobs
                WHERE tenant_id = p_tenant AND submitter_user_id = p_user
                  AND {q} LIMIT 1
            ) THEN
                INSERT INTO queue_submitters (tenant_id, user_id)
                VALUES (p_tenant, p_user) ON CONFLICT DO NOTHING;
            ELSE
                DELETE FROM queue_submitters
                WHERE tenant_id = p_tenant AND user_id = p_user;
            END IF;
        END $$;

        CREATE OR REPLACE FUNCTION nexa_b13_job_submitter_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF {new_q} THEN
                    PERFORM nexa_b13_refresh_queue_submitter(
                        NEW.tenant_id, NEW.submitter_user_id
                    );
                END IF;
            ELSE
                IF OLD.state = NEW.state
                   AND OLD.desired_state = NEW.desired_state
                   AND OLD.recovery_intent IS NOT DISTINCT FROM NEW.recovery_intent
                   AND OLD.tenant_id = NEW.tenant_id
                   AND OLD.submitter_user_id = NEW.submitter_user_id THEN
                    RETURN NEW;
                END IF;
                IF {old_q} THEN
                    PERFORM nexa_b13_refresh_queue_submitter(
                        OLD.tenant_id, OLD.submitter_user_id
                    );
                END IF;
                IF {new_q} THEN
                    PERFORM nexa_b13_refresh_queue_submitter(
                        NEW.tenant_id, NEW.submitter_user_id
                    );
                END IF;
            END IF;
            RETURN NEW;
        END $$;

        CREATE OR REPLACE FUNCTION nexa_b13_emit_eligibility(
            p_tenant uuid, p_old jsonb, p_new jsonb
        ) RETURNS void LANGUAGE plpgsql AS $$
        DECLARE capabilities_changed boolean;
        BEGIN
            IF p_old = p_new THEN RETURN; END IF;
            capabilities_changed :=
                p_old->'inventory'->'architecture' IS DISTINCT FROM
                    p_new->'inventory'->'architecture'
                OR p_old->'inventory'->'workload_capabilities' IS DISTINCT FROM
                    p_new->'inventory'->'workload_capabilities';
            IF capabilities_changed THEN
                IF NOT EXISTS (SELECT 1 FROM jobs WHERE tenant_id = p_tenant
                               AND state = 'QUEUED' LIMIT 1) THEN RETURN; END IF;
            ELSIF NOT EXISTS (
                SELECT 1 FROM job_specs sp JOIN jobs j ON j.job_id = sp.job_id
                WHERE sp.tenant_id = p_tenant AND {j_q}
                  AND (
                    (sp.cpu_millis > least((p_old->>'cpu')::bigint, (p_new->>'cpu')::bigint)
                     AND sp.cpu_millis <= greatest(
                         (p_old->>'cpu')::bigint, (p_new->>'cpu')::bigint))
                    OR (sp.memory_bytes > least(
                         (p_old->>'memory')::bigint, (p_new->>'memory')::bigint)
                     AND sp.memory_bytes <= greatest(
                         (p_old->>'memory')::bigint, (p_new->>'memory')::bigint))
                    OR (sp.gpu_count > least((p_old->>'gpu')::integer, (p_new->>'gpu')::integer)
                     AND sp.gpu_count <= greatest(
                         (p_old->>'gpu')::integer, (p_new->>'gpu')::integer))
                  ) LIMIT 1
            ) THEN RETURN; END IF;
            INSERT INTO queue_eligibility_events
                (tenant_id, occurred_at, old_state, new_state)
            VALUES (p_tenant, clock_timestamp(), p_old, p_new);
        END $$;

        CREATE OR REPLACE FUNCTION nexa_b13_job_queue_size() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE was_queued boolean := false;
        DECLARE is_queued boolean;
        BEGIN
            is_queued := {new_q};
            IF TG_OP = 'UPDATE' THEN
                was_queued := {old_q};
            END IF;
            IF was_queued AND NOT is_queued THEN
                PERFORM nexa_b13_queue_size_delta(NEW.job_id, -1);
            ELSIF is_queued AND NOT was_queued THEN
                PERFORM nexa_b13_queue_size_delta(NEW.job_id, 1);
            END IF;
            RETURN NEW;
        END $$;

        CREATE OR REPLACE FUNCTION nexa_b13_spec_queue_size() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM jobs WHERE job_id = NEW.job_id AND {q}) THEN
                PERFORM nexa_b13_queue_size_delta(NEW.job_id, 1);
            END IF;
            RETURN NEW;
        END $$;
    """


def _recreate_indexes(widened: bool) -> None:
    for name, columns in _INDEXES:
        op.drop_index(name, table_name="jobs")
        op.create_index(name, "jobs", list(columns), postgresql_where=_queued(widened))


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_jobs_queued_dispatchable"),
        "jobs",
        "state <> 'QUEUED' OR (desired_state = 'RUNNING' AND recovery_intent IS NULL) "
        "OR (desired_state = 'PAUSED' "
        "AND recovery_intent IS NOT DISTINCT FROM 'CHECKPOINT_FOR_PAUSE')",
    )
    _recreate_indexes(True)
    op.execute(_functions(True))
    # Control reasons are stored verbatim in audit only (ControlRequest 1..256).
    op.alter_column("audit_records", "reason", type_=String(256), existing_type=String(128))
    op.create_index(
        "ix_idempotency_records_resource",
        "idempotency_records",
        ["resource_id", "idempotency_id"],
        postgresql_where="resource_id IS NOT NULL",
    )
    swept = ", ".join(f"'{name}'" for name in SWEPT_OPERATIONS)
    op.create_index(
        "ix_idempotency_records_b15_sweep",
        "idempotency_records",
        ["expires_at", "idempotency_id"],
        postgresql_where=f"state = 'COMPLETED' AND operation_id IN ({swept})",
    )
    types = ", ".join(f"'{name}'" for name in RECOVERY_EVENT_TYPES)
    op.create_index(
        "ix_events_b15_recovery",
        "events",
        ["created_at", "event_id"],
        postgresql_where=f"event_type IN ({types})",
    )
    # B15-R07: the latest heartbeat's READY check, kept while DISABLED holds STARTING.
    op.add_column(
        "worker_incarnations",
        Column("ready_checked_at", DateTime(timezone=True), nullable=True),
    )
    # B15-R25: before B15 no path stamped terminal_at, and a submit record expired
    # a fixed retention after submission. A terminal row is immutable, so its last
    # update bounds the terminal time from above; each swept record keeps its own
    # retention, now counted from that time instead of from its creation.
    op.execute(f"""
        WITH backfilled AS (
            UPDATE jobs SET terminal_at = updated_at
            WHERE state IN ('SUCCEEDED', 'FAILED', 'CANCELLED') AND terminal_at IS NULL
            RETURNING job_id, terminal_at
        )
        UPDATE idempotency_records AS r
        SET expires_at = r.expires_at + (b.terminal_at - r.created_at)
        FROM backfilled AS b
        WHERE r.resource_id = b.job_id AND r.operation_id IN ({swept})
          AND b.terminal_at > r.created_at
    """)


def downgrade() -> None:
    # The locks keep a new checkpoint-for-pause job or long audit reason from
    # landing between a precheck and the narrowing DDL below.
    op.execute("LOCK TABLE jobs, audit_records IN ACCESS EXCLUSIVE MODE")
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM jobs WHERE state = 'QUEUED' AND NOT ({_queued(False)})) THEN
                RAISE EXCEPTION 'a checkpoint-for-pause job is queued; resolve it first'
                    USING ERRCODE = 'object_not_in_prerequisite_state';
            END IF;
            IF EXISTS (SELECT 1 FROM audit_records WHERE char_length(reason) > 128) THEN
                RAISE EXCEPTION 'an audit reason exceeds 128 characters; it cannot be narrowed'
                    USING ERRCODE = 'object_not_in_prerequisite_state';
            END IF;
        END $$;
    """)
    op.drop_column("worker_incarnations", "ready_checked_at")
    op.drop_index("ix_events_b15_recovery", table_name="events")
    op.drop_index("ix_idempotency_records_b15_sweep", table_name="idempotency_records")
    op.drop_index("ix_idempotency_records_resource", table_name="idempotency_records")
    op.alter_column("audit_records", "reason", type_=String(128), existing_type=String(256))
    op.execute(_functions(False))
    _recreate_indexes(False)
    op.drop_constraint(op.f("ck_jobs_queued_dispatchable"), "jobs", type_="check")
