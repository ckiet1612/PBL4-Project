from dataclasses import dataclass
from decimal import Decimal

from benchmarks.simulator.model import Allocation, JobSpec, ResourceVector, SimulationConfig
from nexa.domain.scheduling import (
    AllocationState,
    Candidate,
    CandidateState,
    CandidateWindow,
    HeldAllocation,
    ReservationSnapshot,
    ResourceCapacity,
    ResourceRequest,
    SchedulingSnapshot,
    TenantLedger,
    TenantPolicySnapshot,
)

_MAX_NORMAL_CANDIDATES = 16


def _capacity(resources: ResourceVector) -> ResourceCapacity:
    return ResourceCapacity(
        resources.cpu_millis,
        resources.memory_bytes,
        resources.gpu_count,
    )


def _request(resources: ResourceVector) -> ResourceRequest:
    return ResourceRequest(
        resources.cpu_millis,
        resources.memory_bytes,
        resources.gpu_count,
    )


def _fits(resources: ResourceVector, capacity: ResourceVector) -> bool:
    return resources.fits_within(capacity)


@dataclass(frozen=True, slots=True)
class ProductSnapshotAdapter:
    policy_version: int = 1

    def build(
        self,
        *,
        config: SimulationConfig,
        now_ms: int,
        queued_jobs: tuple[JobSpec, ...],
        held_allocations: tuple[Allocation, ...],
        tenant_ledgers: tuple[TenantLedger, ...],
        virtual_floor: Decimal,
        active_reservation: ReservationSnapshot | None,
        eligible_wait_overrides: tuple[tuple[str, int], ...],
    ) -> SchedulingSnapshot:
        age_overrides: dict[str, int] = {}
        for job_id, eligible_wait_seconds in eligible_wait_overrides:
            if job_id in age_overrides:
                raise ValueError(f"duplicate eligible wait override for {job_id}")
            if type(eligible_wait_seconds) is not int or eligible_wait_seconds < 0:
                raise ValueError("eligible wait override must be a non-negative integer")
            age_overrides[job_id] = eligible_wait_seconds
        queued_job_ids = [job.job_id for job in queued_jobs]
        if len(set(queued_job_ids)) != len(queued_job_ids):
            raise ValueError("queued jobs contain duplicate job IDs")
        if set(age_overrides) != set(queued_job_ids):
            raise ValueError("eligible wait overrides must match queued jobs exactly")
        active_by_tenant = {tenant.tenant_id: 0 for tenant in config.tenants}
        held_by_tenant = {tenant.tenant_id: ResourceVector.zero() for tenant in config.tenants}
        for allocation in held_allocations:
            active_by_tenant[allocation.tenant_id] += 1
            held_by_tenant[allocation.tenant_id] = held_by_tenant[allocation.tenant_id].add(
                allocation.resources
            )

        candidates_by_tenant: list[tuple[str, CandidateWindow]] = []
        jobs_by_tenant = {
            tenant.tenant_id: [job for job in queued_jobs if job.tenant_id == tenant.tenant_id]
            for tenant in config.tenants
        }
        for tenant in config.tenants:
            candidates = tuple(
                Candidate(
                    job_id=job.job_id,
                    tenant_id=job.tenant_id,
                    user_id=f"{job.tenant_id}:user",
                    job_version=1,
                    ready_sequence=job.ready_sequence,
                    resources=_request(job.resources),
                    base_priority=job.priority,
                    eligible_wait_seconds=age_overrides[job.job_id],
                    retry_ready_at_ms=job.arrival_ms,
                    template_version=1,
                    required_capabilities=(
                        frozenset({"gpu"}) if job.resources.gpu_count else frozenset({"cpu"})
                    ),
                    tenant_active_attempts=active_by_tenant[tenant.tenant_id],
                    user_active_attempts=active_by_tenant[tenant.tenant_id],
                    state=CandidateState.QUEUED,
                )
                for job in jobs_by_tenant[tenant.tenant_id]
            )
            ordered = tuple(
                sorted(
                    candidates,
                    key=lambda candidate: (
                        -min(
                            2,
                            candidate.base_priority + candidate.eligible_wait_seconds // 60,
                        ),
                        candidate.ready_sequence,
                        candidate.job_id,
                    ),
                )
            )
            normal = ordered[:_MAX_NORMAL_CANDIDATES]
            feasible_oldest = [
                candidate
                for candidate in candidates
                if _fits(
                    ResourceVector(
                        candidate.resources.cpu_millis,
                        candidate.resources.memory_bytes,
                        candidate.resources.gpu_count,
                    ),
                    config.capacity,
                )
                and _fits(
                    held_by_tenant[tenant.tenant_id].add(
                        ResourceVector(
                            candidate.resources.cpu_millis,
                            candidate.resources.memory_bytes,
                            candidate.resources.gpu_count,
                        )
                    ),
                    tenant.quota,
                )
                and active_by_tenant[tenant.tenant_id] < tenant.max_running_jobs
            ]
            oldest = min(
                feasible_oldest,
                key=lambda candidate: (candidate.ready_sequence, candidate.job_id),
                default=None,
            )
            if active_reservation is not None and active_reservation.tenant_id == tenant.tenant_id:
                reserved = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate.job_id == active_reservation.job_id
                    ),
                    None,
                )
                if reserved is not None:
                    oldest = reserved
            cursor = None
            if len(ordered) > _MAX_NORMAL_CANDIDATES:
                last = normal[-1]
                cursor = f"{tenant.tenant_id}:{last.ready_sequence}:{last.job_id}".encode()
            candidates_by_tenant.append((tenant.tenant_id, CandidateWindow(normal, oldest, cursor)))

        return SchedulingSnapshot(
            policy_version=self.policy_version,
            allocatable_capacity=_capacity(config.capacity),
            held_allocations=tuple(
                HeldAllocation(
                    allocation_id=allocation.job_id,
                    tenant_id=allocation.tenant_id,
                    resources=_request(allocation.resources),
                    gpu_uuids=allocation.gpu_uuids,
                    state=AllocationState.HELD,
                )
                for allocation in held_allocations
            ),
            tenant_ledgers=tenant_ledgers,
            tenant_limits=tuple(
                TenantPolicySnapshot(
                    tenant_id=tenant.tenant_id,
                    weight=Decimal(tenant.weight),
                    resource_quota=_capacity(tenant.quota),
                    max_active_attempts=tenant.max_running_jobs,
                    max_user_active_attempts=tenant.max_running_jobs,
                )
                for tenant in config.tenants
            ),
            candidates_by_tenant=tuple(candidates_by_tenant),
            active_reservation=active_reservation,
            virtual_floor=virtual_floor,
        )
