"""Idempotency retention sweep for job requests, run by the leader on the 1 s probe.

Every terminal path raises the job's records to `terminal_at + retention`, so a
record past `expires_at` whose job is terminal can no longer be replayed usefully.
PENDING records, other operations and records of a live job are never deleted.
An expired `submitSweep` record goes only once every child has an outcome and no
accepted child still holds its own `submitJob` record, i.e. every accepted child
is past its terminal retention; an unfinished sweep stays resumable (B16-R05).

Each tick walks one bounded batch of expired records. A record that is not due is
deferred (its `expires_at` raised) so the walk leaves it instead of reading it
again every second (B15-R18). Deferral only raises `expires_at` and is not a
deletion rule: the terminal path's `greatest(expires_at, terminal_at + retention)`
still sets the exact terminal retention, because a live Job ends after the deferral
was written and `RECHECK` is shorter than the 30-day minimum retention.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.schema_v16 import SWEPT_OPERATIONS
from nexa.infrastructure.persistence.schema_v17 import SWEEP_PARENT_OPERATION

BATCH_SIZE = 100
# Re-examination delay of a record whose due time is not known yet: the Job is live,
# the record has no Job, or its sweep is unfinished.
RECHECK = timedelta(days=1)
_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")


@dataclass(frozen=True)
class SweepBatch:
    due: list
    # (idempotency_id, until): raise expires_at to at least `until`.
    deferred: list[tuple[object, datetime]]


def sweep_batch(session, limit=BATCH_SIZE) -> SweepBatch:
    """Lock and classify one batch; the caller deletes `due` and defers the rest.

    Idempotency rows are locked before leadership; SKIP LOCKED leaves in-use keys.
    """
    records = s.idempotency_records
    # A correlated scalar subquery is not flattened into a join, so the plan walks
    # the partial sweep index in expiry order and reads one job per record (B15-R18).
    job_state = (
        select(s.jobs.c.state).where(s.jobs.c.job_id == records.c.resource_id).scalar_subquery()
    )
    rows = session.execute(
        select(records.c.idempotency_id, job_state.label("job_state"))
        .where(
            records.c.state == "COMPLETED",
            records.c.operation_id.in_(SWEPT_OPERATIONS),
            # Stable now() bounds the index range; volatile clock_timestamp()
            # would filter every index entry. The earlier instant only keeps more.
            records.c.expires_at < func.now(),
        )
        .order_by(records.c.expires_at, records.c.idempotency_id)
        .limit(limit)
        .with_for_update(of=records, skip_locked=True)
    ).all()
    # Terminal states are immutable, so the job row needs no lock.
    due = [row.idempotency_id for row in rows if row.job_state in _TERMINAL]
    waiting = [row.idempotency_id for row in rows if row.job_state not in _TERMINAL]
    parents = _sweep_parent_rows(session, limit - len(rows)) if len(rows) < limit else []
    if not waiting and not parents:
        return SweepBatch(due=due, deferred=[])
    now = session.execute(select(func.now())).scalar_one()
    deferred = [(record_id, now + RECHECK) for record_id in waiting]
    for row in parents:
        if not row.finished:
            deferred.append((row.idempotency_id, now + RECHECK))
        elif row.child_expiry is None:
            due.append(row.idempotency_id)
        elif row.child_expiry > now:
            # Due no earlier than the last child record, which is only ever raised.
            deferred.append((row.idempotency_id, row.child_expiry))
        # Otherwise a child record is itself due or locked: walked again next tick.
    return SweepBatch(due=due, deferred=deferred)


def _sweep_parent_rows(session, limit: int) -> list:
    records = s.idempotency_records
    child_records = records.alias("child_records")
    parents = s.sweep_parents
    # NULL when the record has no sweep; either way it is not finished.
    finished = (
        select(parents.c.accepted_count + parents.c.rejected_count == parents.c.child_count)
        .where(parents.c.sweep_id == records.c.resource_id)
        .scalar_subquery()
    )
    child_expiry = (
        select(func.max(child_records.c.expires_at))
        .where(
            s.sweep_children.c.sweep_id == records.c.resource_id,
            child_records.c.resource_id == s.sweep_children.c.job_id,
            child_records.c.operation_id == "submitJob",
        )
        .scalar_subquery()
    )
    return session.execute(
        select(
            records.c.idempotency_id,
            finished.label("finished"),
            child_expiry.label("child_expiry"),
        )
        .where(
            records.c.state == "COMPLETED",
            records.c.operation_id == SWEEP_PARENT_OPERATION,
            records.c.expires_at < func.now(),
        )
        .order_by(records.c.expires_at, records.c.idempotency_id)
        .limit(limit)
        .with_for_update(of=records, skip_locked=True)
    ).all()
