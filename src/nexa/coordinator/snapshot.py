"""Bounded per-tenant PostgreSQL windows mapped to the existing B04 policy."""

from dataclasses import replace
from uuid import UUID

from sqlalchemy import and_, func, or_, select, tuple_, update

from nexa.application.errors import ApplicationError
from nexa.application.job_service import JobService
from nexa.coordinator.accounting import epoch_ms
from nexa.domain.scheduling import (
    AllocationState,
    Candidate,
    CandidateWindow,
    Err,
    HeldAllocation,
    ReservationSnapshot,
    ResourceCapacity,
    ResourceRequest,
    SchedulingSnapshot,
    TenantLedger,
    TenantPolicySnapshot,
)
from nexa.infrastructure.persistence import schema as s
from nexa.scheduler.accounting import advance_accounting


def read_snapshot(session, now, policy, worker, inventory, cursors):
    limits = (
        session.execute(
            select(s.tenant_policies)
            .where(s.tenant_policies.c.is_current.is_(True))
            .order_by(s.tenant_policies.c.tenant_id)
        )
        .mappings()
        .all()
    )
    counters = {
        (row["scope_type"], row["scope_id"]): row
        for row in session.execute(select(s.admission_counters)).mappings()
    }
    ledgers = tuple(
        TenantLedger(
            str(row["tenant_id"]),
            row["virtual_score"],
            epoch_ms(row["accounted_through"]),
            row["had_eligible_demand"],
        )
        for row in session.execute(select(s.fairness_ledgers)).mappings()
    )
    held_rows = (
        session.execute(select(s.allocations).where(s.allocations.c.state != "RELEASED"))
        .mappings()
        .all()
    )
    claims = {}
    for row in session.execute(
        select(s.allocation_gpu_claims).where(s.allocation_gpu_claims.c.released_at.is_(None))
    ).mappings():
        claims.setdefault(row["allocation_id"], []).append(row["gpu_uuid"])
    held = tuple(
        HeldAllocation(
            str(row["allocation_id"]),
            str(row["tenant_id"]),
            ResourceRequest(row["cpu_millis"], row["memory_bytes"], row["gpu_count"]),
            tuple(sorted(claims.get(row["allocation_id"], []))),
            AllocationState.HELD if row["state"] == "HELD" else AllocationState.QUARANTINED,
        )
        for row in held_rows
    )
    active = (
        session.execute(select(s.reservations).where(s.reservations.c.invalidated_at.is_(None)))
        .mappings()
        .one_or_none()
    )
    compatible = []
    for template in session.execute(
        select(s.template_versions)
        .join(s.templates, s.templates.c.template_id == s.template_versions.c.template_id)
        .where(s.templates.c.enabled.is_(True))
    ).mappings():
        try:
            requirements = JobService._inventory_requirements(dict(template))
            # B11 intentionally implements the CPU adapter only.
            if template["adapter_id"] == "cpu.iterative" and JobService._inventory_supports(
                dict(inventory), requirements
            ):
                compatible.append((template["template_id"], template["version"]))
        except ApplicationError:
            continue
    capacity = ResourceCapacity(
        inventory["allocatable_cpu_millis"],
        inventory["allocatable_memory_bytes"],
        inventory["allocatable_gpu_count"],
    )
    windows = []
    for limit in limits:
        tenant_id = limit["tenant_id"]
        tenant_active = counters.get(("TENANT", str(tenant_id)), {}).get("active_attempts", 0)
        owned = [row for row in held_rows if row["tenant_id"] == tenant_id]
        free_quota = [
            limit[key] - sum(row[field] for row in owned)
            for key, field in [
                ("cpu_limit_millis", "cpu_millis"),
                ("memory_limit_bytes", "memory_bytes"),
                ("gpu_limit", "gpu_count"),
            ]
        ]
        user_count = (
            select(s.admission_counters.c.active_attempts)
            .where(
                s.admission_counters.c.scope_type == "USER",
                s.admission_counters.c.scope_id
                == func.concat(s.jobs.c.tenant_id, ":", s.jobs.c.submitter_user_id),
            )
            .scalar_subquery()
        )
        query = (
            select(
                s.jobs,
                s.job_specs.c.cpu_millis,
                s.job_specs.c.memory_bytes,
                s.job_specs.c.gpu_count,
                s.job_specs.c.template_version,
            )
            .join(s.job_specs, s.jobs.c.job_id == s.job_specs.c.job_id)
            .where(
                s.jobs.c.tenant_id == tenant_id,
                s.jobs.c.state == "QUEUED",
                s.jobs.c.desired_state == "RUNNING",
                s.jobs.c.recovery_intent.is_(None),
                or_(s.jobs.c.retry_ready_at.is_(None), s.jobs.c.retry_ready_at <= now),
                tuple_(s.job_specs.c.template_id, s.job_specs.c.template_version).in_(compatible),
                s.job_specs.c.gpu_count == 0,
                s.job_specs.c.cpu_millis <= min(capacity.cpu_millis, free_quota[0]),
                s.job_specs.c.memory_bytes <= min(capacity.memory_bytes, free_quota[1]),
                func.coalesce(user_count, 0) < limit["user_active_limit"],
            )
        )
        if tenant_active >= limit["tenant_active_limit"]:
            query = query.where(False)
        enabled = session.execute(
            select(s.tenants.c.enabled).where(s.tenants.c.tenant_id == tenant_id)
        ).scalar_one()
        if not enabled:
            query = query.where(False)

        # Eligibility age is a contiguous interval. Concurrency, policy quota
        # and capability blocks reset it; waiting only for currently occupied
        # capacity leaves it running. These updates do not change Job version.
        eligible_ids = query.with_only_columns(s.jobs.c.job_id).subquery()
        eligible = select(eligible_ids.c.job_id)
        queued = (s.jobs.c.tenant_id == tenant_id) & (s.jobs.c.state == "QUEUED")
        session.execute(
            update(s.jobs)
            .where(queued, s.jobs.c.eligible_since.is_not(None), s.jobs.c.job_id.not_in(eligible))
            .values(eligible_since=None)
        )
        session.execute(
            update(s.jobs)
            .where(queued, s.jobs.c.eligible_since.is_(None), s.jobs.c.job_id.in_(eligible))
            .values(eligible_since=now)
        )

        def candidate(row, tenant_id=tenant_id, tenant_active=tenant_active):
            return Candidate(
                str(row["job_id"]),
                str(tenant_id),
                str(row["submitter_user_id"]),
                row["version"],
                row["ready_sequence"],
                ResourceRequest(row["cpu_millis"], row["memory_bytes"], row["gpu_count"]),
                row["base_priority"],
                max(0, (epoch_ms(now) - epoch_ms(row["eligible_since"] or now)) // 1000),
                epoch_ms(row["retry_ready_at"]) if row["retry_ready_at"] else 0,
                row["template_version"],
                frozenset(),
                tenant_active,
                counters.get(("USER", f"{tenant_id}:{row['submitter_user_id']}"), {}).get(
                    "active_attempts", 0
                ),
            )

        priority = func.least(
            2,
            s.jobs.c.base_priority
            + func.floor(func.extract("epoch", now - s.jobs.c.eligible_since) / 60),
        )
        ordering = (priority.desc(), s.jobs.c.ready_sequence, s.jobs.c.job_id)
        cursor = cursors.get(str(tenant_id))
        normal_query = query
        if cursor:
            normal_query = query.where(
                or_(
                    priority < cursor[0],
                    and_(
                        priority == cursor[0],
                        tuple_(s.jobs.c.ready_sequence, s.jobs.c.job_id)
                        > tuple_(cursor[1], UUID(cursor[2])),
                    ),
                )
            )
        rows = session.execute(normal_query.order_by(*ordering).limit(16)).mappings().all()
        if not rows and cursor:
            rows = session.execute(query.order_by(*ordering).limit(16)).mappings().all()
        oldest = (
            session.execute(
                query.order_by(
                    s.jobs.c.eligible_since.asc().nulls_last(),
                    s.jobs.c.ready_sequence,
                    s.jobs.c.job_id,
                ).limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if active and active["tenant_id"] == tenant_id:
            reserved = (
                session.execute(query.where(s.jobs.c.job_id == active["job_id"]))
                .mappings()
                .one_or_none()
            )
            if reserved:
                oldest = reserved  # Preserve the active protected candidate outside the window.
        next_cursor = None
        if rows:
            last = rows[-1]
            age_minutes = max(0, (epoch_ms(now) - epoch_ms(last["eligible_since"])) // 60000)
            last_priority = min(2, last["base_priority"] + age_minutes)
            next_cursor = f"{last_priority}:{last['ready_sequence']}:{last['job_id']}".encode()
        windows.append(
            (
                str(tenant_id),
                CandidateWindow(
                    tuple(candidate(row) for row in rows),
                    candidate(oldest) if oldest else None,
                    next_cursor,
                ),
            )
        )
    floor = session.execute(select(s.fairness_state.c.virtual_floor)).scalar_one()
    snapshot = SchedulingSnapshot(
        policy["policy_version"],
        capacity,
        held,
        ledgers,
        tuple(
            TenantPolicySnapshot(
                str(row["tenant_id"]),
                row["weight"],
                ResourceCapacity(
                    row["cpu_limit_millis"], row["memory_limit_bytes"], row["gpu_limit"]
                ),
                row["tenant_active_limit"],
                row["user_active_limit"],
            )
            for row in limits
        ),
        tuple(windows),
        ReservationSnapshot(
            str(active["reservation_id"]),
            str(active["job_id"]),
            str(active["tenant_id"]),
            active["policy_version"],
        )
        if active
        else None,
        floor,
    )
    accounted = advance_accounting(snapshot, epoch_ms(now))
    if isinstance(accounted, Err):
        raise RuntimeError(f"invalid scheduler snapshot: {accounted.error}")
    for ledger in accounted.value.tenant_ledgers:
        session.execute(
            update(s.fairness_ledgers)
            .where(s.fairness_ledgers.c.tenant_id == UUID(ledger.tenant_id))
            .values(
                virtual_score=ledger.virtual_score, had_eligible_demand=ledger.had_eligible_demand
            )
        )
    session.execute(
        update(s.fairness_state).values(
            virtual_floor=accounted.value.virtual_floor,
            version=s.fairness_state.c.version + 1,
            updated_at=now,
        )
    )
    return replace(
        snapshot,
        tenant_ledgers=accounted.value.tenant_ledgers,
        virtual_floor=accounted.value.virtual_floor,
    )
