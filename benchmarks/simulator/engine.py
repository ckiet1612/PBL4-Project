from dataclasses import dataclass

from benchmarks.simulator.clock import EventPhase, VirtualClock
from benchmarks.simulator.model import Allocation, JobSpec, JobState, ResourceVector, TenantSpec
from benchmarks.simulator.policy import SchedulerPolicy, SchedulingSnapshot
from benchmarks.simulator.trace import MaterializedTrace


class InvariantViolation(RuntimeError):
    """Raised immediately when a simulator correctness invariant is violated."""


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    time_ms: int
    sequence: int
    event_type: str
    job_id: str
    tenant_id: str
    resources: ResourceVector
    gpu_uuids: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    tenant_id: str
    ready_sequence: int
    arrival_ms: int
    resources: ResourceVector
    duration_ms: int
    state: JobState
    dispatch_ms: int | None
    completion_ms: int | None
    infeasible_reason: str | None


@dataclass(frozen=True, slots=True)
class SimulationResult:
    baseline_name: str
    baseline_version: str
    trace: MaterializedTrace
    job_records: tuple[JobRecord, ...]
    allocations: tuple[Allocation, ...]
    timeline: tuple[TimelineEvent, ...]
    final_allocated: ResourceVector
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


class AllocationLedger:
    def __init__(self, capacity: ResourceVector, gpu_uuids: tuple[str, ...]) -> None:
        if capacity.gpu_count != len(gpu_uuids):
            raise InvariantViolation("GPU capacity does not match configured GPU UUIDs")
        if len(set(gpu_uuids)) != len(gpu_uuids):
            raise InvariantViolation("configured GPU UUIDs are not unique")
        self._capacity = capacity
        self._gpu_uuids = tuple(sorted(gpu_uuids))
        self._allocated = ResourceVector.zero()
        self._held: dict[str, Allocation] = {}

    @property
    def allocated(self) -> ResourceVector:
        return self._allocated

    @property
    def remaining(self) -> ResourceVector:
        return self._capacity.subtract(self._allocated)

    @property
    def held_allocations(self) -> tuple[Allocation, ...]:
        return tuple(sorted(self._held.values(), key=lambda item: item.job_id))

    @property
    def free_gpu_uuids(self) -> tuple[str, ...]:
        held = {gpu_uuid for allocation in self._held.values() for gpu_uuid in allocation.gpu_uuids}
        return tuple(gpu_uuid for gpu_uuid in self._gpu_uuids if gpu_uuid not in held)

    def allocate(self, job: JobSpec, now_ms: int) -> Allocation:
        if job.job_id in self._held:
            raise InvariantViolation(f"job {job.job_id} already has a held allocation")
        if not job.resources.fits_within(self.remaining):
            raise InvariantViolation(f"allocation for {job.job_id} exceeds remaining capacity")
        gpu_uuids = self.free_gpu_uuids[: job.resources.gpu_count]
        if len(gpu_uuids) != job.resources.gpu_count:
            raise InvariantViolation(f"allocation for {job.job_id} exceeds GPU capacity")
        allocation = Allocation(
            job_id=job.job_id,
            tenant_id=job.tenant_id,
            resources=job.resources,
            gpu_uuids=gpu_uuids,
            start_ms=now_ms,
            release_ms=now_ms + job.duration_ms,
        )
        self._allocated = self._allocated.add(job.resources)
        self._held[job.job_id] = allocation
        return allocation

    def release(self, job_id: str) -> Allocation:
        try:
            allocation = self._held.pop(job_id)
        except KeyError as exc:
            raise InvariantViolation(f"allocation for {job_id} is not held") from exc
        self._allocated = self._allocated.subtract(allocation.resources)
        return allocation


