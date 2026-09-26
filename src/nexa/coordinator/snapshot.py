"""Bounded per-tenant PostgreSQL windows mapped to the existing B04 policy."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    and_,
    any_,
    bindparam,
    case,
    cast,
    column,
    exists,
    false,
    func,
    literal,
    or_,
    select,
    true,
    tuple_,
    union_all,
    update,
    values,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from nexa.application.errors import ApplicationError
from nexa.application.job_service import JobService
from nexa.coordinator.accounting import epoch_ms
from nexa.coordinator.eligibility import pending_eligibility_tenants, process_eligibility_batch
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


def _clipped_age_exists(tenant_id, users, priority, resume_at):
    """Index probe: does any queued Job at this priority age from ``resume_at``?"""
    probe = s.jobs.alias("clipped_age_probe")
    return exists(
        select(probe.c.job_id)
        .where(
            probe.c.tenant_id == tenant_id,
            probe.c.submitter_user_id.in_(users),
            probe.c.base_priority == priority,
            probe.c.state == "QUEUED",
            probe.c.desired_state == "RUNNING",
            probe.c.recovery_intent.is_(None),
            probe.c.eligible_since <= resume_at,
        )
        .limit(1)
    )


def _window_rows(session, query, now, cursor, priorities, resume_at=None, tenant_id=None, users=()):
    """Merge bounded normal streams and the independent oldest candidate."""
    minute = now - timedelta(seconds=60)
    two_minutes = now - timedelta(seconds=120)
    effective_since = query.selected_columns.effective_eligible_since

    def fetch(after):
        branches = []
        for effective in (2, 1, 0):
            if after and effective > cursor[0]:
                continue
            for base in range(effective, -1, -1):
                if base not in priorities:
                    continue
                branch = query.where(
                    s.jobs.c.base_priority == base,
                    effective_since.is_not(None),
                )
                if effective == 2:
                    if base == 1:
                        if resume_at is not None and resume_at > minute:
                            continue
                        branch = branch.where(
                            (s.jobs.c.eligible_since if resume_at else effective_since) <= minute
                        )
                    elif base == 0:
                        if resume_at is not None and resume_at > two_minutes:
                            continue
                        branch = branch.where(
                            (s.jobs.c.eligible_since if resume_at else effective_since)
                            <= two_minutes
                        )
                elif effective == 1:
                    if base == 1:
                        if resume_at is None or resume_at <= minute:
                            branch = branch.where(
                                (s.jobs.c.eligible_since if resume_at else effective_since) > minute
                            )
                    else:
                        if resume_at is not None and resume_at > minute:
                            continue
                        branch = branch.where(
                            (s.jobs.c.eligible_since if resume_at else effective_since) <= minute
                        )
                        if resume_at is None or resume_at <= two_minutes:
                            branch = branch.where(
                                (s.jobs.c.eligible_since if resume_at else effective_since)
                                > two_minutes
                            )
                else:
                    if resume_at is None or resume_at <= minute:
                        branch = branch.where(
                            (s.jobs.c.eligible_since if resume_at else effective_since) > minute
                        )
                if after and effective == cursor[0]:
                    branch = branch.where(
                        tuple_(s.jobs.c.ready_sequence, s.jobs.c.job_id)
                        > tuple_(cursor[1], UUID(cursor[2]))
                    )
                branches.append(
                    branch.add_columns(
                        literal(effective).label("effective_priority"),
                        literal(0).label("is_oldest"),
                    )
                    .order_by(s.jobs.c.ready_sequence, s.jobs.c.job_id)
                    .limit(16)
                    .subquery()
                )
        oldest_query = query.add_columns(
            literal(-1).label("effective_priority"), literal(1).label("is_oldest")
        )
        if resume_at is None:
            oldest_branch = (
                oldest_query.order_by(
                    effective_since.asc().nulls_last(),
                    s.jobs.c.ready_sequence,
                    s.jobs.c.job_id,
                )
                .limit(1)
                .subquery()
            )
        else:
            # Without the probe, a tenant whose Jobs all age after resume_at
            # walks its whole priority segment in ready order to find none.
            old_streams = [
                oldest_query.where(
                    s.jobs.c.base_priority == priority,
                    s.jobs.c.eligible_since <= resume_at,
                    _clipped_age_exists(tenant_id, users, priority, resume_at),
                )
                .order_by(s.jobs.c.ready_sequence, s.jobs.c.job_id)
                .limit(1)
                .subquery()
                for priority in (0, 1, 2)
            ]
            old_streams.append(
                oldest_query.where(s.jobs.c.eligible_since > resume_at)
                .order_by(s.jobs.c.eligible_since, s.jobs.c.ready_sequence, s.jobs.c.job_id)
                .limit(1)
                .subquery()
            )
            oldest_candidates = union_all(*(select(stream) for stream in old_streams)).subquery()
            oldest_branch = (
                select(oldest_candidates)
                .order_by(
                    oldest_candidates.c.effective_eligible_since,
                    oldest_candidates.c.ready_sequence,
                    oldest_candidates.c.job_id,
                )
                .limit(1)
                .subquery()
            )
        if branches:
            normal_streams = union_all(*(select(branch) for branch in branches)).subquery()
            normal = (
                select(normal_streams)
                .order_by(
                    normal_streams.c.effective_priority.desc(),
                    normal_streams.c.ready_sequence,
                    normal_streams.c.job_id,
                )
                .limit(16)
                .subquery()
            )
            streams = union_all(select(normal), select(oldest_branch)).subquery()
        else:
            streams = oldest_branch
        result = (
            session.execute(
                select(streams).order_by(
                    streams.c.is_oldest,
                    streams.c.effective_priority.desc(),
                    streams.c.ready_sequence,
                    streams.c.job_id,
                )
            )
            .mappings()
            .all()
        )
        return [row for row in result if not row["is_oldest"]], next(
            (row for row in result if row["is_oldest"]), None
        )

    rows, oldest = fetch(bool(cursor))
    return (rows, oldest) if rows or not cursor else fetch(False)


def _batched_window_rows(session, descriptors, now, compatible, *, resumed=False, grouped=False):
    """Read no-cursor tenants in one indexed LATERAL statement."""
    if not descriptors or not compatible:
        return {}
    minute = now - timedelta(seconds=60)
    two_minutes = now - timedelta(seconds=120)
    columns = [
        column("tenant_id", PG_UUID(as_uuid=True)),
        column("min_cpu", BigInteger()),
        column("max_cpu", BigInteger()),
        column("min_memory", BigInteger()),
        column("max_memory", BigInteger()),
        column("users", ARRAY(PG_UUID(as_uuid=True))),
    ]
    if resumed:
        columns.append(column("resume_at", DateTime(timezone=True)))
    if grouped:
        columns.extend(
            (
                column("group_user_id", PG_UUID(as_uuid=True)),
                column("group_index", Integer()),
                column("cursor_priority", Integer()),
                column("cursor_sequence", BigInteger()),
                column("cursor_job_id", PG_UUID(as_uuid=True)),
            )
        )
    params = values(*columns, name="queue_parameters").data(descriptors).alias("queue_parameters")
    effective_age = (
        func.greatest(s.jobs.c.eligible_since, params.c.resume_at)
        if resumed
        else s.jobs.c.eligible_since
    )
    age_probe = s.jobs.alias("age_probe")

    def age_probe_where(priority):
        return (
            age_probe.c.tenant_id == params.c.tenant_id,
            (
                age_probe.c.submitter_user_id == params.c.group_user_id
                if grouped
                else age_probe.c.submitter_user_id == any_(params.c.users)
            ),
            age_probe.c.base_priority == priority,
            age_probe.c.state == "QUEUED",
            age_probe.c.desired_state == "RUNNING",
            age_probe.c.recovery_intent.is_(None),
        )

    def has_age(priority, cutoff):
        return exists(
            select(age_probe.c.job_id)
            .where(*age_probe_where(priority), age_probe.c.eligible_since > cutoff)
            .limit(1)
            .correlate(params)
        )

    def oldest_age(priority):
        # Ordered LIMIT 1 walks the age index at any table size; an EXISTS on
        # ``eligible_since <= x`` may be costed as a short seq scan instead.
        return (
            select(age_probe.c.eligible_since)
            .where(*age_probe_where(priority))
            .order_by(age_probe.c.eligible_since)
            .limit(1)
            .correlate(params)
            .scalar_subquery()
        )

    def base():
        return (
            select(
                s.jobs.c.job_id,
                s.jobs.c.tenant_id,
                s.jobs.c.submitter_user_id,
                s.jobs.c.version,
                s.jobs.c.ready_sequence,
                s.jobs.c.eligible_since,
                s.jobs.c.retry_ready_at,
                s.jobs.c.base_priority,
                s.job_specs.c.cpu_millis,
                s.job_specs.c.memory_bytes,
                s.job_specs.c.gpu_count,
                s.job_specs.c.template_version,
                effective_age.label("effective_eligible_since"),
            )
            .join(s.job_specs, s.jobs.c.job_id == s.job_specs.c.job_id)
            .where(
                s.jobs.c.tenant_id == params.c.tenant_id,
                s.jobs.c.state == "QUEUED",
                s.jobs.c.desired_state == "RUNNING",
                s.jobs.c.recovery_intent.is_(None),
                s.jobs.c.eligible_since.is_not(None),
                or_(s.jobs.c.retry_ready_at.is_(None), s.jobs.c.retry_ready_at <= now),
                tuple_(s.job_specs.c.template_id, s.job_specs.c.template_version).in_(compatible),
                s.job_specs.c.gpu_count == 0,
                s.job_specs.c.cpu_millis <= params.c.max_cpu,
                s.job_specs.c.memory_bytes <= params.c.max_memory,
                # One opaque predicate for the quota-cell floor: two more range
                # comparisons against VALUES columns cut the row estimate ~9x and
                # flip the 100k plan from the ordered index walk to tenant scans.
                case(
                    (
                        and_(
                            s.job_specs.c.cpu_millis > params.c.min_cpu,
                            s.job_specs.c.memory_bytes > params.c.min_memory,
                        ),
                        true(),
                    ),
                    else_=false(),
                ),
                (
                    s.jobs.c.submitter_user_id == params.c.group_user_id
                    if grouped
                    else s.jobs.c.submitter_user_id == any_(params.c.users)
                ),
            )
            .correlate(params)
        )

    branches = []
    for effective in (2, 1, 0):
        for priority in range(effective, -1, -1):
            branch = base().where(
                s.jobs.c.base_priority == priority,
                s.jobs.c.eligible_since.is_not(None),
            )
            if grouped:
                cursor_priority = cast(params.c.cursor_priority, Integer)
                branch = branch.where(
                    or_(
                        params.c.cursor_priority.is_(None),
                        literal(effective) < cursor_priority,
                        and_(
                            literal(effective) == cursor_priority,
                            tuple_(s.jobs.c.ready_sequence, s.jobs.c.job_id)
                            > tuple_(
                                cast(params.c.cursor_sequence, BigInteger),
                                cast(params.c.cursor_job_id, PG_UUID(as_uuid=True)),
                            ),
                        ),
                    )
                )
            if effective == 2:
                if priority == 1:
                    branch = branch.where(s.jobs.c.eligible_since <= minute)
                    if resumed:
                        branch = branch.where(params.c.resume_at <= minute)
                elif priority == 0:
                    branch = branch.where(s.jobs.c.eligible_since <= two_minutes)
                    if resumed:
                        branch = branch.where(params.c.resume_at <= two_minutes)
            elif effective == 1:
                if priority == 1:
                    age_condition = s.jobs.c.eligible_since > minute
                    branch = branch.where(
                        or_(params.c.resume_at > minute, age_condition)
                        if resumed
                        else age_condition
                    )
                    branch = branch.where(
                        or_(params.c.resume_at > minute, has_age(priority, minute))
                        if resumed
                        else has_age(priority, minute)
                    )
                else:
                    branch = branch.where(s.jobs.c.eligible_since <= minute)
                    if resumed:
                        branch = branch.where(
                            params.c.resume_at <= minute,
                            or_(
                                params.c.resume_at > two_minutes,
                                s.jobs.c.eligible_since > two_minutes,
                            ),
                            or_(
                                params.c.resume_at > two_minutes,
                                has_age(priority, two_minutes),
                            ),
                        )
                    else:
                        branch = branch.where(
                            s.jobs.c.eligible_since > two_minutes,
                            has_age(priority, two_minutes),
                        )
            else:
                age_condition = s.jobs.c.eligible_since > minute
                branch = branch.where(
                    or_(params.c.resume_at > minute, age_condition) if resumed else age_condition
                )
                branch = branch.where(
                    or_(params.c.resume_at > minute, has_age(priority, minute))
                    if resumed
                    else has_age(priority, minute)
                )
            branches.append(
                branch.add_columns(
                    literal(effective).label("effective_priority"),
                    literal(0).label("is_oldest"),
                )
                .order_by(s.jobs.c.ready_sequence, s.jobs.c.job_id)
                .limit(16)
                .subquery()
            )
    normal_streams = union_all(*(select(branch) for branch in branches)).subquery()
    normal = (
        select(normal_streams)
        .order_by(
            normal_streams.c.effective_priority.desc(),
            normal_streams.c.ready_sequence,
            normal_streams.c.job_id,
        )
        .limit(16)
        .subquery()
    )
    oldest_query = base().add_columns(
        literal(-1).label("effective_priority"), literal(1).label("is_oldest")
    )
    if resumed:
        old_streams = [
            oldest_query.where(
                s.jobs.c.base_priority == priority,
                s.jobs.c.eligible_since <= params.c.resume_at,
                # Without the probe, a tenant whose Jobs all age after
                # resume_at walks its whole priority segment to find none.
                oldest_age(priority) <= params.c.resume_at,
            )
            .order_by(s.jobs.c.ready_sequence, s.jobs.c.job_id)
            .limit(1)
            .subquery()
            for priority in (0, 1, 2)
        ]
        old_streams.append(
            oldest_query.where(s.jobs.c.eligible_since > params.c.resume_at)
            .order_by(s.jobs.c.eligible_since, s.jobs.c.ready_sequence, s.jobs.c.job_id)
            .limit(1)
            .subquery()
        )
        oldest_candidates = union_all(*(select(stream) for stream in old_streams)).subquery()
        oldest = (
            select(oldest_candidates)
            .order_by(
                oldest_candidates.c.effective_eligible_since,
                oldest_candidates.c.ready_sequence,
                oldest_candidates.c.job_id,
            )
            .limit(1)
            .subquery()
        )
    else:
        oldest = (
            oldest_query.order_by(
                s.jobs.c.eligible_since.asc().nulls_last(),
                s.jobs.c.ready_sequence,
                s.jobs.c.job_id,
            )
            .limit(1)
            .subquery()
        )
    selected = union_all(select(normal), select(oldest)).lateral("selected_queue")
    key_columns = [params.c.tenant_id.label("batch_tenant_id")]
    if grouped:
        key_columns.append(params.c.group_index.label("batch_group"))
    statement = select(*key_columns, selected).select_from(params.join(selected, true()))
    windows = {(row[0], row[-4]) if grouped else row[0]: ([], None) for row in descriptors}
    for row in session.execute(statement).mappings():
        data = dict(row)
        tenant_id = data.pop("batch_tenant_id")
        key = (tenant_id, data.pop("batch_group")) if grouped else tenant_id
        normal_rows, _ = windows[key]
        if data["is_oldest"]:
            windows[key] = (normal_rows, data)
        else:
            normal_rows.append(data)
    return windows


def _quota_intervals(steps, limit):
    """Merge headroom steps into (lower, upper, resumed_at) size intervals up to limit."""
    intervals = []
    lower = -1
    for upper, resumed_at in steps:
        if lower >= limit:
            break
        upper = min(upper, limit)
        if intervals and intervals[-1][2] == resumed_at:
            intervals[-1] = (intervals[-1][0], upper, resumed_at)
        else:
            intervals.append((lower, upper, resumed_at))
        lower = upper
    return intervals


def _quota_cells(steps, max_cpu, max_memory):
    """Split fitting request sizes into rectangles that share one quota resume time."""
    return [
        (
            cpu_lower,
            cpu_upper,
            memory_lower,
            memory_upper,
            max((value for value in (cpu_at, memory_at) if value), default=None),
        )
        for cpu_lower, cpu_upper, cpu_at in _quota_intervals(steps.get("cpu", ()), max_cpu)
        for memory_lower, memory_upper, memory_at in _quota_intervals(
            steps.get("memory", ()), max_memory
        )
    ]


def _populated_cells(session, cells_by_tenant):
    """Keep the cells holding at least one queued request size, in one statement."""
    rows = [
        (tenant_id, index, *cell[:4])
        for tenant_id, cells in cells_by_tenant.items()
        for index, cell in enumerate(cells)
    ]
    if not rows:
        return {}
    params = (
        values(
            column("tenant_id", PG_UUID(as_uuid=True)),
            column("cell_index", Integer()),
            column("min_cpu", BigInteger()),
            column("max_cpu", BigInteger()),
            column("min_memory", BigInteger()),
            column("max_memory", BigInteger()),
            name="quota_cells",
        )
        .data(rows)
        .alias("quota_cells")
    )
    sizes = s.queue_request_sizes
    statement = (
        select(params.c.tenant_id, params.c.cell_index)
        .where(
            exists(
                select(sizes.c.tenant_id)
                .where(
                    sizes.c.tenant_id == params.c.tenant_id,
                    sizes.c.cpu_millis > params.c.min_cpu,
                    sizes.c.cpu_millis <= params.c.max_cpu,
                    sizes.c.memory_bytes > params.c.min_memory,
                    sizes.c.memory_bytes <= params.c.max_memory,
                )
                .correlate(params)
            )
        )
        .order_by(params.c.tenant_id, params.c.cell_index)
    )
    populated = {}
    for row in session.execute(statement):
        populated.setdefault(row.tenant_id, []).append(
            cells_by_tenant[row.tenant_id][row.cell_index]
        )
    return populated


def read_snapshot(session, now, policy, worker, inventory, cursors, *, replay_eligibility=True):
    pending_tenants = (
        process_eligibility_batch(session, now)
        if replay_eligibility
        else pending_eligibility_tenants(session)
    )
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
    tenant_enabled = {
        row["tenant_id"]: row["enabled"]
        for row in session.execute(select(s.tenants.c.tenant_id, s.tenants.c.enabled)).mappings()
    }
    # Unlocked read: the heartbeat may already have charged past `now`. The
    # proposal keeps that charge and is revalidated by the commit transaction.
    # A tenant without a row yet gets the defaults lock_ledgers will insert.
    ledger_by_tenant = {
        str(row["tenant_id"]): TenantLedger(
            str(row["tenant_id"]),
            row["virtual_score"],
            min(epoch_ms(row["accounted_through"]), epoch_ms(now)),
            row["had_eligible_demand"],
        )
        for row in session.execute(select(s.fairness_ledgers)).mappings()
    }
    for row in limits:
        ledger_by_tenant.setdefault(
            str(row["tenant_id"]),
            TenantLedger(str(row["tenant_id"]), Decimal(0), epoch_ms(now), False),
        )
    ledgers = tuple(ledger_by_tenant.values())
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
    head_priorities = {}
    for head in session.execute(select(s.queue_heads.c.tenant_id, s.queue_heads.c.priority)):
        head_priorities.setdefault(head.tenant_id, set()).add(head.priority)
    queued_submitters = {}
    for row in session.execute(
        select(s.queue_submitters.c.tenant_id, s.queue_submitters.c.user_id)
    ):
        queued_submitters.setdefault(row.tenant_id, []).append(row.user_id)
    # Held allocations move only the per-tenant headroom staircase; a request
    # size's age restarts at the step boundary that last admitted it.
    quota_steps = {}
    for row in session.execute(
        select(s.quota_headroom_steps).order_by(
            s.quota_headroom_steps.c.tenant_id,
            s.quota_headroom_steps.c.resource,
            s.quota_headroom_steps.c.upper_bound,
        )
    ).mappings():
        quota_steps.setdefault(row["tenant_id"], {}).setdefault(row["resource"], []).append(
            (row["upper_bound"], row["resumed_at"])
        )
    fit_limits = {}
    for limit in limits:
        owned = [row for row in held_rows if row["tenant_id"] == limit["tenant_id"]]
        fit_limits[limit["tenant_id"]] = (
            min(
                capacity.cpu_millis,
                limit["cpu_limit_millis"] - sum(row["cpu_millis"] for row in owned),
            ),
            min(
                capacity.memory_bytes,
                limit["memory_limit_bytes"] - sum(row["memory_bytes"] for row in owned),
            ),
        )
    tenant_cells = _populated_cells(
        session,
        {
            tenant_id: _quota_cells(quota_steps.get(tenant_id, {}), max_cpu, max_memory)
            for tenant_id, (max_cpu, max_memory) in fit_limits.items()
        },
    )
    tenant_resumes = {}
    prepared = []
    for limit in limits:
        tenant_id = limit["tenant_id"]
        tenant_active = counters.get(("TENANT", str(tenant_id)), {}).get("active_attempts", 0)
        cells = tenant_cells.get(tenant_id, [])
        bounds = (-1, fit_limits[tenant_id][0], -1, fit_limits[tenant_id][1])
        quota_resume = None
        if len(cells) == 1:
            *bounds, quota_resume = cells[0]
        base_query = (
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
                s.jobs.c.eligible_since.is_not(None),
                or_(s.jobs.c.retry_ready_at.is_(None), s.jobs.c.retry_ready_at <= now),
                tuple_(s.job_specs.c.template_id, s.job_specs.c.template_version).in_(compatible),
                s.job_specs.c.gpu_count == 0,
                s.job_specs.c.cpu_millis > bounds[0],
                s.job_specs.c.cpu_millis <= bounds[1],
                s.job_specs.c.memory_bytes > bounds[2],
                s.job_specs.c.memory_bytes <= bounds[3],
            )
        )
        # No populated cell means no queued size fits the held-quota headroom.
        enabled = tenant_enabled[tenant_id] and tenant_id not in pending_tenants and bool(cells)
        if not enabled:
            base_query = base_query.where(False)
        tenant_resume = max(
            (
                value
                for value in (
                    counters.get(("TENANT", str(tenant_id)), {}).get("eligible_resumed_at"),
                    quota_resume,
                )
                if value
            ),
            default=None,
        )
        tenant_resumes[tenant_id] = tenant_resume
        submitters = queued_submitters.get(tenant_id, ())
        user_resumes = {
            user_id: counters.get(("USER", f"{tenant_id}:{user_id}"), {}).get("eligible_resumed_at")
            for user_id in submitters
        }
        resume_at = (
            max(
                (value for value in (tenant_resume, user_resumes[submitters[0]]) if value),
                default=None,
            )
            if len(submitters) == 1
            else None
        )
        if tenant_resume is None and not any(user_resumes.values()):
            effective_since = s.jobs.c.eligible_since.label("effective_eligible_since")
        elif len(submitters) == 1:
            effective_since = (
                func.greatest(s.jobs.c.eligible_since, literal(resume_at))
                if resume_at
                else s.jobs.c.eligible_since
            ).label("effective_eligible_since")
        else:
            user_resume = (
                select(s.admission_counters.c.eligible_resumed_at)
                .where(
                    s.admission_counters.c.scope_type == "USER",
                    s.admission_counters.c.scope_id
                    == func.concat(s.jobs.c.tenant_id, ":", s.jobs.c.submitter_user_id),
                )
                .correlate(s.jobs)
                .scalar_subquery()
            )
            effective_since = func.greatest(
                s.jobs.c.eligible_since, literal(tenant_resume), user_resume
            ).label("effective_eligible_since")
        query = base_query.add_columns(effective_since)
        if tenant_active >= limit["tenant_active_limit"]:
            query = query.where(False)
        eligible_users = [
            user_id
            for user_id in queued_submitters.get(tenant_id, ())
            if counters.get(("USER", f"{tenant_id}:{user_id}"), {}).get("active_attempts", 0)
            < limit["user_active_limit"]
        ]
        if eligible_users:
            query = query.where(s.jobs.c.submitter_user_id.in_(eligible_users))

        prepared.append(
            (
                limit,
                tenant_id,
                tenant_active,
                enabled,
                eligible_users,
                query,
                cursors.get(str(tenant_id)),
                active,
                tuple(bounds),
                cells,
                tenant_resume is None and not any(user_resumes.values()),
                resume_at,
            )
        )

    descriptors = [
        (tenant_id, *bounds, eligible_users)
        for (
            limit,
            tenant_id,
            tenant_active,
            enabled,
            eligible_users,
            query,
            cursor,
            active,
            bounds,
            cells,
            no_resume,
            _resume_at,
        ) in prepared
        if eligible_users
        and enabled
        and tenant_active < limit["tenant_active_limit"]
        and len(cells) == 1
        and no_resume
        and cursor is None
    ]
    batch_windows = _batched_window_rows(session, descriptors, now, compatible)
    resumed_descriptors = [
        (tenant_id, *bounds, eligible_users, resume_at)
        for (
            limit,
            tenant_id,
            tenant_active,
            enabled,
            eligible_users,
            query,
            cursor,
            active,
            bounds,
            cells,
            no_resume,
            resume_at,
        ) in prepared
        if eligible_users
        and enabled
        and tenant_active < limit["tenant_active_limit"]
        and len(cells) == 1
        and resume_at is not None
        and len(eligible_users) == 1
        and cursor is None
    ]
    batch_windows.update(
        _batched_window_rows(session, resumed_descriptors, now, compatible, resumed=True)
    )
    grouped_no_resume = []
    grouped_resumed = []
    grouped_keys = {}
    grouped_cursors = {}
    for (
        _limit,
        tenant_id,
        tenant_active,
        enabled,
        eligible_users,
        _query,
        cursor,
        _active,
        bounds,
        cells,
        no_resume,
        _resume_at,
    ) in prepared:
        if not eligible_users or not enabled or tenant_active >= _limit["tenant_active_limit"]:
            continue
        # A tenant whose fitting sizes span several quota resume times is read
        # per (submitter, cell), so each cell keeps its own exact age boundary.
        if len(cells) == 1 and (no_resume or len(queued_submitters.get(tenant_id, ())) < 2):
            continue
        groups = cells if len(cells) > 1 else [(*bounds, None)]
        grouped_keys[tenant_id] = []
        grouped_cursors[tenant_id] = cursor
        cursor_values = (cursor[0], cursor[1], UUID(cursor[2])) if cursor else (None,) * 3
        for user_id in eligible_users:
            user_resume = counters.get(("USER", f"{tenant_id}:{user_id}"), {}).get(
                "eligible_resumed_at"
            )
            for *cell_bounds, quota_resume in groups:
                resume_at = max(
                    (
                        value
                        for value in (tenant_resumes[tenant_id], user_resume, quota_resume)
                        if value
                    ),
                    default=None,
                )
                group_index = len(grouped_keys[tenant_id])
                grouped_keys[tenant_id].append(group_index)
                descriptor = (tenant_id, *cell_bounds, [user_id])
                if resume_at is None:
                    grouped_no_resume.append((*descriptor, user_id, group_index, *cursor_values))
                else:
                    grouped_resumed.append(
                        (*descriptor, resume_at, user_id, group_index, *cursor_values)
                    )

    grouped_windows = _batched_window_rows(
        session, grouped_no_resume, now, compatible, grouped=True
    )
    grouped_windows.update(
        _batched_window_rows(session, grouped_resumed, now, compatible, resumed=True, grouped=True)
    )

    def merged_group(tenant_id, groups, results):
        normal = []
        oldest = []
        for group_index in groups:
            group_normal, group_oldest = results.get((tenant_id, group_index), ([], None))
            normal.extend(group_normal)
            if group_oldest:
                oldest.append(group_oldest)
        normal.sort(
            key=lambda row: (-row["effective_priority"], row["ready_sequence"], row["job_id"])
        )
        oldest_row = min(
            oldest,
            key=lambda row: (row["effective_eligible_since"], row["ready_sequence"], row["job_id"]),
            default=None,
        )
        return normal[:16], oldest_row

    for tenant_id, groups in grouped_keys.items():
        rows, oldest = merged_group(tenant_id, groups, grouped_windows)
        if not rows and grouped_cursors[tenant_id]:
            no_resume = [
                (*item[:-3], None, None, None) for item in grouped_no_resume if item[0] == tenant_id
            ]
            resumed = [
                (*item[:-3], None, None, None) for item in grouped_resumed if item[0] == tenant_id
            ]
            reset_windows = _batched_window_rows(session, no_resume, now, compatible, grouped=True)
            reset_windows.update(
                _batched_window_rows(session, resumed, now, compatible, resumed=True, grouped=True)
            )
            rows, oldest = merged_group(tenant_id, groups, reset_windows)
        batch_windows[tenant_id] = (rows, oldest)

    windows = []
    for (
        limit,
        tenant_id,
        tenant_active,
        enabled,
        eligible_users,
        query,
        cursor,
        active,
        _bounds,
        cells,
        _no_resume,
        resume_at,
    ) in prepared:

        def candidate(row, tenant_id=tenant_id, tenant_active=tenant_active):
            return Candidate(
                str(row["job_id"]),
                str(tenant_id),
                str(row["submitter_user_id"]),
                row["version"],
                row["ready_sequence"],
                ResourceRequest(row["cpu_millis"], row["memory_bytes"], row["gpu_count"]),
                row["base_priority"],
                max(
                    0,
                    (epoch_ms(now) - epoch_ms(row["effective_eligible_since"] or now)) // 1000,
                ),
                epoch_ms(row["retry_ready_at"]) if row["retry_ready_at"] else 0,
                row["template_version"],
                frozenset(),
                tenant_active,
                counters.get(("USER", f"{tenant_id}:{row['submitter_user_id']}"), {}).get(
                    "active_attempts", 0
                ),
            )

        rows, oldest = (
            batch_windows[tenant_id]
            if tenant_id in batch_windows
            else _window_rows(
                session,
                query,
                now,
                cursor,
                head_priorities.get(tenant_id, set()),
                resume_at,
                tenant_id,
                eligible_users,
            )
            if eligible_users and enabled and tenant_active < limit["tenant_active_limit"]
            else ([], None)
        )
        if eligible_users and active and active["tenant_id"] == tenant_id:
            reserved = (
                session.execute(query.where(s.jobs.c.job_id == active["job_id"]))
                .mappings()
                .one_or_none()
            )
            if reserved and len(cells) > 1:
                quota_resumes = [
                    cell[4]
                    for cell in cells
                    if cell[0] < reserved["cpu_millis"] <= cell[1]
                    and cell[2] < reserved["memory_bytes"] <= cell[3]
                ]
                reserved = (
                    dict(
                        reserved,
                        effective_eligible_since=max(
                            value
                            for value in (reserved["effective_eligible_since"], *quota_resumes)
                            if value
                        ),
                    )
                    if quota_resumes
                    else None
                )
            if reserved:
                oldest = reserved  # Preserve the active protected candidate outside the window.
        next_cursor = None
        if rows:
            last = rows[-1]
            age_minutes = max(
                0, (epoch_ms(now) - epoch_ms(last["effective_eligible_since"])) // 60000
            )
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
    floor = session.execute(select(s.fairness_state.c.virtual_floor)).scalar_one_or_none()
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
        Decimal(0) if floor is None else floor,
    )
    accounted = advance_accounting(snapshot, epoch_ms(now))
    if isinstance(accounted, Err):
        raise RuntimeError(f"invalid scheduler snapshot: {accounted.error}")
    return replace(
        snapshot,
        tenant_ledgers=accounted.value.tenant_ledgers,
        virtual_floor=accounted.value.virtual_floor,
    )


def persist_fairness_locked(session, snapshot, boundary):
    """Persist floor and demand transitions on ledger rows charged through boundary.

    The caller holds the ledger row locks from account_locked or
    account_now_locked, so the scores written here include every committed
    charge and are never an older in-memory copy.
    """
    tenant_ids = {limit.tenant_id for limit in snapshot.tenant_limits}
    fresh = tuple(
        TenantLedger(
            str(row["tenant_id"]),
            row["virtual_score"],
            epoch_ms(row["accounted_through"]),
            row["had_eligible_demand"],
        )
        for row in session.execute(select(s.fairness_ledgers)).mappings()
        if str(row["tenant_id"]) in tenant_ids
    )
    floor = session.execute(select(s.fairness_state.c.virtual_floor)).scalar_one()
    accounted = advance_accounting(
        replace(snapshot, tenant_ledgers=fresh, virtual_floor=floor), epoch_ms(boundary)
    )
    if isinstance(accounted, Err):
        raise RuntimeError(f"invalid scheduler snapshot: {accounted.error}")
    ledger_updates = [
        {
            "b13_tenant_id": UUID(ledger.tenant_id),
            "b13_score": ledger.virtual_score,
            "b13_demand": ledger.had_eligible_demand,
        }
        for ledger in accounted.value.tenant_ledgers
    ]
    if ledger_updates:
        session.execute(
            update(s.fairness_ledgers)
            .where(s.fairness_ledgers.c.tenant_id == bindparam("b13_tenant_id"))
            .values(
                virtual_score=bindparam("b13_score"),
                had_eligible_demand=bindparam("b13_demand"),
            ),
            ledger_updates,
        )
    session.execute(
        update(s.fairness_state).values(
            virtual_floor=accounted.value.virtual_floor,
            version=s.fairness_state.c.version + 1,
            updated_at=boundary,
        )
    )
