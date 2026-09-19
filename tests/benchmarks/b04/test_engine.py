from decimal import Decimal
from pathlib import Path

from benchmarks.b04.cli import BASELINES
from benchmarks.b04.engine import ProductPolicySimulator
from benchmarks.simulator.engine import Simulator
from benchmarks.simulator.model import JobSpec, ResourceVector, SimulationConfig, TenantSpec
from benchmarks.simulator.trace import (
    FairnessWindow,
    MaterializedTrace,
    load_trace,
    materialize_trace,
)
from nexa.scheduler.policy import WeightedDominantResourceTimePolicy


def _trace(
    jobs: tuple[JobSpec, ...],
    *,
    capacity: ResourceVector,
    tenants: tuple[TenantSpec, ...],
    gpu_uuids: tuple[str, ...] = (),
) -> MaterializedTrace:
    return MaterializedTrace(
        version=1,
        trace_id="b04-test",
        seed=7,
        config=SimulationConfig(capacity, gpu_uuids, tenants),
        fairness_window=FairnessWindow(0, 500_000, tuple(t.tenant_id for t in tenants)),
        jobs=jobs,
        trace_checksum="sha256:" + "1" * 64,
        materialized_checksum="sha256:" + "2" * 64,
    )


def _job(
    job_id: str,
    tenant_id: str,
    sequence: int,
    arrival_ms: int,
    cpu: int,
    duration_ms: int,
    *,
    gpu: int = 0,
    priority: int = 1,
) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        tenant_id=tenant_id,
        ready_sequence=sequence,
        arrival_ms=arrival_ms,
        resources=ResourceVector(cpu, 1, gpu),
        duration_ms=duration_ms,
        priority=priority,
    )


def test_engine_reservation_drains_while_small_jobs_keep_arriving() -> None:
    fixture = Path(__file__).resolve().parents[3] / "benchmarks/fixtures/b04-large-reservation.json"
    trace = materialize_trace(load_trace(fixture), 7)

    result = ProductPolicySimulator().run(trace, WeightedDominantResourceTimePolicy())

    created = next(
        event
        for event in result.timeline
        if event.event_type == "reservation_created" and event.job_id == "large"
    )
    large_record = next(record for record in result.job_records if record.job_id == "large")
    assert created.time_ms == 121_000
    assert large_record.dispatch_ms == 190_000

    draining = [
        decision
        for decision in result.decision_timeline
        if decision.decision_type == "drain_for_reservation" and decision.job_id == "large"
    ]
    assert any(
        candidate.job_id != "large"
        and candidate.eligibility_reason is None
        and candidate.fits_free_resources
        for decision in draining
        for tenant in decision.tenant_state
        for candidate in tenant.candidates
    )
    assert {
        allocation.release_ms
        for allocation in result.allocations
        if allocation.start_ms == 0
        and allocation.job_id.startswith("small-")
        and created.time_ms < allocation.release_ms <= large_record.dispatch_ms
    } == {130_000, 150_000, 170_000, 190_000}
    assert any(job.arrival_ms > large_record.dispatch_ms for job in trace.jobs)

    for baseline_factory in BASELINES.values():
        baseline = Simulator().run(trace, baseline_factory())
        baseline_large = next(record for record in baseline.job_records if record.job_id == "large")
        assert baseline_large.dispatch_ms is not None
        assert baseline_large.dispatch_ms > large_record.dispatch_ms


