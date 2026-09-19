from hypothesis import given, settings
from hypothesis import strategies as st

from benchmarks.b04.engine import ProductPolicySimulator
from benchmarks.simulator.model import (
    JobSpec,
    JobState,
    ResourceVector,
    SimulationConfig,
    TenantSpec,
)
from benchmarks.simulator.trace import FairnessWindow, MaterializedTrace
from nexa.scheduler.policy import WeightedDominantResourceTimePolicy


def _materialized_trace(jobs: tuple[JobSpec, ...], *, gpu_count: int = 0) -> MaterializedTrace:
    capacity = ResourceVector(4, 4, gpu_count)
    tenants = (
        TenantSpec("tenant-a", 1, capacity, 4, 0),
        TenantSpec("tenant-b", 2, capacity, 4, 1),
    )
    return MaterializedTrace(
        version=1,
        trace_id="b04-property",
        seed=7,
        config=SimulationConfig(
            capacity,
            tuple(f"GPU-{index}" for index in range(gpu_count)),
            tenants,
        ),
        fairness_window=FairnessWindow(0, 1_000_000, ("tenant-a", "tenant-b")),
        jobs=jobs,
        trace_checksum="sha256:" + "3" * 64,
        materialized_checksum="sha256:" + "4" * 64,
    )


@settings(max_examples=40, deadline=None)
@given(
    raw_jobs=st.lists(
        st.tuples(
            st.sampled_from(("tenant-a", "tenant-b")),
            st.integers(min_value=0, max_value=5000),
            st.integers(min_value=1, max_value=4),
            st.integers(min_value=1, max_value=5000),
        ),
        min_size=1,
        max_size=12,
    )
)
def test_generated_arrival_release_sequences_preserve_resource_invariants(
    raw_jobs: list[tuple[str, int, int, int]],
) -> None:
    jobs = tuple(
        JobSpec(
            job_id=f"job-{index}",
            tenant_id=tenant_id,
            ready_sequence=index,
            arrival_ms=arrival_ms,
            resources=ResourceVector(cpu, 1, 0),
            duration_ms=duration_ms,
        )
        for index, (tenant_id, arrival_ms, cpu, duration_ms) in enumerate(raw_jobs)
    )
    trace = _materialized_trace(jobs)

    result = ProductPolicySimulator().run(trace, WeightedDominantResourceTimePolicy())

    assert all(record.state is JobState.COMPLETED for record in result.job_records)
    assert not result.invariant_violations
    boundaries = sorted(
        {
            time
            for allocation in result.allocations
            for time in (allocation.start_ms, allocation.release_ms)
        }
    )
    for boundary in boundaries:
        active = [
            allocation
            for allocation in result.allocations
            if allocation.start_ms <= boundary < allocation.release_ms
        ]
        assert sum(allocation.resources.cpu_millis for allocation in active) <= 4
        assert sum(allocation.resources.memory_bytes for allocation in active) <= 4
        for tenant_id in ("tenant-a", "tenant-b"):
            tenant_active = [a for a in active if a.tenant_id == tenant_id]
            assert len(tenant_active) <= 4
            assert sum(a.resources.cpu_millis for a in tenant_active) <= 4


@settings(max_examples=25, deadline=None)
@given(arrivals=st.lists(st.integers(min_value=0, max_value=20_000), min_size=2, max_size=8))
def test_generated_gpu_sequences_never_overlap_one_uuid(arrivals: list[int]) -> None:
    jobs = tuple(
        JobSpec(
            job_id=f"gpu-{index}",
            tenant_id="tenant-a" if index % 2 == 0 else "tenant-b",
            ready_sequence=index,
            arrival_ms=arrival_ms,
            resources=ResourceVector(1, 1, 1),
            duration_ms=3000,
        )
        for index, arrival_ms in enumerate(arrivals)
    )
    trace = _materialized_trace(jobs, gpu_count=1)

    result = ProductPolicySimulator().run(trace, WeightedDominantResourceTimePolicy())

    for left, right in zip(result.allocations, result.allocations[1:], strict=False):
        assert left.gpu_uuids == right.gpu_uuids == ("GPU-0",)
        assert left.release_ms <= right.start_ms


def test_replaying_with_a_new_policy_instance_is_identical() -> None:
    jobs = (
        JobSpec("a", "tenant-a", 1, 0, ResourceVector(2, 1, 0), 2000),
        JobSpec("b", "tenant-b", 2, 0, ResourceVector(2, 1, 0), 2000),
    )
    trace = _materialized_trace(jobs)

    first = ProductPolicySimulator().run(trace, WeightedDominantResourceTimePolicy())
    replay = ProductPolicySimulator().run(trace, WeightedDominantResourceTimePolicy())

    assert first == replay
