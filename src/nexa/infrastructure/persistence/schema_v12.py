"""B13 indexed age existence probes for bounded promotion."""

from sqlalchemy import Index, MetaData

from nexa.infrastructure.persistence import schema_v11

SCHEMA_GENERATION = schema_v11.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v11.metadata.naming_convention))
for _table in schema_v11.metadata.tables.values():
    _table.to_metadata(metadata)

jobs = metadata.tables["jobs"]
Index(
    "ix_jobs_b13_priority_age",
    jobs.c.tenant_id,
    jobs.c.base_priority,
    jobs.c.eligible_since,
    jobs.c.ready_sequence,
    jobs.c.job_id,
    postgresql_where=(jobs.c.state == "QUEUED")
    & (jobs.c.desired_state == "RUNNING")
    & jobs.c.recovery_intent.is_(None),
)
