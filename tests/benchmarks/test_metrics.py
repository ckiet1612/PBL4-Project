from fractions import Fraction

from benchmarks.simulator import SIMULATOR_VERSION
from benchmarks.simulator.baselines import FifoPolicy
from benchmarks.simulator.engine import Simulator
from benchmarks.simulator.metrics import (
    assess_fairness_window,
    build_raw_result,
    dominant_resource_time,
    nearest_rank,
    weighted_jain,
)
from benchmarks.simulator.model import Allocation, JobSpec, ResourceVector, TenantSpec
from benchmarks.simulator.trace import FairnessWindow
from tests.benchmarks.conftest import make_trace


def _job(
    job_id: str,
    tenant_id: str,
    ready_sequence: int,
    *,
    arrival_ms: int = 0,
    cpu_millis: int = 1_000,
    duration_ms: int = 1_000,
) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        tenant_id=tenant_id,
        ready_sequence=ready_sequence,
        arrival_ms=arrival_ms,
        resources=ResourceVector(cpu_millis, 0, 0),
        duration_ms=duration_ms,
    )


def test_nearest_rank_percentiles_use_hand_checked_positions() -> None:
    assert nearest_rank([0, 1_000], 50) == 0
    assert nearest_rank([0, 1_000], 95) == 1_000


def test_dominant_resource_time_clips_allocations_to_window() -> None:
    allocations = (
        Allocation("a-1", "tenant-a", ResourceVector(1_000, 0, 0), (), 0, 1_000),
        Allocation("b-1", "tenant-b", ResourceVector(500, 0, 0), (), 500, 2_500),
    )

    measured = dominant_resource_time(
        allocations,
        ResourceVector(1_000, 1_000, 0),
        start_ms=500,
        end_ms=2_000,
    )

    assert measured == {"tenant-a": Fraction(500), "tenant-b": Fraction(750)}


def test_dominant_resource_time_aggregates_tenant_resources_before_taking_max() -> None:
    allocations = (
        Allocation("a-cpu", "tenant-a", ResourceVector(500, 0, 0), (), 0, 1_000),
        Allocation("a-memory", "tenant-a", ResourceVector(0, 500, 0), (), 0, 1_000),
    )

    measured = dominant_resource_time(
        allocations,
        ResourceVector(1_000, 1_000, 0),
        start_ms=0,
        end_ms=1_000,
    )

    assert measured == {"tenant-a": Fraction(500)}


def test_weighted_jain_uses_resource_time_divided_by_weight() -> None:
    assert weighted_jain(
        {"tenant-a": Fraction(2), "tenant-b": Fraction(4)},
        {"tenant-a": 1, "tenant-b": 2},
    ) == Fraction(1)
    assert weighted_jain(
        {"tenant-a": Fraction(1), "tenant-b": Fraction(3)},
        {"tenant-a": 1, "tenant-b": 1},
    ) == Fraction(4, 5)


def test_wait_time_throughput_and_completion_metrics_match_small_trace() -> None:
    jobs = (_job("a-1", "tenant-a", 1), _job("b-1", "tenant-b", 2))
    result = Simulator().run(
        make_trace(jobs, capacity=ResourceVector(1_000, 1_000, 0)), FifoPolicy()
    )

    raw = build_raw_result(result)

    waits = {job["job_id"]: job["wait_ms"] for job in raw["jobs"]}
    assert waits == {"a-1": 0, "b-1": 1_000}
    assert raw["metrics"]["max_wait_ms"] == 1_000
    assert raw["metrics"]["p50_wait_ms"] == 0
    assert raw["metrics"]["p95_wait_ms"] == 1_000
    assert raw["metrics"]["throughput_jobs_per_second"] == "1"
    assert raw["metrics"]["simulation_runtime_ms"] == 2_000


def test_fairness_window_excludes_non_continuous_and_quota_blocked_tenants() -> None:
    capacity = ResourceVector(1_000, 1_000, 0)
    tenants = (
        TenantSpec("tenant-a", 1, capacity, 2, 0),
        TenantSpec("tenant-b", 1, capacity, 2, 1),
        TenantSpec("tenant-c", 1, ResourceVector(100, 1_000, 0), 2, 2),
    )
    jobs = (
        _job("a-1", "tenant-a", 1, duration_ms=2_000),
        _job("b-1", "tenant-b", 2, arrival_ms=500),
        _job("c-1", "tenant-c", 3, cpu_millis=500),
    )
    trace = make_trace(
        jobs,
        capacity=capacity,
        tenants=tenants,
        fairness_window=FairnessWindow(0, 1_000, ("tenant-a", "tenant-b", "tenant-c")),
    )
    result = Simulator().run(trace, FifoPolicy())

    assessment = assess_fairness_window(result)

    assert assessment.included_tenants == ("tenant-a",)
    assert assessment.excluded_tenants == {
        "tenant-b": "not_continuously_demanding",
        "tenant-c": "quota_does_not_permit_window_workload",
    }
    assert assessment.weighted_jain is None


def test_fairness_window_excludes_quota_that_cannot_reach_weighted_share() -> None:
    capacity = ResourceVector(4_000, 1_000, 0)
    tenants = (
        TenantSpec("tenant-a", 1, ResourceVector(1_000, 1_000, 0), 4, 0),
        TenantSpec("tenant-b", 1, capacity, 4, 1),
    )
    jobs = (
        _job("a-1", "tenant-a", 1, cpu_millis=1_000, duration_ms=2_000),
        _job("a-2", "tenant-a", 2, cpu_millis=1_000, duration_ms=2_000),
        _job("b-1", "tenant-b", 3, cpu_millis=3_000, duration_ms=2_000),
    )
    trace = make_trace(
        jobs,
        capacity=capacity,
        tenants=tenants,
        fairness_window=FairnessWindow(0, 1_000, ("tenant-a", "tenant-b")),
    )

    assessment = assess_fairness_window(Simulator().run(trace, FifoPolicy()))

    assert assessment.included_tenants == ("tenant-b",)
    assert assessment.excluded_tenants == {
        "tenant-a": "quota_or_concurrency_does_not_permit_weighted_share"
    }
    assert assessment.weighted_jain is None


