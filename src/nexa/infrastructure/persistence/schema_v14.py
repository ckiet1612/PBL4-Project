"""B13 per-tenant held-quota headroom and queued request-size counts."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
)

from nexa.infrastructure.persistence import schema_v13
from nexa.infrastructure.persistence.schema_v2 import UUID_TYPE

SCHEMA_GENERATION = schema_v13.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v13.metadata.naming_convention))
for _table in schema_v13.metadata.tables.values():
    _table.to_metadata(metadata)

quota_headroom_steps = Table(
    "quota_headroom_steps",
    metadata,
    Column(
        "tenant_id",
        UUID_TYPE,
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("resource", Text, primary_key=True),
    Column("upper_bound", BigInteger, primary_key=True),
    Column("resumed_at", DateTime(timezone=True)),
    CheckConstraint("resource IN ('cpu', 'memory')", name="resource"),
    CheckConstraint("upper_bound >= 0", name="upper_bound"),
)
queue_request_sizes = Table(
    "queue_request_sizes",
    metadata,
    Column(
        "tenant_id",
        UUID_TYPE,
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("cpu_millis", BigInteger, primary_key=True),
    Column("memory_bytes", BigInteger, primary_key=True),
    Column("queued_jobs", Integer, nullable=False),
    CheckConstraint("queued_jobs > 0", name="queued_jobs"),
)
