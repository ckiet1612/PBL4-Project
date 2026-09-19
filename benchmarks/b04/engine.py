from dataclasses import dataclass
from decimal import Decimal

from benchmarks.b04.adapter import ProductSnapshotAdapter
from benchmarks.simulator.clock import EventPhase, VirtualClock
from benchmarks.simulator.engine import AllocationLedger, InvariantViolation, JobRecord
from benchmarks.simulator.model import Allocation, JobSpec, JobState, ResourceVector
from benchmarks.simulator.trace import MaterializedTrace
from nexa.domain.scheduling import (
    Candidate,
    CreateReservation,
    Dispatch,
    DrainForReservation,
    Err,
    InvalidateReservation,
    NoDecision,
    ReservationSnapshot,
    ResourceCapacity,
    SchedulingDecision,
    SchedulingSnapshot,
    TenantLedger,
)
from nexa.scheduler.accounting import (
    AccountingState,
    advance_accounting,
    candidate_ineligibility_reason,
)
from nexa.scheduler.policy import WeightedDominantResourceTimePolicy, effective_priority


@dataclass(frozen=True, slots=True)
class ProductTimelineEvent:
    time_ms: int
    sequence: int
    event_type: str
    job_id: str
    tenant_id: str
    reason: str | None = None
    reservation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProductAccountingRecord:
    interval_start_ms: int
    interval_end_ms: int
    held_resources_by_tenant: tuple[tuple[str, ResourceCapacity], ...]
    dominant_shares: tuple[tuple[str, Decimal], ...]
    tenant_ledgers: tuple[TenantLedger, ...]
    virtual_floor: Decimal
    eligible_tenant_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProductCandidateRecord:
    job_id: str
    user_id: str
    ready_sequence: int
    eligible_wait_seconds: int
    effective_priority: int
    eligibility_reason: str | None
    fits_free_resources: bool


@dataclass(frozen=True, slots=True)
class ProductTenantDecisionState:
    tenant_id: str
    virtual_score: Decimal
    dominant_share: Decimal
    weight: Decimal
    held_resources: ResourceCapacity
    oldest_ready_sequence: int | None
    normal_candidate_ids: tuple[str, ...]
    oldest_eligible_job_id: str | None
    continuation_cursor: bytes | None
    candidates: tuple[ProductCandidateRecord, ...]


@dataclass(frozen=True, slots=True)
class ProductDecisionRecord:
    time_ms: int
    decision_type: str
    job_id: str
    tenant_id: str
    reason: str
    reservation_id: str | None
    virtual_floor: Decimal
    tenant_state: tuple[ProductTenantDecisionState, ...]


@dataclass(frozen=True, slots=True)
class ProductSimulationResult:
    policy_name: str
    policy_version: str
    trace: MaterializedTrace
    job_records: tuple[JobRecord, ...]
    allocations: tuple[Allocation, ...]
    timeline: tuple[ProductTimelineEvent, ...]
    accounting_timeline: tuple[ProductAccountingRecord, ...]
    decision_timeline: tuple[ProductDecisionRecord, ...]
    final_tenant_ledgers: tuple[TenantLedger, ...]
    final_virtual_floor: Decimal
    policy_decisions: int
    candidate_evaluations: int
    invariant_violations: tuple[str, ...]


@dataclass(slots=True)
class _MutableJob:
    spec: JobSpec
    state: JobState = JobState.PENDING
    dispatch_ms: int | None = None
    completion_ms: int | None = None
    infeasible_reason: str | None = None
    eligible_wait_ms: int = 0
    eligible_since_ms: int | None = None


