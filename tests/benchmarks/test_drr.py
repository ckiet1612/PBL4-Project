from fractions import Fraction

from benchmarks.simulator.baselines import DeficitRoundRobinPolicy
from benchmarks.simulator.engine import Simulator
from benchmarks.simulator.model import JobSpec, ResourceVector, TenantSpec
from benchmarks.simulator.policy import SchedulingSnapshot
from tests.benchmarks.conftest import make_trace


def _job(job_id: str, tenant_id: str, cpu_millis: int, ready_sequence: int) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        tenant_id=tenant_id,
        ready_sequence=ready_sequence,
        arrival_ms=0,
        resources=ResourceVector(cpu_millis, 0, 0),
        duration_ms=1_000,
    )


def _snapshot(candidates: tuple[JobSpec, ...]) -> SchedulingSnapshot:
    capacity = ResourceVector(4_000, 4_000, 0)
    return SchedulingSnapshot(
        now_ms=0,
        capacity=capacity,
        remaining_capacity=capacity,
        free_gpu_uuids=(),
        candidates=candidates,
        held_allocations=(),
        tenants=(
            TenantSpec("tenant-a", 1, capacity, 4, 0),
            TenantSpec("tenant-b", 4, capacity, 4, 1),
        ),
    )


def test_drr_adds_exact_quantum_and_subtracts_dominant_request_cost() -> None:
    policy = DeficitRoundRobinPolicy()
    expensive = _job("a-expensive", "tenant-a", 4_000, 1)
    cheap = _job("b-cheap", "tenant-b", 1_000, 2)

    assert policy.choose(_snapshot((expensive, cheap))) == "b-cheap"
    assert policy.deficit_for("tenant-a") == Fraction(1, 4)
    assert policy.deficit_for("tenant-b") == Fraction(3, 4)

    assert policy.choose(_snapshot((expensive,))) == "a-expensive"
    assert policy.deficit_for("tenant-a") == 0


def test_drr_runs_to_completion_with_mixed_exact_costs() -> None:
    capacity = ResourceVector(4_000, 4_000, 0)
    tenants = (
        TenantSpec("tenant-a", 1, capacity, 8, 0),
        TenantSpec("tenant-b", 4, capacity, 8, 1),
    )
    jobs = (
        _job("a-1", "tenant-a", 4_000, 1),
        _job("a-2", "tenant-a", 4_000, 2),
        _job("b-1", "tenant-b", 1_000, 10),
        _job("b-2", "tenant-b", 1_000, 11),
        _job("b-3", "tenant-b", 1_000, 12),
        _job("b-4", "tenant-b", 1_000, 13),
    )

    result = Simulator().run(
        make_trace(jobs, capacity=capacity, tenants=tenants), DeficitRoundRobinPolicy()
    )

    assert all(record.completion_ms is not None for record in result.job_records)


def test_drr_spends_remaining_credit_before_advancing_to_next_tenant() -> None:
    capacity = ResourceVector(4_000, 4_000, 0)
    tenants = (
        TenantSpec("tenant-a", 1, capacity, 16, 0),
        TenantSpec("tenant-b", 4, capacity, 16, 1),
    )
    jobs = tuple(
        _job(f"{tenant_id}-{index}", tenant_id, 1_000, ready_sequence)
        for tenant_id, start in (("tenant-a", 1), ("tenant-b", 100))
        for index, ready_sequence in enumerate(range(start, start + 10), start=1)
    )

    result = Simulator().run(
        make_trace(jobs, capacity=capacity, tenants=tenants), DeficitRoundRobinPolicy()
    )

    dispatched_tenants = [
        event.tenant_id for event in result.timeline if event.event_type == "dispatch"
    ]
    assert dispatched_tenants[:10] == [
        "tenant-a",
        "tenant-b",
        "tenant-b",
        "tenant-b",
        "tenant-b",
        "tenant-a",
        "tenant-b",
        "tenant-b",
        "tenant-b",
        "tenant-b",
    ]