def test_fairness_window_rechecks_weighted_share_after_each_exclusion() -> None:
    capacity = ResourceVector(3_000, 1_000, 0)
    tenants = (
        TenantSpec("tenant-a", 1, ResourceVector(500, 1_000, 0), 4, 0),
        TenantSpec("tenant-b", 1, ResourceVector(1_000, 1_000, 0), 4, 1),
        TenantSpec("tenant-c", 1, capacity, 4, 2),
    )
    jobs = tuple(
        _job(
            f"{tenant_id}-{index}",
            tenant_id,
            tenant_order * 100 + index,
            cpu_millis=500,
        )
        for tenant_order, tenant_id in enumerate(("tenant-a", "tenant-b", "tenant-c"))
        for index in range(10)
    )
    trace = make_trace(
        jobs,
        capacity=capacity,
        tenants=tenants,
        fairness_window=FairnessWindow(
            0,
            1_000,
            ("tenant-a", "tenant-b", "tenant-c"),
        ),
    )

    assessment = assess_fairness_window(Simulator().run(trace, FifoPolicy()))

    assert assessment.included_tenants == ("tenant-c",)
    assert assessment.excluded_tenants == {
        "tenant-a": "quota_or_concurrency_does_not_permit_weighted_share",
        "tenant-b": "quota_or_concurrency_does_not_permit_weighted_share",
    }
    assert assessment.weighted_jain is None


def test_fairness_window_ignores_quota_infeasible_job_arriving_after_window() -> None:
    capacity = ResourceVector(1_000, 1_000, 0)
    tenants = (
        TenantSpec("tenant-a", 1, capacity, 2, 0),
        TenantSpec("tenant-b", 1, capacity, 2, 1),
    )
    jobs = (
        _job("a-window", "tenant-a", 1, cpu_millis=500, duration_ms=1_000),
        _job("b-window", "tenant-b", 2, cpu_millis=500, duration_ms=1_000),
        _job(
            "a-late-over-quota",
            "tenant-a",
            3,
            arrival_ms=5_000,
            cpu_millis=1_500,
            duration_ms=1_000,
        ),
    )
    trace = make_trace(
        jobs,
        capacity=capacity,
        tenants=tenants,
        fairness_window=FairnessWindow(0, 1_000, ("tenant-a", "tenant-b")),
    )

    assessment = assess_fairness_window(Simulator().run(trace, FifoPolicy()))

    assert assessment.included_tenants == ("tenant-a", "tenant-b")
    assert assessment.excluded_tenants == {}
    assert assessment.weighted_jain == 1


def test_fairness_window_ignores_quota_infeasible_job_before_window() -> None:
    capacity = ResourceVector(1_000, 1_000, 0)
    tenants = (
        TenantSpec("tenant-a", 1, ResourceVector(500, 1_000, 0), 2, 0),
        TenantSpec("tenant-b", 1, capacity, 2, 1),
    )
    jobs = (
        _job("a-old-over-quota", "tenant-a", 1, cpu_millis=600),
        _job("a-window", "tenant-a", 2, arrival_ms=5_000, cpu_millis=500),
        _job("b-window", "tenant-b", 3, arrival_ms=5_000, cpu_millis=500),
    )
    trace = make_trace(
        jobs,
        capacity=capacity,
        tenants=tenants,
        fairness_window=FairnessWindow(5_000, 6_000, ("tenant-a", "tenant-b")),
    )

    assessment = assess_fairness_window(Simulator().run(trace, FifoPolicy()))

    assert assessment.included_tenants == ("tenant-a", "tenant-b")
    assert assessment.excluded_tenants == {}
    assert assessment.weighted_jain == 1


def test_raw_result_contains_required_provenance_counts_and_timelines() -> None:
    result = Simulator().run(make_trace((_job("a-1", "tenant-a", 1),)), FifoPolicy())

    raw = build_raw_result(result)

    assert raw["schema_version"] == 1
    assert raw["simulator_version"] == SIMULATOR_VERSION
    assert raw["baseline"] == {
        "name": "fifo",
        "version": "1",
        "cost": {"policy_decisions": 1, "candidate_evaluations": 1},
    }
    assert raw["provenance"] == {
        "seed": 1,
        "trace_id": "test-trace",
        "trace_checksum": "sha256:" + "1" * 64,
        "materialized_checksum": "sha256:" + "2" * 64,
    }
    assert raw["counts"] == {
        "arrived": 1,
        "accepted": 1,
        "rejected": 0,
        "completed": 1,
        "infeasible": 0,
    }
    assert len(raw["dispatch_timeline"]) == 1
    assert len(raw["allocation_timeline"]) == 1
    assert raw["invariant_violations"] == []
    assert raw["starvation"]["undispatched_job_ids"] == []
    assert raw["metrics"]["event_count"] == 4


def test_raw_event_timeline_preserves_global_sequence_order() -> None:
    jobs = (_job("a-1", "tenant-a", 1), _job("b-1", "tenant-b", 2))
    result = Simulator().run(
        make_trace(jobs, capacity=ResourceVector(1_000, 1_000, 0)), FifoPolicy()
    )

    raw = build_raw_result(result)

    assert [event["sequence"] for event in raw["event_timeline"]] == list(
        range(len(result.timeline))
    )