class ProductPolicySimulator:
    def run(
        self,
        trace: MaterializedTrace,
        policy: WeightedDominantResourceTimePolicy,
    ) -> ProductSimulationResult:
        clock = VirtualClock(start_ms=0)
        jobs = {job.job_id: _MutableJob(job) for job in trace.jobs}
        for job in trace.jobs:
            clock.schedule(job.arrival_ms, EventPhase.ARRIVAL, ("arrival", job.job_id))

        tenants = {tenant.tenant_id: tenant for tenant in trace.config.tenants}
        allocation_ledger = AllocationLedger(trace.config.capacity, trace.config.gpu_uuids)
        ledgers = tuple(
            TenantLedger(tenant.tenant_id, Decimal(0), 0, False) for tenant in trace.config.tenants
        )
        virtual_floor = Decimal(0)
        active_reservation: ReservationSnapshot | None = None
        reservation_sequence = 0
        adapter = ProductSnapshotAdapter()
        allocations: list[Allocation] = []
        timeline: list[ProductTimelineEvent] = []
        accounting_timeline: list[ProductAccountingRecord] = []
        decision_timeline: list[ProductDecisionRecord] = []
        timeline_sequence = 0
        policy_decisions = 0
        candidate_evaluations = 0
        scheduled_wakeups: set[tuple[int, str]] = set()
        next_tick_ms: int | None = None

        def append_event(
            event_type: str,
            *,
            job_id: str = "",
            tenant_id: str = "",
            reason: str | None = None,
            reservation_id: str | None = None,
            time_ms: int | None = None,
        ) -> None:
            nonlocal timeline_sequence
            timeline.append(
                ProductTimelineEvent(
                    time_ms=clock.now_ms if time_ms is None else time_ms,
                    sequence=timeline_sequence,
                    event_type=event_type,
                    job_id=job_id,
                    tenant_id=tenant_id,
                    reason=reason,
                    reservation_id=reservation_id,
                )
            )
            timeline_sequence += 1

        def queued_jobs() -> tuple[JobSpec, ...]:
            return tuple(
                runtime.spec
                for runtime in sorted(jobs.values(), key=lambda item: item.spec.ready_sequence)
                if runtime.state is JobState.QUEUED
            )

        def eligible_wait_overrides(now_ms: int) -> tuple[tuple[str, int], ...]:
            return tuple(
                (
                    runtime.spec.job_id,
                    (
                        runtime.eligible_wait_ms
                        + (
                            now_ms - runtime.eligible_since_ms
                            if runtime.eligible_since_ms is not None
                            else 0
                        )
                    )
                    // 1000,
                )
                for runtime in sorted(jobs.values(), key=lambda item: item.spec.job_id)
                if runtime.state is JobState.QUEUED
            )

        def build_snapshot(now_ms: int):
            return adapter.build(
                config=trace.config,
                now_ms=now_ms,
                queued_jobs=queued_jobs(),
                held_allocations=allocation_ledger.held_allocations,
                tenant_ledgers=ledgers,
                virtual_floor=virtual_floor,
                active_reservation=active_reservation,
                eligible_wait_overrides=eligible_wait_overrides(now_ms),
            )

        def build_accounting_snapshot(now_ms: int):
            return adapter.build(
                config=trace.config,
                now_ms=now_ms,
                queued_jobs=queued_jobs(),
                held_allocations=allocation_ledger.held_allocations,
                tenant_ledgers=ledgers,
                virtual_floor=virtual_floor,
                active_reservation=active_reservation,
                eligible_wait_overrides=eligible_wait_overrides(now_ms),
            )

        def account_to(now_ms: int) -> None:
            nonlocal ledgers, virtual_floor
            previous_ms = min(ledger.accounted_through_ms for ledger in ledgers)
            held_before = bool(allocation_ledger.held_allocations)
            accounted = advance_accounting(build_accounting_snapshot(now_ms), now_ms)
            if isinstance(accounted, Err):
                raise InvariantViolation(f"product accounting failed: {accounted.error}")
            ledgers = accounted.value.tenant_ledgers
            virtual_floor = accounted.value.virtual_floor
            if now_ms > previous_ms:
                accounting_timeline.append(
                    ProductAccountingRecord(
                        interval_start_ms=previous_ms,
                        interval_end_ms=now_ms,
                        held_resources_by_tenant=accounted.value.held_resources_by_tenant,
                        dominant_shares=accounted.value.dominant_shares,
                        tenant_ledgers=accounted.value.tenant_ledgers,
                        virtual_floor=accounted.value.virtual_floor,
                        eligible_tenant_ids=accounted.value.eligible_tenant_ids,
                    )
                )
            if held_before and now_ms > previous_ms:
                append_event("accounting_tick", time_ms=now_ms)

        def schedule_wakeup(time_ms: int, kind: str) -> None:
            key = (time_ms, kind)
            if time_ms <= clock.now_ms or key in scheduled_wakeups:
                return
            scheduled_wakeups.add(key)
            clock.schedule(time_ms, EventPhase.ARRIVAL, ("wakeup", kind))

        def schedule_next_tick() -> None:
            nonlocal next_tick_ms
            if allocation_ledger.held_allocations and next_tick_ms is None:
                next_tick_ms = clock.now_ms + 1000
                clock.schedule(next_tick_ms, EventPhase.ARRIVAL, ("wakeup", "tick"))

        def refresh_eligibility(now_ms: int) -> None:
            held_by_tenant = {tenant_id: ResourceVector.zero() for tenant_id in tenants}
            active_by_tenant = {tenant_id: 0 for tenant_id in tenants}
            for allocation in allocation_ledger.held_allocations:
                held_by_tenant[allocation.tenant_id] = held_by_tenant[allocation.tenant_id].add(
                    allocation.resources
                )
                active_by_tenant[allocation.tenant_id] += 1

            for runtime in jobs.values():
                tenant = tenants[runtime.spec.tenant_id]
                is_eligible = (
                    runtime.state is JobState.QUEUED
                    and held_by_tenant[runtime.spec.tenant_id]
                    .add(runtime.spec.resources)
                    .fits_within(tenant.quota)
                    and active_by_tenant[runtime.spec.tenant_id] < tenant.max_running_jobs
                )
                if is_eligible and runtime.eligible_since_ms is None:
                    runtime.eligible_since_ms = now_ms
                elif not is_eligible and runtime.eligible_since_ms is not None:
                    runtime.eligible_wait_ms += now_ms - runtime.eligible_since_ms
                    runtime.eligible_since_ms = None

                if runtime.eligible_since_ms is None:
                    continue
                current_wait_ms = runtime.eligible_wait_ms + now_ms - runtime.eligible_since_ms
                for threshold_ms, kind in ((60_000, "aging"), (120_000, "reservation")):
                    if current_wait_ms < threshold_ms:
                        schedule_wakeup(now_ms + threshold_ms - current_wait_ms, kind)

        def assert_allocation_invariants() -> None:
            held_by_tenant = {tenant_id: ResourceVector.zero() for tenant_id in tenants}
            count_by_tenant = {tenant_id: 0 for tenant_id in tenants}
            gpu_uuids: set[str] = set()
            for allocation in allocation_ledger.held_allocations:
                held_by_tenant[allocation.tenant_id] = held_by_tenant[allocation.tenant_id].add(
                    allocation.resources
                )
                count_by_tenant[allocation.tenant_id] += 1
                for gpu_uuid in allocation.gpu_uuids:
                    if gpu_uuid in gpu_uuids:
                        raise InvariantViolation(f"GPU UUID {gpu_uuid} is held twice")
                    gpu_uuids.add(gpu_uuid)
            for tenant_id, resources in held_by_tenant.items():
                tenant = tenants[tenant_id]
                if not resources.fits_within(tenant.quota):
                    raise InvariantViolation(f"tenant {tenant_id} exceeds quota")
                if count_by_tenant[tenant_id] > tenant.max_running_jobs:
                    raise InvariantViolation(f"tenant {tenant_id} exceeds concurrency")

        def request_fits_free(candidate: Candidate, free: ResourceCapacity) -> bool:
            return (
                candidate.resources.cpu_millis <= free.cpu_millis
                and candidate.resources.memory_bytes <= free.memory_bytes
                and candidate.resources.gpu_count <= free.gpu_count
            )

        def record_decision(
            snapshot: SchedulingSnapshot,
            accounting: AccountingState,
            decision: SchedulingDecision,
        ) -> None:
            held_by_tenant = dict(accounting.held_resources_by_tenant)
            dominant_shares = dict(accounting.dominant_shares)
            limits = {limit.tenant_id: limit for limit in snapshot.tenant_limits}
            ledgers_by_tenant = {ledger.tenant_id: ledger for ledger in accounting.tenant_ledgers}
            total_held = ResourceCapacity(0, 0, 0)
            for held in held_by_tenant.values():
                total_held = ResourceCapacity(
                    total_held.cpu_millis + held.cpu_millis,
                    total_held.memory_bytes + held.memory_bytes,
                    total_held.gpu_count + held.gpu_count,
                )
            free = ResourceCapacity(
                snapshot.allocatable_capacity.cpu_millis - total_held.cpu_millis,
                snapshot.allocatable_capacity.memory_bytes - total_held.memory_bytes,
                snapshot.allocatable_capacity.gpu_count - total_held.gpu_count,
            )

            tenant_state: list[ProductTenantDecisionState] = []
            for tenant_id, window in snapshot.candidates_by_tenant:
                unique_candidates = {candidate.job_id: candidate for candidate in window.normal}
                if window.oldest_eligible is not None:
                    unique_candidates[window.oldest_eligible.job_id] = window.oldest_eligible
                candidate_records: list[ProductCandidateRecord] = []
                eligible_sequences: list[int] = []
                for candidate in unique_candidates.values():
                    ineligibility = candidate_ineligibility_reason(
                        candidate,
                        now_ms=clock.now_ms,
                        capacity=snapshot.allocatable_capacity,
                        held=held_by_tenant[tenant_id],
                        limits=limits[tenant_id],
                    )
                    if ineligibility is None:
                        eligible_sequences.append(candidate.ready_sequence)
                    candidate_records.append(
                        ProductCandidateRecord(
                            job_id=candidate.job_id,
                            user_id=candidate.user_id,
                            ready_sequence=candidate.ready_sequence,
                            eligible_wait_seconds=candidate.eligible_wait_seconds,
                            effective_priority=effective_priority(candidate),
                            eligibility_reason=ineligibility,
                            fits_free_resources=request_fits_free(candidate, free),
                        )
                    )
                tenant_state.append(
                    ProductTenantDecisionState(
                        tenant_id=tenant_id,
                        virtual_score=ledgers_by_tenant[tenant_id].virtual_score,
                        dominant_share=dominant_shares[tenant_id],
                        weight=limits[tenant_id].weight,
                        held_resources=held_by_tenant[tenant_id],
                        oldest_ready_sequence=min(eligible_sequences, default=None),
                        normal_candidate_ids=tuple(candidate.job_id for candidate in window.normal),
                        oldest_eligible_job_id=(
                            window.oldest_eligible.job_id
                            if window.oldest_eligible is not None
                            else None
                        ),
                        continuation_cursor=window.continuation_cursor,
                        candidates=tuple(candidate_records),
                    )
                )

            if isinstance(decision, Dispatch):
                matching_state = next(
                    state
                    for state in tenant_state
                    if any(candidate.job_id == decision.job_id for candidate in state.candidates)
                )
                decision_type = "dispatch"
                job_id = decision.job_id
                tenant_id = matching_state.tenant_id
                reason = "reservation_fit" if decision.reservation_id else "fairness_order"
                reservation_id = decision.reservation_id
            elif isinstance(decision, CreateReservation):
                decision_type = "create_reservation"
                job_id = decision.job_id
                tenant_id = decision.tenant_id
                reason = decision.reason
                reservation_id = f"reservation-{reservation_sequence + 1:06d}"
            elif isinstance(decision, DrainForReservation):
                decision_type = "drain_for_reservation"
                job_id = decision.job_id
                tenant_id = active_reservation.tenant_id if active_reservation else ""
                reason = "waiting_for_free_resources"
                reservation_id = decision.reservation_id
            elif isinstance(decision, InvalidateReservation):
                decision_type = "invalidate_reservation"
                job_id = active_reservation.job_id if active_reservation else ""
                tenant_id = active_reservation.tenant_id if active_reservation else ""
                reason = decision.reason
                reservation_id = decision.reservation_id
            else:
                decision_type = "no_decision"
                job_id = ""
                tenant_id = ""
                reason = decision.reason
                reservation_id = None

            decision_timeline.append(
                ProductDecisionRecord(
                    time_ms=clock.now_ms,
                    decision_type=decision_type,
                    job_id=job_id,
                    tenant_id=tenant_id,
                    reason=reason,
                    reservation_id=reservation_id,
                    virtual_floor=accounting.virtual_floor,
                    tenant_state=tuple(tenant_state),
                )
            )

        while clock.has_events:
            current_time = clock.next_time_ms
            account_to(current_time)
            should_decide = False
            while clock.has_events and clock.next_time_ms == current_time:
                event = clock.pop_next()
                event_type, payload = event.payload
                if event_type == "wakeup":
                    if payload == "tick":
                        next_tick_ms = None
                    else:
                        scheduled_wakeups.discard((event.time_ms, payload))
                    should_decide = should_decide or payload != "tick"
                    continue
                should_decide = True
                runtime = jobs[payload]
                if event_type == "release":
                    if runtime.state is not JobState.RUNNING:
                        raise InvariantViolation(
                            f"release for {runtime.spec.job_id} found state {runtime.state}"
                        )
                    allocation = allocation_ledger.release(runtime.spec.job_id)
                    runtime.state = JobState.COMPLETED
                    runtime.completion_ms = clock.now_ms
                    append_event(
                        "complete",
                        job_id=runtime.spec.job_id,
                        tenant_id=runtime.spec.tenant_id,
                    )
                    append_event(
                        "release",
                        job_id=runtime.spec.job_id,
                        tenant_id=runtime.spec.tenant_id,
                        reason=",".join(allocation.gpu_uuids) or None,
                    )
                    continue

                if runtime.state is not JobState.PENDING:
                    raise InvariantViolation(
                        f"arrival for {runtime.spec.job_id} found state {runtime.state}"
                    )
                append_event(
                    "arrival",
                    job_id=runtime.spec.job_id,
                    tenant_id=runtime.spec.tenant_id,
                )
                tenant = tenants[runtime.spec.tenant_id]
                if not runtime.spec.resources.fits_within(trace.config.capacity):
                    runtime.state = JobState.INFEASIBLE
                    runtime.infeasible_reason = "request_exceeds_capacity"
                elif not runtime.spec.resources.fits_within(tenant.quota):
                    runtime.state = JobState.INFEASIBLE
                    runtime.infeasible_reason = "request_exceeds_tenant_quota"
                else:
                    runtime.state = JobState.QUEUED
                if runtime.infeasible_reason is not None:
                    append_event(
                        "infeasible",
                        job_id=runtime.spec.job_id,
                        tenant_id=runtime.spec.tenant_id,
                        reason=runtime.infeasible_reason,
                    )

            refresh_eligibility(clock.now_ms)
            while should_decide:
                refresh_eligibility(clock.now_ms)
                snapshot = build_snapshot(clock.now_ms)
                accounted = advance_accounting(snapshot, clock.now_ms)
                if isinstance(accounted, Err):
                    raise InvariantViolation(f"product accounting failed: {accounted.error}")
                ledgers = accounted.value.tenant_ledgers
                virtual_floor = accounted.value.virtual_floor
                snapshot = build_snapshot(clock.now_ms)
                policy_decisions += 1
                candidate_evaluations += sum(
                    len(
                        {
                            candidate.job_id
                            for candidate in (*window.normal, window.oldest_eligible)
                            if candidate is not None
                        }
                    )
                    for _, window in snapshot.candidates_by_tenant
                )
                decision_result = policy.decide(snapshot, clock.now_ms)
                if isinstance(decision_result, Err):
                    raise InvariantViolation(f"product policy failed: {decision_result.error}")
                decision = decision_result.value
                record_decision(snapshot, accounted.value, decision)
                if isinstance(decision, Dispatch):
                    runtime = jobs[decision.job_id]
                    if runtime.state is not JobState.QUEUED:
                        raise InvariantViolation(
                            f"policy dispatched non-queued job {decision.job_id}"
                        )
                    if decision.expected_job_version != 1:
                        raise InvariantViolation("policy returned an unexpected job version")
                    allocation = allocation_ledger.allocate(runtime.spec, clock.now_ms)
                    allocations.append(allocation)
                    runtime.state = JobState.RUNNING
                    runtime.dispatch_ms = clock.now_ms
                    append_event(
                        "dispatch",
                        job_id=runtime.spec.job_id,
                        tenant_id=runtime.spec.tenant_id,
                        reservation_id=decision.reservation_id,
                    )
                    if decision.reservation_id is not None:
                        append_event(
                            "reservation_dispatched",
                            job_id=runtime.spec.job_id,
                            tenant_id=runtime.spec.tenant_id,
                            reservation_id=decision.reservation_id,
                        )
                        active_reservation = None
                    clock.schedule(
                        allocation.release_ms,
                        EventPhase.RELEASE,
                        ("release", runtime.spec.job_id),
                    )
                    assert_allocation_invariants()
                    schedule_next_tick()
                    continue
                if isinstance(decision, CreateReservation):
                    if active_reservation is not None:
                        raise InvariantViolation("policy attempted to create a second reservation")
                    reservation_sequence += 1
                    active_reservation = ReservationSnapshot(
                        reservation_id=f"reservation-{reservation_sequence:06d}",
                        job_id=decision.job_id,
                        tenant_id=decision.tenant_id,
                        policy_version=snapshot.policy_version,
                    )
                    append_event(
                        "reservation_created",
                        job_id=decision.job_id,
                        tenant_id=decision.tenant_id,
                        reason=decision.reason,
                        reservation_id=active_reservation.reservation_id,
                    )
                    continue
                if isinstance(decision, DrainForReservation):
                    append_event(
                        "reservation_draining",
                        job_id=decision.job_id,
                        tenant_id=(active_reservation.tenant_id if active_reservation else ""),
                        reservation_id=decision.reservation_id,
                    )
                    break
                if isinstance(decision, InvalidateReservation):
                    if active_reservation is None:
                        raise InvariantViolation("policy invalidated a missing reservation")
                    append_event(
                        "reservation_invalidated",
                        job_id=active_reservation.job_id,
                        tenant_id=active_reservation.tenant_id,
                        reason=decision.reason,
                        reservation_id=decision.reservation_id,
                    )
                    active_reservation = None
                    continue
                if isinstance(decision, NoDecision):
                    break
                raise InvariantViolation(f"unsupported product decision: {decision}")

            schedule_next_tick()

        queued = sorted(
            runtime.spec.job_id for runtime in jobs.values() if runtime.state is JobState.QUEUED
        )
        if queued:
            raise InvariantViolation(
                f"product simulation stalled with queued jobs: {', '.join(queued)}"
            )
        if allocation_ledger.held_allocations:
            raise InvariantViolation("product simulation ended with held allocations")

        records = tuple(
            JobRecord(
                job_id=runtime.spec.job_id,
                tenant_id=runtime.spec.tenant_id,
                ready_sequence=runtime.spec.ready_sequence,
                arrival_ms=runtime.spec.arrival_ms,
                resources=runtime.spec.resources,
                duration_ms=runtime.spec.duration_ms,
                state=runtime.state,
                dispatch_ms=runtime.dispatch_ms,
                completion_ms=runtime.completion_ms,
                infeasible_reason=runtime.infeasible_reason,
            )
            for runtime in sorted(jobs.values(), key=lambda item: item.spec.ready_sequence)
        )
        return ProductSimulationResult(
            policy_name=policy.name,
            policy_version=policy.version,
            trace=trace,
            job_records=records,
            allocations=tuple(allocations),
            timeline=tuple(timeline),
            accounting_timeline=tuple(accounting_timeline),
            decision_timeline=tuple(decision_timeline),
            final_tenant_ledgers=ledgers,
            final_virtual_floor=virtual_floor,
            policy_decisions=policy_decisions,
            candidate_evaluations=candidate_evaluations,
            invariant_violations=(),
        )