def test_engine_preserves_reserved_candidate_when_quota_release_reorders_window() -> None:
    tenants = (
        TenantSpec("target", 1, ResourceVector(8, 64, 0), 2, 0),
        TenantSpec("blocker", 1, ResourceVector(8, 64, 0), 1, 1),
    )
    jobs = (
        _job("target-running", "target", 0, 0, 4, 130_000),
        _job("blocker-running", "blocker", 1, 0, 4, 130_000),
        *(
            _job(
                f"old-{index:02d}",
                "target",
                10 + index,
                1,
                8,
                1_000,
                priority=2,
            )
            for index in range(16)
        ),
        _job("protected", "target", 30, 1, 4, 1_000),
    )
    trace = _trace(jobs, capacity=ResourceVector(8, 64, 0), tenants=tenants)

    result = ProductPolicySimulator().run(trace, WeightedDominantResourceTimePolicy())

    created = next(
        event
        for event in result.timeline
        if event.event_type == "reservation_created" and event.job_id == "protected"
    )
    assert created.time_ms == 120_001
    assert created.reservation_id is not None
    assert not any(
        event.event_type == "reservation_invalidated"
        and event.reservation_id == created.reservation_id
        for event in result.timeline
    )
    dispatched = next(
        event
        for event in result.timeline
        if event.event_type == "dispatch" and event.reservation_id == created.reservation_id
    )
    assert dispatched.job_id == "protected"
    assert dispatched.time_ms == 130_000

    decision = next(
        item
        for item in result.decision_timeline
        if item.decision_type == "dispatch" and item.reservation_id == created.reservation_id
    )
    target_state = next(item for item in decision.tenant_state if item.tenant_id == "target")
    assert len(target_state.normal_candidate_ids) == 16
    assert target_state.oldest_eligible_job_id == "protected"


def test_engine_accounts_each_interval_once_at_tick_and_release_boundaries() -> None:
    tenants = (TenantSpec("tenant-a", 1, ResourceVector(4, 4, 0), 1, 0),)
    jobs = (_job("job", "tenant-a", 1, 0, 4, 2500),)
    trace = _trace(jobs, capacity=ResourceVector(4, 4, 0), tenants=tenants)

    result = ProductPolicySimulator().run(trace, WeightedDominantResourceTimePolicy())

    assert result.final_tenant_ledgers[0].virtual_score.as_tuple().exponent >= -50
    assert result.final_tenant_ledgers[0].virtual_score == 2.5
    assert [
        event.time_ms for event in result.timeline if event.event_type == "accounting_tick"
    ] == [
        1000,
        2000,
        2500,
    ]


def test_accounting_keeps_tenant_active_when_newer_request_fits_remaining_quota() -> None:
    tenants = (
        TenantSpec("tenant-a", 1, ResourceVector(4, 4, 0), 2, 0),
        TenantSpec("tenant-b", 1, ResourceVector(4, 4, 0), 1, 1),
        TenantSpec("tenant-c", 1, ResourceVector(4, 4, 0), 1, 2),
    )
    jobs = (
        _job("a-running", "tenant-a", 0, 0, 3, 3000),
        _job("b-running", "tenant-b", 1, 0, 1, 3000),
        _job("a-old-blocked", "tenant-a", 2, 0, 2, 1000),
        _job("a-later-eligible", "tenant-a", 3, 0, 1, 1000),
        _job("c-new", "tenant-c", 4, 3000, 1, 1000),
    )
    trace = _trace(jobs, capacity=ResourceVector(4, 4, 0), tenants=tenants)

    result = ProductPolicySimulator().run(trace, WeightedDominantResourceTimePolicy())

    first_tick = next(
        record for record in result.accounting_timeline if record.interval_end_ms == 1000
    )
    assert first_tick.eligible_tenant_ids == ("tenant-a",)
    ledgers = {ledger.tenant_id: ledger for ledger in first_tick.tenant_ledgers}
    assert ledgers["tenant-a"].had_eligible_demand
    assert first_tick.virtual_floor == Decimal("0.75")
    dispatches_at_release = [
        event.job_id
        for event in result.timeline
        if event.event_type == "dispatch" and event.time_ms == 3000
    ]
    assert dispatches_at_release[0] == "a-old-blocked"


