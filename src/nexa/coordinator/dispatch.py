"""All effects of an accepted policy proposal commit together."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, insert, select, update

from nexa.coordinator.accounting import rebase_locked
from nexa.domain.scheduling import CreateReservation, Dispatch, InvalidateReservation
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7


def apply_decision(session, decision, *, worker, job, now, epoch, holder_id):
    if isinstance(decision, InvalidateReservation):
        session.execute(
            update(s.reservations)
            .where(
                s.reservations.c.reservation_id == UUID(decision.reservation_id),
                s.reservations.c.invalidated_at.is_(None),
            )
            .values(invalidated_at=now, invalidation_reason=decision.reason)
        )
        _event(session, job, now, holder_id, "RESERVATION_INVALIDATED", decision.reason)
        return
    if isinstance(decision, CreateReservation):
        session.execute(
            insert(s.reservations).values(
                reservation_id=new_uuid7(),
                tenant_id=job["tenant_id"],
                job_id=job["job_id"],
                eligibility_since=job["eligible_since"] or now,
                policy_version=session.execute(
                    select(s.policy_versions.c.policy_version).where(
                        s.policy_versions.c.is_current.is_(True)
                    )
                ).scalar_one(),
                created_at=now,
            )
        )
        _event(session, job, now, holder_id, "RESERVATION_CREATED", decision.reason)
        return
    if not isinstance(decision, Dispatch):
        return
    if (
        job["state"] != "QUEUED"
        or job["desired_state"] != "RUNNING"
        or job["version"] != decision.expected_job_version
    ):
        raise RuntimeError("stale dispatch proposal")
    if session.execute(
        select(s.attempt_authority_grants.c.grant_id).where(
            s.attempt_authority_grants.c.job_id == job["job_id"],
            s.attempt_authority_grants.c.ended_at.is_(None),
        )
    ).first():
        raise RuntimeError("job already has live authority")
    spec = (
        session.execute(select(s.job_specs).where(s.job_specs.c.job_id == job["job_id"]))
        .mappings()
        .one()
    )
    if spec["gpu_count"]:
        raise RuntimeError("B11 CPU dispatch cannot grant a GPU")
    attempt_id, allocation_id, lease_id = new_uuid7(), new_uuid7(), new_uuid7()
    fence = job["job_fence"] + 1
    identity = dict(
        tenant_id=job["tenant_id"],
        job_id=job["job_id"],
        attempt_id=attempt_id,
        worker_id=worker["worker_id"],
    )
    number = (
        session.execute(
            select(func.coalesce(func.max(s.attempts.c.attempt_number), 0)).where(
                s.attempts.c.job_id == job["job_id"]
            )
        ).scalar_one()
        + 1
    )
    session.execute(
        insert(s.attempts).values(
            **identity,
            attempt_number=number,
            state="CREATED",
            execution_intent="RUN",
            worker_incarnation_id=worker["current_incarnation_id"],
            job_fence=fence,
            startup_nonce=new_uuid7(),
            dispatch_coordinator_epoch=epoch,
            created_at=now,
            updated_at=now,
        )
    )
    session.execute(
        insert(s.allocations).values(
            **identity,
            allocation_id=allocation_id,
            cpu_millis=spec["cpu_millis"],
            memory_bytes=spec["memory_bytes"],
            gpu_count=0,
            state="HELD",
            held_at=now,
        )
    )
    session.execute(
        insert(s.attempt_leases).values(
            **identity,
            allocation_id=allocation_id,
            lease_id=lease_id,
            current_worker_incarnation_id=worker["current_incarnation_id"],
            job_fence=fence,
            issued_at=now,
            expires_at=now + timedelta(seconds=45),
        )
    )
    session.execute(
        insert(s.attempt_authority_grants).values(
            **identity,
            allocation_id=allocation_id,
            lease_id=lease_id,
            grant_id=new_uuid7(),
            worker_incarnation_id=worker["current_incarnation_id"],
            job_fence=fence,
            callback_id=new_uuid7(),
            granted_at=now,
        )
    )
    for scope_type, scope_id in [
        ("GLOBAL", "global"),
        ("TENANT", str(job["tenant_id"])),
        ("USER", f"{job['tenant_id']}:{job['submitter_user_id']}"),
    ]:
        result = session.execute(
            update(s.admission_counters)
            .where(
                s.admission_counters.c.scope_type == scope_type,
                s.admission_counters.c.scope_id == scope_id,
            )
            .values(
                active_attempts=s.admission_counters.c.active_attempts + 1,
                version=s.admission_counters.c.version + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError("missing admitted job counter")
    session.execute(
        update(s.jobs)
        .where(s.jobs.c.job_id == job["job_id"])
        .values(state="DISPATCHING", job_fence=fence, waiting_reason=None)
    )
    _event(session, job, now, holder_id, "JOB_DISPATCHING", None)
    limit = (
        session.execute(
            select(s.tenant_policies).where(
                s.tenant_policies.c.tenant_id == job["tenant_id"],
                s.tenant_policies.c.is_current.is_(True),
            )
        )
        .mappings()
        .one()
    )
    tenant_active = session.execute(
        select(s.admission_counters.c.active_attempts).where(
            s.admission_counters.c.scope_type == "TENANT",
            s.admission_counters.c.scope_id == str(job["tenant_id"]),
        )
    ).scalar_one()
    user_active = session.execute(
        select(s.admission_counters.c.active_attempts).where(
            s.admission_counters.c.scope_type == "USER",
            s.admission_counters.c.scope_id == f"{job['tenant_id']}:{job['submitter_user_id']}",
        )
    ).scalar_one()
    if tenant_active >= limit["tenant_active_limit"]:
        session.execute(
            update(s.jobs)
            .where(s.jobs.c.tenant_id == job["tenant_id"], s.jobs.c.state == "QUEUED")
            .values(eligible_since=None)
        )
    elif user_active >= limit["user_active_limit"]:
        session.execute(
            update(s.jobs)
            .where(
                s.jobs.c.tenant_id == job["tenant_id"],
                s.jobs.c.submitter_user_id == job["submitter_user_id"],
                s.jobs.c.state == "QUEUED",
            )
            .values(eligible_since=None)
        )
    if decision.reservation_id:
        session.execute(
            update(s.reservations)
            .where(
                s.reservations.c.reservation_id == UUID(decision.reservation_id),
                s.reservations.c.invalidated_at.is_(None),
            )
            .values(invalidated_at=now, invalidation_reason="dispatched")
        )
    rebase_locked(session, now)


def _event(session, job, now, holder_id, event_type, reason):
    if job is None:
        return
    session.execute(
        update(s.jobs)
        .where(s.jobs.c.job_id == job["job_id"])
        .values(
            version=job["version"] + 1, event_sequence=job["event_sequence"] + 1, updated_at=now
        )
    )
    session.execute(
        insert(s.events).values(
            event_id=new_uuid7(),
            tenant_id=job["tenant_id"],
            job_id=job["job_id"],
            sequence=job["event_sequence"] + 1,
            event_type=event_type,
            reason=reason,
            actor_type="COORDINATOR",
            actor_id=str(holder_id),
            created_at=now,
        )
    )
    session.execute(
        insert(s.audit_records).values(
            audit_id=new_uuid7(),
            actor_type="COORDINATOR",
            actor_id=str(holder_id),
            tenant_id=job["tenant_id"],
            action=event_type,
            target_type="JOB",
            target_id=str(job["job_id"]),
            before_version=job["version"],
            after_version=job["version"] + 1,
            reason=reason,
            created_at=now,
        )
    )
