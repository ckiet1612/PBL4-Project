"""Lease reaper: DB-time lease expiry revokes and fences authority, never releases capacity.

An unlocked index probe (`ix_attempt_leases_active_expiry`) finds due leases; each
is then handled in its own transaction under leadership -> policy -> job ->
session -> attempt -> lease -> allocation -> grant locks and re-checked, so a
renewal, cancel, failure or second reaper committed first leaves nothing to do.
Quarantined allocations stay charged until verified cleanup releases them.
"""

from sqlalchemy import func, select

from nexa.application.job_recovery import fence_attempt, revoke_leftover_authority
from nexa.coordinator.dispatch import _event
from nexa.infrastructure.persistence import schema as s

BATCH_SIZE = 16
_ACTIVE = ("DISPATCHING", "RUNNING", "PAUSING")


def expired_leases(session, limit=BATCH_SIZE) -> list:
    """Unlocked, index-backed probe for live leases past their DB-time expiry."""
    return list(
        session.execute(
            select(s.attempt_leases.c.lease_id)
            .where(
                s.attempt_leases.c.revoked_at.is_(None),
                s.attempt_leases.c.expires_at <= func.clock_timestamp(),
            )
            .order_by(s.attempt_leases.c.expires_at, s.attempt_leases.c.lease_id)
            .limit(limit)
        ).scalars()
    )


def reap_lease_locked(session, *, lease_id, holder_id, now_fn) -> bool:
    """Reap one lease; the caller holds leadership and the policy lock.

    `now_fn` re-reads DB time after the row locks, so expiry is judged at the
    moment this transaction owns the job.
    """
    ref = session.execute(
        select(s.attempt_leases.c.job_id, s.attempt_leases.c.attempt_id).where(
            s.attempt_leases.c.lease_id == lease_id
        )
    ).one_or_none()
    if ref is None:
        return False
    job_id, attempt_id = ref
    job = (
        session.execute(select(s.jobs).where(s.jobs.c.job_id == job_id).with_for_update())
        .mappings()
        .one()
    )
    session.execute(
        select(s.logical_sessions).where(s.logical_sessions.c.job_id == job_id).with_for_update()
    ).all()
    attempt = (
        session.execute(
            select(s.attempts).where(s.attempts.c.attempt_id == attempt_id).with_for_update()
        )
        .mappings()
        .one()
    )
    lease = (
        session.execute(
            select(s.attempt_leases)
            .where(s.attempt_leases.c.lease_id == lease_id)
            .with_for_update()
        )
        .mappings()
        .one()
    )
    session.execute(
        select(s.allocations)
        .where(s.allocations.c.allocation_id == lease["allocation_id"])
        .with_for_update()
    ).all()
    session.execute(
        select(s.attempt_authority_grants)
        .where(
            s.attempt_authority_grants.c.attempt_id == attempt["attempt_id"],
            s.attempt_authority_grants.c.ended_at.is_(None),
        )
        .with_for_update()
    ).all()
    now = now_fn()
    # CAS recheck: a renewal or revoking transaction committed first wins.
    if lease["revoked_at"] is not None or lease["expires_at"] > now:
        return False
    if job["state"] in _ACTIVE and attempt["job_fence"] == job["job_fence"]:
        fence_attempt(
            session,
            attempt_id=attempt["attempt_id"],
            now=now,
            state="LOST",
            failure_class="INFRASTRUCTURE",
            failure_reason="LEASE_EXPIRED",
            revoke_reason="LEASE_EXPIRED",
        )
        # Desired state is kept: a PAUSING job recovers toward PAUSED. One job UPDATE
        # (see job_recovery.extend_retention) keeps foreign-key checks off users.
        _event(
            session,
            job,
            now,
            holder_id,
            "ATTEMPT_LOST",
            "LEASE_EXPIRED",
            state="RECOVERING",
            job_fence=job["job_fence"] + 1,
        )
        return True
    # A terminal job (e.g. SUCCEEDED before cleanup) keeps its attempt state and fence;
    # only the leftover authority is revoked and the allocation quarantined.
    revoke_leftover_authority(
        session,
        attempt_id=attempt["attempt_id"],
        allocation_id=lease["allocation_id"],
        now=now,
        revoke_reason="LEASE_EXPIRED",
    )
    # No job state changes, so the version (ETag) is kept (L1).
    _event(session, job, now, holder_id, "LEASE_REVOKED", "LEASE_EXPIRED", keep_version=True)
    return True
