from benchmarks.simulator.baselines import FifoPolicy
from benchmarks.simulator.engine import Simulator
from benchmarks.simulator.model import JobSpec, ResourceVector
from tests.benchmarks.conftest import make_trace


def _job(job_id: str, ready_sequence: int, cpu_millis: int, duration_ms: int) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        tenant_id="tenant-a",
        ready_sequence=ready_sequence,
        arrival_ms=0,
        resources=ResourceVector(cpu_millis, 1_024, 0),
        duration_ms=duration_ms,
    )


def test_fifo_selects_oldest_fitting_job() -> None:
    jobs = (
        _job("old-large", 1, 2_000, 2_000),
        _job("old-small", 2, 1_000, 2_000),
        _job("young-small", 3, 1_000, 2_000),
    )

    result = Simulator().run(make_trace(jobs), FifoPolicy())

    assert [event.job_id for event in result.timeline if event.event_type == "dispatch"] == [
        "old-large",
        "old-small",
        "young-small",
    ]


def test_fifo_skips_temporarily_nonfitting_head_without_marking_it_infeasible() -> None:
    jobs = (
        _job("holder", 1, 1_000, 2_000),
        _job("blocked-head", 2, 2_000, 1_000),
        _job("fitting-tail", 3, 1_000, 1_000),
    )

    result = Simulator().run(make_trace(jobs), FifoPolicy())
    records = {record.job_id: record for record in result.job_records}

    assert records["fitting-tail"].dispatch_ms == 0
    assert records["blocked-head"].dispatch_ms == 2_000
    assert records["blocked-head"].infeasible_reason is None
