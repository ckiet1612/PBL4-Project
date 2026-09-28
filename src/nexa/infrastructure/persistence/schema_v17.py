"""B16 sweep expansion and batch-inference extent."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from nexa.infrastructure.persistence import schema_v16
from nexa.infrastructure.persistence.schema_v2 import UTC_TIMESTAMP, UUID_TYPE

SCHEMA_GENERATION = schema_v16.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v16.metadata.naming_convention))
for _table in schema_v16.metadata.tables.values():
    _table.to_metadata(metadata)

# The parent keeps the canonical request hash, the wire base spec and the ordered
# expansion ({parameters, parameter_hash} per child_index), so a replay resumes the
# unfinished indexes from the database alone; idempotency_context is the parent's
# submitSweep record ID. Only the outcome counts may change, and never decrease.
sweep_parents = metadata.tables["sweep_parents"]
sweep_parents.append_column(Column("request_hash", String(71), nullable=False))
sweep_parents.append_column(Column("base_spec", JSONB, nullable=False))
sweep_parents.append_column(Column("expansion", JSONB, nullable=False))
sweep_parents.append_constraint(
    CheckConstraint("request_hash ~ '^sha256:[0-9a-f]{64}$'", name="request_hash")
)
sweep_parents.append_constraint(
    CheckConstraint(
        "jsonb_typeof(expansion) = 'array' AND jsonb_array_length(expansion) = child_count",
        name="expansion_length",
    )
)

# Item count N and chunk size of a batch-inference job, recorded once by the first
# fenced publish that reports them; every later chunk manifest and result must match
# (B16-R06).
inference_extents = Table(
    "inference_extents",
    metadata,
    Column("job_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("item_count", BigInteger, nullable=False),
    Column("chunk_size", Integer, nullable=False),
    Column("source_attempt_id", UUID_TYPE, nullable=False),
    Column("source_job_fence", BigInteger, nullable=False),
    Column("recorded_at", UTC_TIMESTAMP, nullable=False, server_default=func.clock_timestamp()),
    ForeignKeyConstraint(
        ["tenant_id", "job_id"], ["jobs.tenant_id", "jobs.job_id"], ondelete="RESTRICT"
    ),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "source_attempt_id", "source_job_fence"],
        ["attempts.tenant_id", "attempts.job_id", "attempts.attempt_id", "attempts.job_fence"],
        ondelete="RESTRICT",
    ),
    CheckConstraint("item_count BETWEEN 1 AND 1000000000", name="item_count"),
    CheckConstraint("chunk_size BETWEEN 1 AND 100000", name="chunk_size"),
)

# The retention sweep deletes an expired submitSweep record only once every child
# has an outcome and no accepted child still has its own submitJob record, i.e.
# every accepted child is past its terminal retention (B16-R05).
SWEEP_PARENT_OPERATION = "submitSweep"
idempotency_records = metadata.tables["idempotency_records"]
Index(
    "ix_idempotency_records_b16_sweep_parent",
    idempotency_records.c.expires_at,
    idempotency_records.c.idempotency_id,
    postgresql_where=(idempotency_records.c.state == "COMPLETED")
    & (idempotency_records.c.operation_id == SWEEP_PARENT_OPERATION),
)
