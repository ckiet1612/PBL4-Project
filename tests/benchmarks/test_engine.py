from dataclasses import dataclass

import pytest

from benchmarks.simulator.baselines import FifoPolicy
from benchmarks.simulator.engine import AllocationLedger, InvariantViolation, Simulator
from benchmarks.simulator.model import JobSpec, JobState, ResourceVector, TenantSpec
from benchmarks.simulator.policy import SchedulingSnapshot
from tests.benchmarks.conftest import make_trace


def _job(
    job_id: str,
    *,
    tenant_id: str = "tenant-a",
    ready_sequence: int = 1,
    arrival_ms: int = 0,
    duration_ms: int = 1_000,
    cpu_millis: int = 1_000,
    memory_bytes: int = 1_024,
    gpu_count: int = 0,
) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        tenant_id=tenant_id,
        ready_sequence=ready_sequence,
        arrival_ms=arrival_ms,
        resources=ResourceVector(cpu_millis, memory_bytes, gpu_count),
        duration_ms=duration_ms,
    )


def test_engine_runs_arrival_dispatch_completion_and_release() -> None:
    result = Simulator().run(make_trace((_job("job-1"),)), FifoPolicy())

    record = result.job_records[0]
    assert record.state is JobState.COMPLETED
    assert record.dispatch_ms == 0
    assert record.completion_ms == 1_000
    assert [event.event_type for event in result.timeline] == [
        "arrival",
        "dispatch",
        "complete",
        "release",
    ]
    assert result.final_allocated == ResourceVector.zero()


def test_concurrency_is_derived_from_resources_not_queue_length() -> None:
    jobs = (
        _job("job-1", ready_sequence=1),
        _job("job-2", ready_sequence=2),
        _job("job-3", ready_sequence=3),
    )

    result = Simulator().run(make_trace(jobs), FifoPolicy())

    dispatches = {record.job_id: record.dispatch_ms for record in result.job_records}
    assert dispatches == {"job-1": 0, "job-2": 0, "job-3": 1_000}


def test_tenant_quota_is_enforced_independently_of_queue_length() -> None:
    capacity = ResourceVector(2_000, 4_096, 0)
    tenants = (
        TenantSpec("tenant-a", 1, ResourceVector(1_000, 4_096, 0), 4, 0),
        TenantSpec("tenant-b", 1, capacity, 4, 1),
    )
    jobs = (
        _job("a-1", ready_sequence=1),
        _job("a-2", ready_sequence=2),
        _job("b-1", tenant_id="tenant-b", ready_sequence=3),
    )

    result = Simulator().run(make_trace(jobs, capacity=capacity, tenants=tenants), FifoPolicy())

    dispatches = {record.job_id: record.dispatch_ms for record in result.job_records}
    assert dispatches == {"a-1": 0, "a-2": 1_000, "b-1": 0}


@pytest.mark.parametrize(
    ("job", "reason"),
    [
        (_job("too-large", cpu_millis=3_000), "request_exceeds_capacity"),
        (_job("over-quota", cpu_millis=1_500), "request_exceeds_tenant_quota"),
    ],
)
def test_permanently_infeasible_job_has_stable_reason(job: JobSpec, reason: str) -> None:
    tenant = TenantSpec("tenant-a", 1, ResourceVector(1_000, 4_096, 0), 1, 0)

    result = Simulator().run(make_trace((job,), tenants=(tenant,)), FifoPolicy())

    record = result.job_records[0]
    assert record.state is JobState.INFEASIBLE
    assert record.infeasible_reason == reason
    assert record.dispatch_ms is None


def test_allocation_ledger_rejects_capacity_overflow_and_double_release() -> None:
    ledger = AllocationLedger(ResourceVector(1_000, 1_024, 0), ())

    with pytest.raises(InvariantViolation, match="capacity"):
        ledger.allocate(_job("too-large", cpu_millis=2_000), now_ms=0)

    ledger.allocate(_job("job-1"), now_ms=0)
    ledger.release("job-1")
    with pytest.raises(InvariantViolation, match="not held"):
        ledger.release("job-1")


def test_allocation_ledger_assigns_gpu_once_and_reuses_after_release() -> None:
    ledger = AllocationLedger(ResourceVector(2_000, 2_048, 1), ("gpu-1",))
    first = ledger.allocate(_job("job-1", gpu_count=1), now_ms=0)

    assert first.gpu_uuids == ("gpu-1",)
    with pytest.raises(InvariantViolation, match="capacity"):
        ledger.allocate(_job("job-2", gpu_count=1), now_ms=0)

    ledger.release("job-1")
    assert ledger.allocate(_job("job-2", gpu_count=1), now_ms=1_000).gpu_uuids == ("gpu-1",)


@dataclass
class InvalidPolicy:
    name: str = "invalid"
    version: str = "test"

    def choose(self, snapshot: SchedulingSnapshot) -> str | None:
        return "not-in-snapshot"


def test_policy_selection_outside_snapshot_fails_run() -> None:
    with pytest.raises(InvariantViolation, match="eligible snapshot"):
        Simulator().run(make_trace((_job("job-1"),)), InvalidPolicy())
