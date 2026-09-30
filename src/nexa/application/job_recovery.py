"""Shared B15 linearization steps for control, reaper and worker disable; no Docker access."""

from datetime import timedelta

from sqlalchemy import func, or_, select, update

from nexa.infrastructure.persistence import schema as s


def extend_retention(session, job_id, terminal_at, retention_days):
    """Keep the job's idempotency records at least `retention_days` past its terminal time.

    Callers stamp `terminal_at` in their single job UPDATE: a second UPDATE of the
    same row re-runs its foreign-key checks, taking KEY SHARE on the submitter's
    users row (B15-R04), which request transactions lock before policy/job locks.
    """
    session.execute(
        update(s.idempotency_records)
        .where(s.idempotency_records.c.resource_id == job_id)
        .values(
            expires_at=func.greatest(
                s.idempotency_records.c.expires_at, terminal_at + timedelta(days=retention_days)
            )
        )
    )


def fence_attempt(session, *, attempt_id, now, state, failure_class, failure_reason, revoke_reason):
    """End the live authority of one attempt; the caller holds every lock and bumps the fence.

    The allocation stays charged as QUARANTINED until a verified cleanup releases it.
    """
    session.execute(
        update(s.attempts)
        .where(s.attempts.c.attempt_id == attempt_id)
        .values(
            state=state,
            failure_class=failure_class,
            failure_reason=failure_reason,
            updated_at=now,
        )
    )
    session.execute(
        update(s.attempt_leases)
        .where(s.attempt_leases.c.attempt_id == attempt_id, s.attempt_leases.c.revoked_at.is_(None))
        .values(revoked_at=now, revoke_reason=revoke_reason)
    )
    session.execute(
        update(s.attempt_authority_grants)
        .where(
            s.attempt_authority_grants.c.attempt_id == attempt_id,
            s.attempt_authority_grants.c.ended_at.is_(None),
        )
        .values(ended_at=now)
    )
    session.execute(
        update(s.allocations)
        .where(s.allocations.c.attempt_id == attempt_id, s.allocations.c.state == "HELD")
        .values(state="QUARANTINED", quarantined_at=now)
    )
    session.execute(
        update(s.result_reservations)
        .where(
            s.result_reservations.c.attempt_id == attempt_id,
            s.result_reservations.c.state == "ACTIVE",
        )
        .values(state="ABANDONED")
    )
    session.execute(
        update(s.checkpoint_reservations)
        .where(
            s.checkpoint_reservations.c.attempt_id == attempt_id,
            s.checkpoint_reservations.c.state == "RESERVED",
        )
        .values(state="ABANDONED", ended_at=now)
    )


def revoke_leftover_authority(session, *, attempt_id, allocation_id, now, revoke_reason):
    """Revoke a live lease whose job no longer needs it, e.g. SUCCEEDED before cleanup.

    The attempt keeps its state and the job its fence; the allocation is quarantined.
    """
    session.execute(
        update(s.attempt_leases)
        .where(s.attempt_leases.c.attempt_id == attempt_id, s.attempt_leases.c.revoked_at.is_(None))
        .values(revoked_at=now, revoke_reason=revoke_reason)
    )
    session.execute(
        update(s.attempt_authority_grants)
        .where(
            s.attempt_authority_grants.c.attempt_id == attempt_id,
            s.attempt_authority_grants.c.ended_at.is_(None),
        )
        .values(ended_at=now)
    )
    session.execute(
        update(s.allocations)
        .where(s.allocations.c.allocation_id == allocation_id, s.allocations.c.state == "HELD")
        .values(state="QUARANTINED", quarantined_at=now)
    )
    session.execute(
        update(s.checkpoint_reservations)
        .where(
            s.checkpoint_reservations.c.attempt_id == attempt_id,
            s.checkpoint_reservations.c.state == "RESERVED",
        )
        .values(state="ABANDONED", ended_at=now)
    )
    # L2: as in fence_attempt, the revoked authority cannot publish a result.
    session.execute(
        update(s.result_reservations)
        .where(
            s.result_reservations.c.attempt_id == attempt_id,
            s.result_reservations.c.state == "ACTIVE",
        )
        .values(state="ABANDONED")
    )


def restorable_scope(job_id):
    """Checkpoints a new Attempt of `job_id` may restore: its own and manual-retry references.

    A CheckpointReference never changes the source checkpoint's ownership or blob metadata.
    """
    return or_(
        s.checkpoints.c.job_id == job_id,
        s.checkpoints.c.checkpoint_id.in_(
            select(s.checkpoint_references.c.source_checkpoint_id).where(
                s.checkpoint_references.c.target_job_id == job_id
            )
        ),
    )


def retry_lineage(session, tenant_id, job_id):
    """`job_id` and every Job it was manually retried from, in the tenant (B15-R05).

    The manual-retry chain `retry_of_job_id` is the only way a Job may inherit another
    Job's checkpoint; UNION stops the walk on a (never written) cycle.
    """
    chain = (
        select(s.jobs.c.job_id, s.jobs.c.retry_of_job_id)
        .where(s.jobs.c.tenant_id == tenant_id, s.jobs.c.job_id == job_id)
        .cte("retry_chain", recursive=True)
    )
    chain = chain.union(
        select(s.jobs.c.job_id, s.jobs.c.retry_of_job_id).where(
            s.jobs.c.tenant_id == tenant_id, s.jobs.c.job_id == chain.c.retry_of_job_id
        )
    )
    return set(session.execute(select(chain.c.job_id)).scalars())


def restore_order(job_id):
    """Own checkpoints first, then newest sequence; the id breaks ties across source jobs."""
    return (
        s.checkpoints.c.job_id != job_id,
        s.checkpoints.c.sequence.desc(),
        s.checkpoints.c.checkpoint_id.desc(),
    )
