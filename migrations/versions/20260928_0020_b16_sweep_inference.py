"""Store the B16 sweep expansion and the batch-inference item count.

``sweep_parents`` gains the canonical request hash, the wire base spec and the
immutable ordered expansion, so a replay resumes unfinished child indexes from
the database alone (workloads-checkpoints.md, "Partial acceptance and replay").
No code wrote sweep rows before B16, so the NOT NULL columns are added to an
empty table; the upgrade refuses otherwise. Parent identity columns become
immutable with non-decreasing outcome counts, and ``sweep_children`` outcomes
become insert-only like the other immutable records.

``inference_extents`` records the item count N and chunk size of a batch-inference
job once, at the first fenced publish that reports them (B16-R06). A partial index
lets the retention sweep reach expired ``submitSweep`` records (B16-R05).
Downgrade drops everything this revision added and refuses while a sweep or an
extent exists.
"""

from alembic import op
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20260928_0020"
down_revision = "20260926_0019"
branch_labels = None
depends_on = None

_IMMUTABLE_PARENT_COLUMNS = (
    "sweep_id",
    "tenant_id",
    "submitter_user_id",
    "base_spec_checksum",
    "idempotency_context",
    "child_count",
    "request_hash",
    "base_spec",
    "expansion",
    "created_at",
)


def upgrade() -> None:
    op.execute("LOCK TABLE sweep_parents, sweep_children IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM sweep_parents) THEN
                RAISE EXCEPTION 'sweep_parents has rows without a stored expansion'
                    USING ERRCODE = 'object_not_in_prerequisite_state';
            END IF;
        END $$;
    """)
    op.add_column("sweep_parents", Column("request_hash", String(71), nullable=False))
    op.add_column("sweep_parents", Column("base_spec", JSONB, nullable=False))
    op.add_column("sweep_parents", Column("expansion", JSONB, nullable=False))
    op.create_check_constraint(
        op.f("ck_sweep_parents_request_hash"),
        "sweep_parents",
        "request_hash ~ '^sha256:[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        op.f("ck_sweep_parents_expansion_length"),
        "sweep_parents",
        "jsonb_typeof(expansion) = 'array' AND jsonb_array_length(expansion) = child_count",
    )
    old = ", ".join(f"OLD.{name}" for name in _IMMUTABLE_PARENT_COLUMNS)
    new = ", ".join(f"NEW.{name}" for name in _IMMUTABLE_PARENT_COLUMNS)
    op.execute(f"""
        CREATE FUNCTION nexa_b16_guard_sweep_parent() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               OR ROW({new}) IS DISTINCT FROM ROW({old})
               OR NEW.accepted_count < OLD.accepted_count
               OR NEW.rejected_count < OLD.rejected_count THEN
                RAISE EXCEPTION 'sweep_parents identity and outcomes are immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER trg_sweep_parents_guard
        BEFORE UPDATE OR DELETE ON sweep_parents
        FOR EACH ROW EXECUTE FUNCTION nexa_b16_guard_sweep_parent()
    """)
    op.execute("""
        CREATE TRIGGER trg_sweep_children_immutable
        BEFORE UPDATE OR DELETE ON sweep_children
        FOR EACH ROW EXECUTE FUNCTION nexa_reject_immutable_mutation()
    """)
    op.create_table(
        "inference_extents",
        Column("job_id", UUID(as_uuid=True), nullable=False),
        Column("tenant_id", UUID(as_uuid=True), nullable=False),
        Column("item_count", BigInteger, nullable=False),
        Column("chunk_size", Integer, nullable=False),
        Column("source_attempt_id", UUID(as_uuid=True), nullable=False),
        Column("source_job_fence", BigInteger, nullable=False),
        Column(
            "recorded_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.clock_timestamp(),
        ),
        PrimaryKeyConstraint("job_id", name=op.f("pk_inference_extents")),
        ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["jobs.tenant_id", "jobs.job_id"],
            name=op.f("fk_inference_extents_tenant_id_jobs"),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "job_id", "source_attempt_id", "source_job_fence"],
            ["attempts.tenant_id", "attempts.job_id", "attempts.attempt_id", "attempts.job_fence"],
            name=op.f("fk_inference_extents_tenant_id_attempts"),
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "item_count BETWEEN 1 AND 1000000000", name=op.f("ck_inference_extents_item_count")
        ),
        CheckConstraint(
            "chunk_size BETWEEN 1 AND 100000", name=op.f("ck_inference_extents_chunk_size")
        ),
    )
    op.execute("""
        CREATE TRIGGER trg_inference_extents_immutable
        BEFORE UPDATE OR DELETE ON inference_extents
        FOR EACH ROW EXECUTE FUNCTION nexa_reject_immutable_mutation()
    """)
    op.create_index(
        "ix_idempotency_records_b16_sweep_parent",
        "idempotency_records",
        ["expires_at", "idempotency_id"],
        postgresql_where="state = 'COMPLETED' AND operation_id = 'submitSweep'",
    )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE sweep_parents, sweep_children, inference_extents IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM sweep_parents) OR EXISTS (SELECT 1 FROM inference_extents) THEN
                RAISE EXCEPTION 'B16 sweep or inference extent rows exist; they cannot be kept'
                    USING ERRCODE = 'object_not_in_prerequisite_state';
            END IF;
        END $$;
    """)
    op.drop_index("ix_idempotency_records_b16_sweep_parent", table_name="idempotency_records")
    op.drop_table("inference_extents")
    op.execute("DROP TRIGGER trg_sweep_children_immutable ON sweep_children")
    op.execute("DROP TRIGGER trg_sweep_parents_guard ON sweep_parents")
    op.execute("DROP FUNCTION nexa_b16_guard_sweep_parent()")
    op.drop_constraint(op.f("ck_sweep_parents_expansion_length"), "sweep_parents", type_="check")
    op.drop_constraint(op.f("ck_sweep_parents_request_hash"), "sweep_parents", type_="check")
    op.drop_column("sweep_parents", "expansion")
    op.drop_column("sweep_parents", "base_spec")
    op.drop_column("sweep_parents", "request_hash")
