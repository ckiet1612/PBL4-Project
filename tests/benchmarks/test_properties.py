from collections.abc import Callable

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from benchmarks.simulator.baselines import (
    DeficitRoundRobinPolicy,
    DominantResourceFairnessPolicy,
    FifoPolicy,
    RoundRobinPolicy,
    WeightedRoundRobinPolicy,
)
from benchmarks.simulator.engine import SimulationResult, Simulator
from benchmarks.simulator.model import JobSpec, JobState, ResourceVector, TenantSpec
from benchmarks.simulator.policy import SchedulerPolicy
from tests.benchmarks.conftest import make_trace

POLICIES: tuple[Callable[[], SchedulerPolicy], ...] = (
    FifoPolicy,
    RoundRobinPolicy,
    WeightedRoundRobinPolicy,
    DeficitRoundRobinPolicy,
    DominantResourceFairnessPolicy,
)


@st.composite
def finite_trace_inputs(
    draw: st.DrawFn,
) -> tuple[ResourceVector, tuple[TenantSpec, ...], tuple[JobSpec, ...], tuple[str, ...]]:
    cpu_units = draw(st.integers(min_value=1, max_value=4))
    memory_units = draw(st.integers(min_value=1, max_value=4))
    gpu_count = draw(st.integers(min_value=0, max_value=2))
    capacity = ResourceVector(cpu_units * 500, memory_units * 1_024, gpu_count)
    gpu_uuids = tuple(f"gpu-{index}" for index in range(gpu_count))
    tenant_inputs = [
        (
            draw(st.sampled_from((1, 2, 4))),
            ResourceVector(
                draw(st.integers(min_value=0, max_value=capacity.cpu_millis)),
                draw(st.integers(min_value=0, max_value=capacity.memory_bytes)),
                draw(st.integers(min_value=0, max_value=capacity.gpu_count)),
            ),
            draw(st.integers(min_value=1, max_value=4)),
        )
        for _ in range(2)
    ]
    tenants = (
        TenantSpec("tenant-a", *tenant_inputs[0], 0),
        TenantSpec("tenant-b", *tenant_inputs[1], 1),
    )
    raw_jobs = draw(
        st.lists(
            st.tuples(
                st.sampled_from(("tenant-a", "tenant-b")),
                st.integers(min_value=0, max_value=500),
                st.integers(min_value=100, max_value=1_000),
                st.integers(min_value=0, max_value=capacity.cpu_millis),
                st.integers(min_value=0, max_value=capacity.memory_bytes),
                st.integers(min_value=0, max_value=capacity.gpu_count),
            ),
            min_size=1,
            max_size=10,
        )
    )
    jobs = tuple(
        JobSpec(
            job_id=f"job-{index}",
            tenant_id=tenant_id,
            ready_sequence=index,
            arrival_ms=arrival_ms,
            resources=ResourceVector(cpu_millis, memory_bytes, requested_gpus),
            duration_ms=duration_ms,
        )
        for index, (
            tenant_id,
            arrival_ms,
            duration_ms,
            cpu_millis,
            memory_bytes,
            requested_gpus,
        ) in enumerate(raw_jobs, start=1)
    )
    return capacity, tenants, jobs, gpu_uuids


def _assert_no_overlap_violation(result: SimulationResult) -> None:
    boundaries = sorted(
        {
            point
            for allocation in result.allocations
            for point in (allocation.start_ms, allocation.release_ms)
        }
    )
    capacity = result.trace.config.capacity
    tenants = {tenant.tenant_id: tenant for tenant in result.trace.config.tenants}
    for point in boundaries:
        active = [
            allocation
            for allocation in result.allocations
            if allocation.start_ms <= point < allocation.release_ms
        ]
        total = ResourceVector.zero()
        gpu_uuids: list[str] = []
        held_by_tenant = {tenant_id: ResourceVector.zero() for tenant_id in tenants}
        running_by_tenant = {tenant_id: 0 for tenant_id in tenants}
        for allocation in active:
            total = total.add(allocation.resources)
            gpu_uuids.extend(allocation.gpu_uuids)
            held_by_tenant[allocation.tenant_id] = held_by_tenant[allocation.tenant_id].add(
                allocation.resources
            )
            running_by_tenant[allocation.tenant_id] += 1
        assert total.fits_within(capacity)
        assert len(gpu_uuids) == len(set(gpu_uuids))
        for tenant_id, tenant in tenants.items():
            assert held_by_tenant[tenant_id].fits_within(tenant.quota)
            assert running_by_tenant[tenant_id] <= tenant.max_running_jobs


@given(finite_trace_inputs())
@settings(max_examples=40, deadline=None)
def test_all_baselines_preserve_capacity_gpu_release_and_terminal_invariants(
    trace_inputs: tuple[
        ResourceVector, tuple[TenantSpec, ...], tuple[JobSpec, ...], tuple[str, ...]
    ],
) -> None:
    capacity, tenants, jobs, gpu_uuids = trace_inputs
    trace = make_trace(jobs, capacity=capacity, tenants=tenants, gpu_uuids=gpu_uuids)

    for factory in POLICIES:
        result = Simulator().run(trace, factory())
        assert result.final_allocated == ResourceVector.zero()
        assert all(
            record.state in {JobState.COMPLETED, JobState.INFEASIBLE}
            for record in result.job_records
        )
        _assert_no_overlap_violation(result)


@pytest.mark.parametrize("factory", POLICIES)
def test_reusing_policy_instance_still_replays_identically(
    factory: Callable[[], SchedulerPolicy],
) -> None:
    capacity = ResourceVector(1_000, 1_000, 0)
    tenants = (
        TenantSpec("tenant-a", 1, capacity, 2, 0),
        TenantSpec("tenant-b", 2, capacity, 2, 1),
    )
    jobs = (
        JobSpec("a-1", "tenant-a", 1, 0, capacity, 100),
        JobSpec("b-1", "tenant-b", 2, 0, capacity, 100),
        JobSpec("a-2", "tenant-a", 3, 0, capacity, 100),
    )
    trace = make_trace(jobs, capacity=capacity, tenants=tenants)
    policy = factory()

    first = Simulator().run(trace, policy)
    second = Simulator().run(trace, policy)

    assert first.timeline == second.timeline
    assert first.allocations == second.allocations


@given(finite_trace_inputs())
@settings(max_examples=30, deadline=None)
def test_all_baselines_keep_identical_materialized_workload_and_config(
    trace_inputs: tuple[
        ResourceVector, tuple[TenantSpec, ...], tuple[JobSpec, ...], tuple[str, ...]
    ],
) -> None:
    capacity, tenants, jobs, gpu_uuids = trace_inputs
    trace = make_trace(jobs, capacity=capacity, tenants=tenants, gpu_uuids=gpu_uuids)

    results = [Simulator().run(trace, factory()) for factory in POLICIES]

    assert {result.trace.materialized_checksum for result in results} == {
        trace.materialized_checksum
    }
    assert {result.trace.config for result in results} == {trace.config}
    assert {result.trace.jobs for result in results} == {trace.jobs}
