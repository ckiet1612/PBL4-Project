"""B13 per-submitter indexed age probes for resumed queues."""

from sqlalchemy import Index, MetaData

from nexa.infrastructure.persistence import schema_v12

SCHEMA_GENERATION = schema_v12.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v12.metadata.naming_convention))
for _table in schema_v12.metadata.tables.values():
    _table.to_metadata(metadata)

jobs = metadata.tables["jobs"]
Index(
    "ix_jobs_b13_submitter_age",
    jobs.c.tenant_id,
    jobs.c.submitter_user_id,
    jobs.c.base_priority,
    jobs.c.eligible_since,
    jobs.c.ready_sequence,
    jobs.c.job_id,
    postgresql_where=(jobs.c.state == "QUEUED")
    & (jobs.c.desired_state == "RUNNING")
    & jobs.c.recovery_intent.is_(None),
)
