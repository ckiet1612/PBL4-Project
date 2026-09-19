from benchmarks.simulator.baselines import RoundRobinPolicy, WeightedRoundRobinPolicy
from benchmarks.simulator.engine import SimulationResult, Simulator
from benchmarks.simulator.model import JobSpec, ResourceVector, TenantSpec
from tests.benchmarks.conftest import make_trace


def _tenant(tenant_id: str, weight: int, order: int) -> TenantSpec:
    capacity = ResourceVector(1_000, 1_024, 0)
    return TenantSpec(tenant_id, weight, capacity, 1, order)


def _job(job_id: str, tenant_id: str, ready_sequence: int) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        tenant_id=tenant_id,
        ready_sequence=ready_sequence,
        arrival_ms=0,
        resources=ResourceVector(1_000, 1_024, 0),
        duration_ms=1_000,
    )


def _dispatch_order(result: SimulationResult) -> list[str]:
    return [event.job_id for event in result.timeline if event.event_type == "dispatch"]


def test_round_robin_rotates_tenants_and_preserves_cursor_when_one_is_empty() -> None:
    tenants = (_tenant("tenant-a", 1, 0), _tenant("tenant-b", 1, 1), _tenant("tenant-c", 1, 2))
    jobs = (
        _job("a-1", "tenant-a", 1),
        _job("c-1", "tenant-c", 2),
        _job("a-2", "tenant-a", 3),
        _job("c-2", "tenant-c", 4),
    )

    result = Simulator().run(
        make_trace(jobs, capacity=tenants[0].quota, tenants=tenants), RoundRobinPolicy()
    )

    assert _dispatch_order(result) == ["a-1", "c-1", "a-2", "c-2"]


def test_weighted_round_robin_uses_repeating_integer_weight_slots() -> None:
    tenants = (_tenant("tenant-a", 1, 0), _tenant("tenant-b", 2, 1), _tenant("tenant-c", 4, 2))
    jobs = tuple(
        _job(f"{tenant_id}-{index}", tenant_id, ready_sequence)
        for tenant_id, count, start in (
            ("tenant-a", 2, 1),
            ("tenant-b", 4, 100),
            ("tenant-c", 8, 200),
        )
        for index, ready_sequence in enumerate(range(start, start + count), start=1)
    )

    result = Simulator().run(
        make_trace(jobs, capacity=tenants[0].quota, tenants=tenants),
        WeightedRoundRobinPolicy(),
    )

    assert [job_id.split("-")[1] for job_id in _dispatch_order(result)[:7]] == [
        "a",
        "b",
        "b",
        "c",
        "c",
        "c",
        "c",
    ]


def test_weighted_round_robin_reduces_weights_to_the_smallest_integer_cycle() -> None:
    tenants = (_tenant("tenant-a", 2, 0), _tenant("tenant-b", 4, 1), _tenant("tenant-c", 8, 2))
    jobs = tuple(
        _job(f"{tenant_id}-{index}", tenant_id, ready_sequence)
        for tenant_id, count, start in (
            ("tenant-a", 2, 1),
            ("tenant-b", 4, 100),
            ("tenant-c", 8, 200),
        )
        for index, ready_sequence in enumerate(range(start, start + count), start=1)
    )

    result = Simulator().run(
        make_trace(jobs, capacity=tenants[0].quota, tenants=tenants),
        WeightedRoundRobinPolicy(),
    )

    assert [job_id.split("-")[1] for job_id in _dispatch_order(result)[:7]] == [
        "a",
        "b",
        "b",
        "c",
        "c",
        "c",
        "c",
    ]


def test_weighted_round_robin_does_not_override_tenant_quota() -> None:
    capacity = ResourceVector(2_000, 2_048, 0)
    tenants = (
        TenantSpec("tenant-a", 4, ResourceVector(1_000, 2_048, 0), 4, 0),
        TenantSpec("tenant-b", 1, capacity, 4, 1),
    )
    jobs = (
        JobSpec("a-1", "tenant-a", 1, 0, ResourceVector(1_000, 1_024, 0), 1_000),
        JobSpec("a-2", "tenant-a", 2, 0, ResourceVector(1_000, 1_024, 0), 1_000),
        JobSpec("b-1", "tenant-b", 3, 0, ResourceVector(1_000, 1_024, 0), 1_000),
    )

    result = Simulator().run(
        make_trace(jobs, capacity=capacity, tenants=tenants), WeightedRoundRobinPolicy()
    )
    dispatch_ms = {record.job_id: record.dispatch_ms for record in result.job_records}

    assert dispatch_ms == {"a-1": 0, "a-2": 1_000, "b-1": 0}
