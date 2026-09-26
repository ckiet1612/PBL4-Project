"""Maintain the existing per-tenant/priority queue heads in Job transactions."""

from alembic import op

revision = "20260925_0008"
down_revision = "20260923_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_jobs_b13_head",
        "jobs",
        ["tenant_id", "base_priority", "ready_sequence", "job_id"],
        postgresql_where="state = 'QUEUED' AND desired_state = 'RUNNING' "
        "AND recovery_intent IS NULL",
    )
    op.execute("""
        DELETE FROM queue_heads;
        INSERT INTO queue_heads
            (tenant_id, priority, candidate_job_id, ready_sequence,
             eligible_since, retry_ready_at, rebuilt_at)
        SELECT DISTINCT ON (tenant_id, base_priority)
            tenant_id, base_priority, job_id, ready_sequence,
            eligible_since, retry_ready_at, clock_timestamp()
        FROM jobs
        WHERE state = 'QUEUED' AND desired_state = 'RUNNING'
          AND recovery_intent IS NULL
        ORDER BY tenant_id, base_priority, ready_sequence, job_id;
    """)
    op.execute("""
        CREATE FUNCTION nexa_b13_refresh_queue_head(p_tenant uuid, p_priority integer)
        RETURNS void LANGUAGE plpgsql AS $$
        DECLARE head jobs%ROWTYPE;
        DECLARE current_head queue_heads%ROWTYPE;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_tenant::text || ':' || p_priority::text, 130013)
            );
            SELECT * INTO head FROM jobs
            WHERE tenant_id = p_tenant AND base_priority = p_priority
              AND state = 'QUEUED' AND desired_state = 'RUNNING'
              AND recovery_intent IS NULL
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

        CREATE FUNCTION nexa_b13_job_head_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE p integer;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state = 'QUEUED' AND NEW.desired_state = 'RUNNING'
                   AND NEW.recovery_intent IS NULL THEN
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

        CREATE TRIGGER b13_job_head_insert AFTER INSERT ON jobs
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_job_head_change();
        CREATE TRIGGER b13_job_head_update
        AFTER UPDATE OF state, desired_state, recovery_intent,
                        base_priority, ready_sequence,
                        eligible_since, retry_ready_at ON jobs
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_job_head_change();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER b13_job_head_update ON jobs")
    op.execute("DROP TRIGGER b13_job_head_insert ON jobs")
    op.execute("DROP FUNCTION nexa_b13_job_head_change()")
    op.execute("DROP FUNCTION nexa_b13_refresh_queue_head(uuid, integer)")
    op.execute("DELETE FROM queue_heads")
    op.drop_index("ix_jobs_b13_head", table_name="jobs")