def test_engine_keeps_one_global_tick_stream_across_jittered_arrivals() -> None:
    tenants = (TenantSpec("tenant-a", 1, ResourceVector(4, 4, 0), 4, 0),)
    jobs = (
        _job("job-0", "tenant-a", 0, 0, 1, 3000),
        _job("job-1", "tenant-a", 1, 1, 1, 3000),
        _job("job-2", "tenant-a", 2, 2, 1, 3000),
    )
    trace = _trace(jobs, capacity=ResourceVector(4, 4, 0), tenants=tenants)

    result = ProductPolicySimulator().run(trace, WeightedDominantResourceTimePolicy())

    assert [
        event.time_ms for event in result.timeline if event.event_type == "accounting_tick"
    ] == [1, 2, 1000, 2000, 3000, 3001, 3002]


def test_engine_never_oversubscribes_or_reuses_a_held_gpu_uuid() -> None:
    tenants = (
        TenantSpec("tenant-a", 1, ResourceVector(4, 4, 1), 2, 0),
        TenantSpec("tenant-b", 1, ResourceVector(4, 4, 1), 2, 1),
    )
    jobs = (
        _job("gpu-a", "tenant-a", 1, 0, 1, 5000, gpu=1),
        _job("gpu-b", "tenant-b", 2, 0, 1, 5000, gpu=1),
    )
    trace = _trace(
        jobs,
        capacity=ResourceVector(2, 2, 1),
        tenants=tenants,
        gpu_uuids=("GPU-a",),
    )

    result = ProductPolicySimulator().run(trace, WeightedDominantResourceTimePolicy())

    assert not result.invariant_violations
    first, second = result.allocations
    assert first.gpu_uuids == second.gpu_uuids == ("GPU-a",)
    assert first.release_ms <= second.start_ms


def test_concurrency_blocked_time_does_not_count_as_eligible_wait() -> None:
    tenants = (TenantSpec("tenant-a", 1, ResourceVector(1, 2, 0), 1, 0),)
    jobs = (
        _job("running", "tenant-a", 0, 0, 1, 200_000),
        _job("blocked", "tenant-a", 1, 0, 1, 1_000),
    )
    trace = _trace(jobs, capacity=ResourceVector(1, 2, 0), tenants=tenants)

    result = ProductPolicySimulator().run(trace, WeightedDominantResourceTimePolicy())

    blocked_dispatch = next(
        event
        for event in result.timeline
        if event.event_type == "dispatch" and event.job_id == "blocked"
    )
    assert blocked_dispatch.time_ms == 200_000
    assert not any(
        event.event_type == "reservation_created" and event.job_id == "blocked"
        for event in result.timeline
    )
    decision = next(
        item
        for item in result.decision_timeline
        if item.decision_type == "dispatch" and item.job_id == "blocked"
    )
    selected = next(
        candidate
        for tenant in decision.tenant_state
        for candidate in tenant.candidates
        if candidate.job_id == "blocked"
    )
    assert selected.eligible_wait_seconds == 0


def test_eligible_wait_pauses_and_resumes_before_reservation_boundary() -> None:
    tenants = (
        TenantSpec("blocker-tenant", 1, ResourceVector(4, 4, 0), 1, 0),
        TenantSpec("target-tenant", 1, ResourceVector(4, 4, 0), 1, 1),
    )
    jobs = (
        _job("blocker", "blocker-tenant", 0, 0, 2, 200_000),
        _job("large", "target-tenant", 1, 0, 4, 10_000),
        _job("small", "target-tenant", 2, 30_000, 1, 30_000),
    )
    trace = _trace(jobs, capacity=ResourceVector(4, 4, 0), tenants=tenants)

    result = ProductPolicySimulator().run(trace, WeightedDominantResourceTimePolicy())

    reservation = next(
        item
        for item in result.decision_timeline
        if item.decision_type == "create_reservation" and item.job_id == "large"
    )
    assert reservation.time_ms == 150_000
    assert reservation.reason == "eligible_wait_threshold"
    selected = next(
        candidate
        for tenant in reservation.tenant_state
        for candidate in tenant.candidates
        if candidate.job_id == "large"
    )
    assert selected.eligible_wait_seconds == 120
