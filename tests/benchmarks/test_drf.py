from benchmarks.simulator.baselines import DominantResourceFairnessPolicy
from benchmarks.simulator.model import Allocation, JobSpec, ResourceVector, TenantSpec
from benchmarks.simulator.policy import SchedulingSnapshot


def _job(job_id: str, tenant_id: str, ready_sequence: int) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        tenant_id=tenant_id,
        ready_sequence=ready_sequence,
        arrival_ms=0,
        resources=ResourceVector(500, 500, 0),
        duration_ms=1_000,
    )


def _allocation(job_id: str, tenant_id: str, cpu_millis: int) -> Allocation:
    return Allocation(
        job_id=job_id,
        tenant_id=tenant_id,
        resources=ResourceVector(cpu_millis, 0, 0),
        gpu_uuids=(),
        start_ms=0,
        release_ms=1_000,
    )


def _snapshot(
    candidates: tuple[JobSpec, ...],
    allocations: tuple[Allocation, ...] = (),
    weights: tuple[int, int] = (1, 1),
) -> SchedulingSnapshot:
    capacity = ResourceVector(4_000, 4_000, 0)
    return SchedulingSnapshot(
        now_ms=0,
        capacity=capacity,
        remaining_capacity=capacity,
        free_gpu_uuids=(),
        candidates=candidates,
        held_allocations=allocations,
        tenants=(
            TenantSpec("tenant-a", weights[0], capacity, 4, 0),
            TenantSpec("tenant-b", weights[1], capacity, 4, 1),
        ),
    )


def test_drf_selects_tenant_with_lower_current_dominant_share() -> None:
    policy = DominantResourceFairnessPolicy()
    candidates = (_job("a-next", "tenant-a", 1), _job("b-next", "tenant-b", 2))
    allocations = (
        _allocation("a-running", "tenant-a", 2_000),
        _allocation("b-running", "tenant-b", 1_000),
    )

    assert policy.choose(_snapshot(candidates, allocations)) == "b-next"


def test_drf_ties_by_higher_weight_then_ready_sequence_then_tenant_id() -> None:
    policy = DominantResourceFairnessPolicy()
    candidates = (_job("a-next", "tenant-a", 1), _job("b-next", "tenant-b", 2))
    assert policy.choose(_snapshot(candidates, weights=(1, 2))) == "b-next"

    older_b = (_job("a-next", "tenant-a", 5), _job("b-next", "tenant-b", 4))
    assert policy.choose(_snapshot(older_b)) == "b-next"

    same_ready = (_job("a-next", "tenant-a", 5), _job("b-next", "tenant-b", 5))
    assert policy.choose(_snapshot(same_ready)) == "a-next"
