"""B13 indexed, transactionally maintained queued priority heads."""

from sqlalchemy import Index, MetaData

from nexa.infrastructure.persistence import schema_v6

SCHEMA_GENERATION = schema_v6.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v6.metadata.naming_convention))
for _table in schema_v6.metadata.tables.values():
    _table.to_metadata(metadata)

jobs = metadata.tables["jobs"]
Index(
    "ix_jobs_b13_head",
    jobs.c.tenant_id,
    jobs.c.base_priority,
    jobs.c.ready_sequence,
    jobs.c.job_id,
    postgresql_where=(jobs.c.state == "QUEUED")
    & (jobs.c.desired_state == "RUNNING")
    & jobs.c.recovery_intent.is_(None),
)
