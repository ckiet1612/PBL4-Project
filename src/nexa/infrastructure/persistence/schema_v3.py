"""B07 additive artifact-storage metadata layered on the frozen B06 schema."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    UniqueConstraint,
    func,
    text,
)

from nexa.infrastructure.persistence import schema_v2

# This is an additive physical extension. The compatibility generation remains
# stable; the Alembic head still identifies the new schema explicitly.
SCHEMA_GENERATION = schema_v2.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v2.metadata.naming_convention))
for _table in schema_v2.metadata.tables.values():
    _table.to_metadata(metadata)

upload_sessions = metadata.tables["upload_sessions"]
upload_sessions.append_column(Column("idempotency_id", schema_v2.UUID_TYPE))
upload_sessions.append_constraint(
    ForeignKeyConstraint(
        ["idempotency_id"], ["idempotency_records.idempotency_id"], ondelete="RESTRICT"
    )
)
upload_sessions.append_constraint(
    UniqueConstraint("idempotency_id", name="uq_upload_sessions_idempotency")
)

artifact_storage_counters = Table(
    "artifact_storage_counters",
    metadata,
    Column("tenant_id", schema_v2.UUID_TYPE, nullable=False),
    Column("committed_bytes", BigInteger, nullable=False, server_default=text("0")),
    Column("reserved_bytes", BigInteger, nullable=False, server_default=text("0")),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("created_at", schema_v2.UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    Column("updated_at", schema_v2.UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("tenant_id"),
    ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="RESTRICT"),
    CheckConstraint(
        "committed_bytes BETWEEN 0 AND 9223372036854775807", name="committed_nonnegative"
    ),
    CheckConstraint(
        "reserved_bytes BETWEEN 0 AND 9223372036854775807", name="reserved_nonnegative"
    ),
    CheckConstraint("version BETWEEN 1 AND 9223372036854775807", name="version_positive"),
)

__all__ = ["SCHEMA_GENERATION", "artifact_storage_counters", "metadata", "upload_sessions"]
