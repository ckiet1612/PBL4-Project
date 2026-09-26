"""B13 transactionally maintained queued submitter set."""

from sqlalchemy import Column, ForeignKey, Index, MetaData, Table

from nexa.infrastructure.persistence import schema_v8
from nexa.infrastructure.persistence.schema_v2 import UUID_TYPE

SCHEMA_GENERATION = schema_v8.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v8.metadata.naming_convention))
for _table in schema_v8.metadata.tables.values():
    _table.to_metadata(metadata)

jobs = metadata.tables["jobs"]
Index(
    "ix_jobs_b13_submitter",
    jobs.c.tenant_id,
    jobs.c.submitter_user_id,
    jobs.c.base_priority,
    jobs.c.ready_sequence,
    jobs.c.job_id,
    postgresql_where=(jobs.c.state == "QUEUED")
    & (jobs.c.desired_state == "RUNNING")
    & jobs.c.recovery_intent.is_(None),
)

queue_submitters = Table(
    "queue_submitters",
    metadata,
    Column(
        "tenant_id",
        UUID_TYPE,
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column(
        "user_id", UUID_TYPE, ForeignKey("users.user_id", ondelete="RESTRICT"), primary_key=True
    ),
)
