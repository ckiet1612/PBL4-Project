"""Idempotency retention sweep for job requests, run by the leader on the 1 s probe.

Every terminal path raises the job's records to `terminal_at + retention`, so a
record past `expires_at` whose job is terminal can no longer be replayed usefully.
PENDING records, other operations and records of a live job are never deleted.
An expired `submitSweep` record goes only once every child has an outcome and no
accepted child still holds its own `submitJob` record, i.e. every accepted child
is past its terminal retention; an unfinished sweep stays resumable (B16-R05).
"""

from sqlalchemy import and_, exists, func, select

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.schema_v16 import SWEPT_OPERATIONS
from nexa.infrastructure.persistence.schema_v17 import SWEEP_PARENT_OPERATION

BATCH_SIZE = 100
_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")


def expired_records(session, limit=BATCH_SIZE) -> list:
    """Lock due records first (idempotency precedes leadership); SKIP LOCKED leaves in-use keys."""
    records = s.idempotency_records
    # A correlated scalar subquery is not flattened into a join, so the plan walks
    # the partial sweep index in expiry order instead of scanning every job when
    # statistics show few terminal jobs (B15-R18).
    job_state = (
        select(s.jobs.c.state).where(s.jobs.c.job_id == records.c.resource_id).scalar_subquery()
    )
    due = list(
        session.execute(
            select(records.c.idempotency_id)
            .where(
                records.c.state == "COMPLETED",
                records.c.operation_id.in_(SWEPT_OPERATIONS),
                # Stable now() bounds the index range; volatile clock_timestamp()
                # would filter every index entry. The earlier instant only keeps more.
                records.c.expires_at < func.now(),
                # Terminal states are immutable, so the job row needs no lock.
                job_state.in_(_TERMINAL),
            )
            .order_by(records.c.expires_at, records.c.idempotency_id)
            .limit(limit)
            .with_for_update(of=records, skip_locked=True)
        ).scalars()
    )
    if len(due) < limit:
        due.extend(_expired_sweep_records(session, limit - len(due)))
    return due


def _expired_sweep_records(session, limit: int) -> list:
    records = s.idempotency_records
    child_records = records.alias("child_records")
    finished = exists().where(
        s.sweep_parents.c.sweep_id == records.c.resource_id,
        s.sweep_parents.c.accepted_count + s.sweep_parents.c.rejected_count
        == s.sweep_parents.c.child_count,
    )
    child_replayable = exists().where(
        s.sweep_children.c.sweep_id == records.c.resource_id,
        child_records.c.resource_id == s.sweep_children.c.job_id,
        child_records.c.operation_id == "submitJob",
    )
    return list(
        session.execute(
            select(records.c.idempotency_id)
            .where(
                and_(
                    records.c.state == "COMPLETED",
                    records.c.operation_id == SWEEP_PARENT_OPERATION,
                    records.c.expires_at < func.now(),
                ),
                finished,
                ~child_replayable,
            )
            .order_by(records.c.expires_at, records.c.idempotency_id)
            .limit(limit)
            .with_for_update(of=records, skip_locked=True)
        ).scalars()
    )
