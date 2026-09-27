"""B15 control/recovery: checkpoint-for-pause jobs share the B13 queue indexes."""

from sqlalchemy import CheckConstraint, Column, DateTime, Index, MetaData, String

from nexa.infrastructure.persistence import schema_v15

SCHEMA_GENERATION = schema_v15.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v15.metadata.naming_convention))
for _table in schema_v15.metadata.tables.values():
    _table.to_metadata(metadata)

RECOVERY_EVENT_TYPES = (
    "ATTEMPT_FAILED",
    "ATTEMPT_FENCED",
    "ATTEMPT_LOST",
    "LEASE_REVOKED",
    "ALLOCATION_RELEASED",
    "RETRY_READY",
    "RETRY_BLOCKED",
    "PAUSE_ABORTED",
    "CHECKPOINT_CORRUPT",
    "CHECKPOINT_INCOMPATIBLE",
    "CHECKPOINT_REJECTED",
    "CHECKPOINT_RESTORE_SELECTED",
    "CHECKPOINT_FALLBACK_TO_INPUT",
    "CHECKPOINT_RESTORE_UNAVAILABLE",
)

jobs = metadata.tables["jobs"]
# A queued job is dispatchable as RUN or as CHECKPOINT_FOR_PAUSE; the CHECK
# makes state = 'QUEUED' alone the complete queue predicate.
jobs.append_constraint(
    CheckConstraint(
        "state <> 'QUEUED' OR (desired_state = 'RUNNING' AND recovery_intent IS NULL) "
        "OR (desired_state = 'PAUSED' "
        "AND recovery_intent IS NOT DISTINCT FROM 'CHECKPOINT_FOR_PAUSE')",
        name="queued_dispatchable",
    )
)
_QUEUE_INDEXES = {
    "ix_jobs_b13_head": ("tenant_id", "base_priority", "ready_sequence", "job_id"),
    "ix_jobs_b13_submitter": (
        "tenant_id",
        "submitter_user_id",
        "base_priority",
        "ready_sequence",
        "job_id",
    ),
    "ix_jobs_b13_priority_age": (
        "tenant_id",
        "base_priority",
        "eligible_since",
        "ready_sequence",
        "job_id",
    ),
    "ix_jobs_b13_submitter_age": (
        "tenant_id",
        "submitter_user_id",
        "base_priority",
        "eligible_since",
        "ready_sequence",
        "job_id",
    ),
}
for _index in [index for index in jobs.indexes if index.name in _QUEUE_INDEXES]:
    jobs.indexes.discard(_index)
for _name, _columns in _QUEUE_INDEXES.items():
    Index(
        _name,
        *(jobs.c[column] for column in _columns),
        postgresql_where=jobs.c.state == "QUEUED",
    )

audit_records = metadata.tables["audit_records"]
audit_records.c.reason.type = String(256)

idempotency_records = metadata.tables["idempotency_records"]
Index(
    "ix_idempotency_records_resource",
    idempotency_records.c.resource_id,
    idempotency_records.c.idempotency_id,
    postgresql_where=idempotency_records.c.resource_id.is_not(None),
)
# The retention sweep scans only records it may delete; expired records of other
# operations would otherwise accumulate ahead of it in ix_idempotency_records_expiry.
SWEPT_OPERATIONS = ("submitJob", "cancelJob", "pauseJob", "resumeJob", "retryFailedJob")
Index(
    "ix_idempotency_records_b15_sweep",
    idempotency_records.c.expires_at,
    idempotency_records.c.idempotency_id,
    postgresql_where=(idempotency_records.c.state == "COMPLETED")
    & idempotency_records.c.operation_id.in_(SWEPT_OPERATIONS),
)

events = metadata.tables["events"]
Index(
    "ix_events_b15_recovery",
    events.c.created_at,
    events.c.event_id,
    postgresql_where=events.c.event_type.in_(RECOVERY_EVENT_TYPES),
)

worker_incarnations = metadata.tables["worker_incarnations"]
# DB time of this incarnation's latest heartbeat that passed every READY check except
# the admin state; NULL after one that failed. Enable of a DISABLED worker, whose
# health is held at STARTING, relies on it (B15-R07).
worker_incarnations.append_column(Column("ready_checked_at", DateTime(timezone=True)))