class Simulator:
    def run(self, trace: MaterializedTrace, policy: SchedulerPolicy) -> SimulationResult:
        reset = getattr(policy, "reset", None)
        if callable(reset):
            reset()
        clock = VirtualClock(start_ms=0)
        jobs = {job.job_id: _MutableJob(job) for job in trace.jobs}
        for job in trace.jobs:
            clock.schedule(job.arrival_ms, EventPhase.ARRIVAL, job.job_id)

        tenants = {tenant.tenant_id: tenant for tenant in trace.config.tenants}
        ledger = AllocationLedger(trace.config.capacity, trace.config.gpu_uuids)
        allocations: list[Allocation] = []
        timeline: list[TimelineEvent] = []
        timeline_sequence = 0
        policy_decisions = 0
        candidate_evaluations = 0

        def append_event(
            event_type: str,
            job: JobSpec,
            *,
            gpu_uuids: tuple[str, ...] = (),
            reason: str | None = None,
        ) -> None:
            nonlocal timeline_sequence
            timeline.append(
                TimelineEvent(
                    time_ms=clock.now_ms,
                    sequence=timeline_sequence,
                    event_type=event_type,
                    job_id=job.job_id,
                    tenant_id=job.tenant_id,
                    resources=job.resources,
                    gpu_uuids=gpu_uuids,
                    reason=reason,
                )
            )
            timeline_sequence += 1

        while clock.has_events:
            current_time = clock.next_time_ms
            while clock.has_events and clock.next_time_ms == current_time:
                event = clock.pop_next()
                runtime = jobs[event.payload]
                if event.phase is EventPhase.RELEASE:
                    if runtime.state is not JobState.RUNNING:
                        raise InvariantViolation(
                            f"release for {runtime.spec.job_id} found state {runtime.state}"
                        )
                    append_event("complete", runtime.spec)
                    allocation = ledger.release(runtime.spec.job_id)
                    runtime.state = JobState.COMPLETED
                    runtime.completion_ms = clock.now_ms
                    append_event("release", runtime.spec, gpu_uuids=allocation.gpu_uuids)
                    continue

                if runtime.state is not JobState.PENDING:
                    raise InvariantViolation(
                        f"arrival for {runtime.spec.job_id} found state {runtime.state}"
                    )
                append_event("arrival", runtime.spec)
                tenant = tenants[runtime.spec.tenant_id]
                if not runtime.spec.resources.fits_within(trace.config.capacity):
                    runtime.state = JobState.INFEASIBLE
                    runtime.infeasible_reason = "request_exceeds_capacity"
                    append_event("infeasible", runtime.spec, reason=runtime.infeasible_reason)
                elif not runtime.spec.resources.fits_within(tenant.quota):
                    runtime.state = JobState.INFEASIBLE
                    runtime.infeasible_reason = "request_exceeds_tenant_quota"
                    append_event("infeasible", runtime.spec, reason=runtime.infeasible_reason)
                else:
                    runtime.state = JobState.QUEUED

            while True:
                candidates = self._eligible_candidates(jobs, tenants, ledger)
                if not candidates:
                    break
                snapshot = SchedulingSnapshot(
                    now_ms=clock.now_ms,
                    capacity=trace.config.capacity,
                    remaining_capacity=ledger.remaining,
                    free_gpu_uuids=ledger.free_gpu_uuids,
                    candidates=candidates,
                    held_allocations=ledger.held_allocations,
                    tenants=trace.config.tenants,
                )
                policy_decisions += 1
                candidate_evaluations += len(candidates)
                selected_job_id = policy.choose(snapshot)
                if selected_job_id is None:
                    break
                candidate_ids = {candidate.job_id for candidate in candidates}
                if selected_job_id not in candidate_ids:
                    raise InvariantViolation(
                        f"policy selected {selected_job_id} outside the eligible snapshot"
                    )
                runtime = jobs[selected_job_id]
                allocation = ledger.allocate(runtime.spec, clock.now_ms)
                allocations.append(allocation)
                runtime.state = JobState.RUNNING
                runtime.dispatch_ms = clock.now_ms
                append_event("dispatch", runtime.spec, gpu_uuids=allocation.gpu_uuids)
                clock.schedule(allocation.release_ms, EventPhase.RELEASE, runtime.spec.job_id)

        queued = sorted(
            runtime.spec.job_id for runtime in jobs.values() if runtime.state is JobState.QUEUED
        )
        if queued:
            raise InvariantViolation(f"simulation stalled with queued jobs: {', '.join(queued)}")
        if ledger.held_allocations:
            raise InvariantViolation("simulation ended with held allocations")

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
        return SimulationResult(
            baseline_name=policy.name,
            baseline_version=policy.version,
            trace=trace,
            job_records=records,
            allocations=tuple(allocations),
            timeline=tuple(timeline),
            final_allocated=ledger.allocated,
            policy_decisions=policy_decisions,
            candidate_evaluations=candidate_evaluations,
            invariant_violations=(),
        )

    @staticmethod
    def _eligible_candidates(
        jobs: dict[str, _MutableJob],
        tenants: dict[str, TenantSpec],
        ledger: AllocationLedger,
    ) -> tuple[JobSpec, ...]:
        held_by_tenant: dict[str, ResourceVector] = {
            tenant_id: ResourceVector.zero() for tenant_id in tenants
        }
        running_by_tenant = {tenant_id: 0 for tenant_id in tenants}
        for allocation in ledger.held_allocations:
            held_by_tenant[allocation.tenant_id] = held_by_tenant[allocation.tenant_id].add(
                allocation.resources
            )
            running_by_tenant[allocation.tenant_id] += 1

        candidates: list[JobSpec] = []
        for runtime in jobs.values():
            if runtime.state is not JobState.QUEUED:
                continue
            job = runtime.spec
            tenant = tenants[job.tenant_id]
            if running_by_tenant[job.tenant_id] >= tenant.max_running_jobs:
                continue
            if not held_by_tenant[job.tenant_id].add(job.resources).fits_within(tenant.quota):
                continue
            if not job.resources.fits_within(ledger.remaining):
                continue
            if job.resources.gpu_count > len(ledger.free_gpu_uuids):
                continue
            candidates.append(job)
        return tuple(sorted(candidates, key=lambda item: (item.ready_sequence, item.job_id)))
