"""B13 bounded, durable queue eligibility transitions."""

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, MetaData, Table
from sqlalchemy.dialects.postgresql import JSONB

from nexa.infrastructure.persistence import schema_v10
from nexa.infrastructure.persistence.schema_v2 import UUID_TYPE

SCHEMA_GENERATION = schema_v10.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v10.metadata.naming_convention))
for _table in schema_v10.metadata.tables.values():
    _table.to_metadata(metadata)

queue_eligibility_events = Table(
    "queue_eligibility_events",
    metadata,
    Column("event_id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "tenant_id", UUID_TYPE, ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), nullable=False
    ),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("old_state", JSONB, nullable=False),
    Column("new_state", JSONB, nullable=False),
    Column("last_job_id", UUID_TYPE),
    Column("last_processed_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
)
Index(
    "ix_queue_eligibility_pending",
    queue_eligibility_events.c.tenant_id,
    queue_eligibility_events.c.event_id,
    postgresql_where=queue_eligibility_events.c.completed_at.is_(None),
)
Index(
    "ix_job_specs_b13_cpu",
    metadata.tables["job_specs"].c.tenant_id,
    metadata.tables["job_specs"].c.cpu_millis,
    metadata.tables["job_specs"].c.job_id,
)
Index(
    "ix_job_specs_b13_memory",
    metadata.tables["job_specs"].c.tenant_id,
    metadata.tables["job_specs"].c.memory_bytes,
    metadata.tables["job_specs"].c.job_id,
)
