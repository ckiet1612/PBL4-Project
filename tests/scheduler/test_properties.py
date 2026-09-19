from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from nexa.domain.scheduling import (
    AllocationState,
    Candidate,
    CandidateState,
    CandidateWindow,
    Dispatch,
    Err,
    HeldAllocation,
    Ok,
    ResourceCapacity,
    ResourceRequest,
    SchedulingSnapshot,
    TenantLedger,
    TenantPolicySnapshot,
)
from nexa.scheduler.accounting import advance_accounting
from nexa.scheduler.policy import WeightedDominantResourceTimePolicy


def _candidate(job_id: str, tenant_id: str, sequence: int, cpu: int) -> Candidate:
    return Candidate(
        job_id=job_id,
        tenant_id=tenant_id,
        user_id=f"user-{tenant_id}",
        job_version=1,
        ready_sequence=sequence,
        resources=ResourceRequest(cpu, 1, 0),
        base_priority=1,
        eligible_wait_seconds=0,
        retry_ready_at_ms=0,
        template_version=1,
        required_capabilities=frozenset({"cpu"}),
        tenant_active_attempts=0,
        user_active_attempts=0,
        state=CandidateState.QUEUED,
    )


def _snapshot(
    windows: tuple[tuple[str, CandidateWindow], ...],
    *,
    capacity: int,
    held: tuple[HeldAllocation, ...] = (),
    scores: tuple[tuple[str, int], ...] = (("tenant-a", 0), ("tenant-b", 0)),
    floor: int = 0,
) -> SchedulingSnapshot:
    return SchedulingSnapshot(
        policy_version=1,
        allocatable_capacity=ResourceCapacity(capacity, capacity, 0),
        held_allocations=held,
        tenant_ledgers=tuple(
            TenantLedger(tenant_id, Decimal(score), 0, True) for tenant_id, score in scores
        ),
        tenant_limits=tuple(
            TenantPolicySnapshot(
                tenant_id,
                Decimal(1),
                ResourceCapacity(capacity * 2, capacity * 2, 0),
                4,
                4,
            )
            for tenant_id, _ in scores
        ),
        candidates_by_tenant=windows,
        active_reservation=None,
        virtual_floor=Decimal(floor),
    )


@given(
    score_a=st.integers(min_value=0, max_value=100),
    score_b=st.integers(min_value=0, max_value=100),
    reverse_windows=st.booleans(),
    reverse_candidates=st.booleans(),
)
def test_policy_result_is_independent_of_input_iteration_order(
    score_a: int,
    score_b: int,
    reverse_windows: bool,
    reverse_candidates: bool,
) -> None:
    canonical_a = (
        _candidate("a-1", "tenant-a", 1, 1),
        _candidate("a-2", "tenant-a", 2, 1),
    )
    canonical_b = (
        _candidate("b-1", "tenant-b", 3, 1),
        _candidate("b-2", "tenant-b", 4, 1),
    )
    candidates_a = tuple(reversed(canonical_a)) if reverse_candidates else canonical_a
    candidates_b = tuple(reversed(canonical_b)) if reverse_candidates else canonical_b
    windows = (
        ("tenant-a", CandidateWindow(candidates_a, candidates_a[0], None)),
        ("tenant-b", CandidateWindow(candidates_b, candidates_b[0], None)),
    )
    if reverse_windows:
        windows = tuple(reversed(windows))
    snapshot = _snapshot(
        windows,
        capacity=4,
        scores=(("tenant-a", score_a), ("tenant-b", score_b)),
    )
    canonical = _snapshot(
        (
            ("tenant-a", CandidateWindow(canonical_a, canonical_a[0], None)),
            ("tenant-b", CandidateWindow(canonical_b, canonical_b[0], None)),
        ),
        capacity=4,
        scores=(("tenant-a", score_a), ("tenant-b", score_b)),
    )

    assert WeightedDominantResourceTimePolicy().decide(
        snapshot, 0
    ) == WeightedDominantResourceTimePolicy().decide(canonical, 0)


@given(
    capacity=st.integers(min_value=2, max_value=8),
    held_cpu=st.integers(min_value=0, max_value=7),
    requested_cpu=st.integers(min_value=1, max_value=8),
)
def test_dispatch_never_exceeds_current_free_capacity(
    capacity: int, held_cpu: int, requested_cpu: int
) -> None:
    held_cpu = min(held_cpu, capacity)
    requested_cpu = min(requested_cpu, capacity)
    held = ()
    if held_cpu:
        held = (
            HeldAllocation(
                "held",
                "tenant-b",
                ResourceRequest(held_cpu, 1, 0),
                (),
                AllocationState.HELD,
            ),
        )
    candidate = _candidate("candidate", "tenant-a", 1, requested_cpu)
    snapshot = _snapshot(
        (
            ("tenant-a", CandidateWindow((candidate,), candidate, None)),
            ("tenant-b", CandidateWindow((), None, None)),
        ),
        capacity=capacity,
        held=held,
    )

    result = WeightedDominantResourceTimePolicy().decide(snapshot, 0)

    assert isinstance(result, Ok)
    if isinstance(result.value, Dispatch):
        assert requested_cpu <= capacity - held_cpu
        assert result.value.job_id == "candidate"


@given(
    initial_floor=st.integers(min_value=0, max_value=100),
    score=st.integers(min_value=0, max_value=100),
    elapsed_ms=st.integers(min_value=0, max_value=10_000),
)
def test_accounting_score_and_floor_never_decrease(
    initial_floor: int, score: int, elapsed_ms: int
) -> None:
    candidate = _candidate("candidate", "tenant-a", 1, 1)
    snapshot = _snapshot(
        (("tenant-a", CandidateWindow((candidate,), candidate, None)),),
        capacity=4,
        scores=(("tenant-a", score),),
        floor=initial_floor,
    )

    result = advance_accounting(snapshot, elapsed_ms)

    assert not isinstance(result, Err)
    assert result.value.tenant_ledgers[0].virtual_score >= Decimal(score)
    assert result.value.virtual_floor >= Decimal(initial_floor)


def test_new_policy_instance_reconstructs_the_same_decision_from_snapshot() -> None:
    candidate = _candidate("candidate", "tenant-a", 1, 1)
    snapshot = _snapshot(
        (("tenant-a", CandidateWindow((candidate,), candidate, b"cursor")),),
        capacity=4,
        scores=(("tenant-a", 3),),
        floor=2,
    )

    first = WeightedDominantResourceTimePolicy().decide(snapshot, 0)
    reconstructed = WeightedDominantResourceTimePolicy().decide(snapshot, 0)

    assert first == reconstructed
