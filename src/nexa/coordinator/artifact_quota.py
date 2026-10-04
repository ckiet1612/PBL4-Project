"""Tenant artifact byte quota at dispatch (B19-R07).

The snapshot reads `artifact_storage_counters` and hands the policy one boolean per
tenant (`TenantPolicySnapshot.artifact_quota_available`); the policy stays pure and
treats every Job of an exhausted tenant as ineligible. This module only keeps the
derived `waiting_reason` visible: QUEUED Jobs of an exhausted tenant show
`waiting_for_quota`, and lose it once the tenant is back under quota.

Marking is a bounded, tenant-scoped UPDATE over the queued partial indexes with
`SKIP LOCKED`, so it never waits on a Job row a user or the tick holds. It writes no
event: the event-type set is closed and the reason is derived, not state. The Job
version is bumped because the representation (and so its ETag) changed.
"""

from sqlalchemy import and_, select, update

from nexa.infrastructure.persistence import schema as s

BATCH_SIZE = 256
REASON = "waiting_for_quota"


def exhausted_tenants(session, quota_bytes: int) -> set:
    counters = s.artifact_storage_counters
    return set(
        session.execute(
            select(counters.c.tenant_id).where(
                counters.c.committed_bytes + counters.c.reserved_bytes >= quota_bytes
            )
        ).scalars()
    )


def _queued(tenant_id):
    jobs = s.jobs
    # Same predicate as the queued partial indexes (ck_jobs_queued_dispatchable).
    return and_(jobs.c.tenant_id == tenant_id, jobs.c.state == "QUEUED")


def mark_batch(session, tenant_id, *, blocked: bool, now, limit: int = BATCH_SIZE) -> int:
    """Set (blocked) or clear the quota reason on at most `limit` QUEUED Jobs of a tenant."""
    jobs = s.jobs
    if blocked:
        pending = jobs.c.waiting_reason.is_distinct_from(REASON)
        value = REASON
    else:
        pending = jobs.c.waiting_reason == REASON
        value = None
    target = (
        select(jobs.c.job_id)
        .where(_queued(tenant_id), pending)
        .order_by(jobs.c.job_id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return session.execute(
        update(jobs)
        .where(jobs.c.job_id.in_(target))
        .values(waiting_reason=value, version=jobs.c.version + 1, updated_at=now)
    ).rowcount
