from decimal import Decimal

import pytest

from nexa.domain.scheduling import (
    AllocationState,
    Candidate,
    CandidateBoundExceeded,
    CandidateState,
    CandidateWindow,
    Dispatch,
    Err,
    HeldAllocation,
    InvalidSnapshot,
    NoDecision,
    Ok,
    ResourceCapacity,
    ResourceRequest,
    SchedulingSnapshot,
    TenantLedger,
    TenantPolicySnapshot,
)
from nexa.scheduler.policy import WeightedDominantResourceTimePolicy, effective_priority

_DEFAULT_CAPACITY = ResourceCapacity(8, 8, 1)


def _candidate(
    job_id: str,
    tenant_id: str,
    sequence: int,
    *,
    user_id: str | None = None,
    cpu: int = 1,
    memory: int = 1,
    gpu: int = 0,
    priority: int = 1,
    age: int = 0,
    retry_ready_at_ms: int = 0,
    tenant_active: int = 0,
    user_active: int = 0,
    state: CandidateState = CandidateState.QUEUED,
    template_compatible: bool = True,
    capabilities_compatible: bool = True,
    policy_compatible: bool = True,
) -> Candidate:
    return Candidate(
        job_id=job_id,
        tenant_id=tenant_id,
        user_id=user_id or f"user-{tenant_id}",
        job_version=sequence + 10,
        ready_sequence=sequence,
        resources=ResourceRequest(cpu, memory, gpu),
        base_priority=priority,
        eligible_wait_seconds=age,
        retry_ready_at_ms=retry_ready_at_ms,
        template_version=1,
        required_capabilities=frozenset({"cuda"}) if gpu else frozenset({"cpu"}),
        tenant_active_attempts=tenant_active,
        user_active_attempts=user_active,
        state=state,
        template_compatible=template_compatible,
        capabilities_compatible=capabilities_compatible,
        policy_compatible=policy_compatible,
    )


def _limit(
    tenant_id: str,
    *,
    weight: str = "1",
    quota: ResourceCapacity = _DEFAULT_CAPACITY,
    tenant_concurrency: int = 2,
    user_concurrency: int = 1,
) -> TenantPolicySnapshot:
    return TenantPolicySnapshot(
        tenant_id=tenant_id,
        weight=Decimal(weight),
        resource_quota=quota,
        max_active_attempts=tenant_concurrency,
        max_user_active_attempts=user_concurrency,
    )


def _snapshot(
    windows: tuple[tuple[str, CandidateWindow], ...],
    *,
    capacity: ResourceCapacity = _DEFAULT_CAPACITY,
    allocations: tuple[HeldAllocation, ...] = (),
    ledgers: tuple[TenantLedger, ...] | None = None,
    limits: tuple[TenantPolicySnapshot, ...] | None = None,
    policy_version: int = 7,
    floor: str = "0",
) -> SchedulingSnapshot:
    tenant_ids = tuple(tenant_id for tenant_id, _ in windows)
    if ledgers is None:
        ledgers = tuple(TenantLedger(tenant_id, Decimal(0), 0, True) for tenant_id in tenant_ids)
    if limits is None:
        limits = tuple(_limit(tenant_id) for tenant_id in tenant_ids)
    return SchedulingSnapshot(
        policy_version=policy_version,
        allocatable_capacity=capacity,
        held_allocations=allocations,
        tenant_ledgers=ledgers,
        tenant_limits=limits,
        candidates_by_tenant=windows,
        active_reservation=None,
        virtual_floor=Decimal(floor),
    )


def _window(*candidates: Candidate, oldest: Candidate | None = None) -> CandidateWindow:
    return CandidateWindow(tuple(candidates), oldest, None)


def _dispatch_job(snapshot: SchedulingSnapshot, now_ms: int = 0) -> Dispatch:
    result = WeightedDominantResourceTimePolicy().decide(snapshot, now_ms)
    assert isinstance(result, Ok)
    assert isinstance(result.value, Dispatch)
    return result.value


@pytest.mark.parametrize(
    ("age", "expected"),
    [(59, 0), (60, 1), (61, 1), (119, 1), (120, 2), (121, 2), (999, 2)],
)
def test_effective_priority_uses_eligible_age_boundaries(age: int, expected: int) -> None:
    assert effective_priority(_candidate("job", "tenant", 1, priority=0, age=age)) == expected


def test_job_order_is_priority_then_ready_sequence_then_job_id() -> None:
    low = _candidate("job-low", "tenant-a", 1, priority=0)
    later = _candidate("job-z", "tenant-a", 3, priority=1)
    lexical_later = _candidate("job-b", "tenant-a", 2, priority=1)
    lexical_first = _candidate("job-a", "tenant-a", 2, priority=1)
    snapshot = _snapshot((("tenant-a", _window(low, later, lexical_later, lexical_first)),))

    assert _dispatch_job(snapshot).job_id == "job-a"


def test_aging_changes_only_order_within_the_selected_tenant() -> None:
    aged = _candidate("aged", "tenant-a", 2, priority=0, age=60)
    fresh = _candidate("fresh", "tenant-a", 1, priority=0, age=0)
    other = _candidate("other", "tenant-b", 0, priority=2, age=999)
    snapshot = _snapshot(
        (("tenant-a", _window(fresh, aged)), ("tenant-b", _window(other))),
        ledgers=(
            TenantLedger("tenant-a", Decimal(0), 0, True),
            TenantLedger("tenant-b", Decimal(5), 0, True),
        ),
    )

    assert _dispatch_job(snapshot).job_id == "aged"


def test_tenant_order_uses_virtual_score_before_other_keys() -> None:
    a = _candidate("a", "tenant-a", 1)
    b = _candidate("b", "tenant-b", 2)
    snapshot = _snapshot(
        (("tenant-a", _window(a)), ("tenant-b", _window(b))),
        ledgers=(
            TenantLedger("tenant-a", Decimal(2), 0, True),
            TenantLedger("tenant-b", Decimal(1), 0, True),
        ),
    )

    assert _dispatch_job(snapshot).job_id == "b"


def test_tenant_order_uses_dominant_share_over_weight_as_second_key() -> None:
    a = _candidate("a", "tenant-a", 1)
    b = _candidate("b", "tenant-b", 2)
    snapshot = _snapshot(
        (("tenant-a", _window(a)), ("tenant-b", _window(b))),
        allocations=(
            HeldAllocation(
                "allocation-a",
                "tenant-a",
                ResourceRequest(4, 1, 0),
                (),
                AllocationState.HELD,
            ),
            HeldAllocation(
                "allocation-b",
                "tenant-b",
                ResourceRequest(2, 1, 0),
                (),
                AllocationState.HELD,
            ),
        ),
        limits=(_limit("tenant-a", weight="1"), _limit("tenant-b", weight="1")),
    )

    assert _dispatch_job(snapshot).job_id == "b"


def test_tenant_order_uses_oldest_sequence_then_tenant_id() -> None:
    later = _candidate("later", "tenant-a", 3)
    older = _candidate("older", "tenant-b", 2)
    snapshot = _snapshot((("tenant-a", _window(later)), ("tenant-b", _window(older))))
    assert _dispatch_job(snapshot).job_id == "older"

    same_a = _candidate("same-a", "tenant-a", 5)
    same_b = _candidate("same-b", "tenant-b", 5)
    tied = _snapshot(
        (("tenant-b", _window(same_b)), ("tenant-a", _window(same_a))),
        ledgers=(
            TenantLedger("tenant-b", Decimal(0), 0, True),
            TenantLedger("tenant-a", Decimal(0), 0, True),
        ),
        limits=(_limit("tenant-b"), _limit("tenant-a")),
    )
    assert _dispatch_job(tied).job_id == "same-a"


@pytest.mark.parametrize(
    "blocked",
    [
        _candidate("retry", "tenant-a", 1, retry_ready_at_ms=1001),
        _candidate("template", "tenant-a", 1, template_compatible=False),
        _candidate("capability", "tenant-a", 1, capabilities_compatible=False),
        _candidate("policy", "tenant-a", 1, policy_compatible=False),
        _candidate("cancelled", "tenant-a", 1, state=CandidateState.CANCELLED),
        _candidate("tenant-limit", "tenant-a", 1, tenant_active=2),
        _candidate("user-limit", "tenant-a", 1, user_active=1),
    ],
)
def test_ineligible_candidate_is_not_dispatched(blocked: Candidate) -> None:
    result = WeightedDominantResourceTimePolicy().decide(
        _snapshot((("tenant-a", _window(blocked)),)), 1000
    )

    assert isinstance(result, Ok)
    assert isinstance(result.value, NoDecision)


def test_request_must_fit_total_capacity_and_hard_quota() -> None:
    too_large = _candidate("large", "tenant-a", 1, cpu=9)
    over_quota = _candidate("quota", "tenant-b", 2, cpu=5)
    snapshot = _snapshot(
        (("tenant-a", _window(too_large)), ("tenant-b", _window(over_quota))),
        limits=(
            _limit("tenant-a"),
            _limit("tenant-b", quota=ResourceCapacity(4, 8, 1)),
        ),
    )

    result = WeightedDominantResourceTimePolicy().decide(snapshot, 0)

    assert isinstance(result, Ok)
    assert isinstance(result.value, NoDecision)


def test_non_fitting_candidate_remains_eligible_but_another_candidate_can_dispatch() -> None:
    held = HeldAllocation(
        "allocation-a",
        "tenant-a",
        ResourceRequest(6, 1, 0),
        (),
        AllocationState.HELD,
    )
    blocked = _candidate("blocked", "tenant-a", 1, cpu=4, age=119)
    fitting = _candidate("fitting", "tenant-a", 2, cpu=2, age=0)
    snapshot = _snapshot(
        (("tenant-a", _window(blocked, fitting, oldest=blocked)),),
        allocations=(held,),
        limits=(_limit("tenant-a", quota=ResourceCapacity(12, 12, 1)),),
    )

    assert _dispatch_job(snapshot).job_id == "fitting"
    assert blocked.eligible_wait_seconds == 119


def test_policy_moves_to_next_fair_tenant_when_first_has_no_current_fit() -> None:
    held = HeldAllocation(
        "allocation-a",
        "tenant-a",
        ResourceRequest(6, 1, 0),
        (),
        AllocationState.HELD,
    )
    blocked = _candidate("blocked", "tenant-a", 1, cpu=4)
    fitting = _candidate("fitting", "tenant-b", 2, cpu=2)
    snapshot = _snapshot(
        (("tenant-a", _window(blocked)), ("tenant-b", _window(fitting))),
        allocations=(held,),
        limits=(
            _limit("tenant-a", quota=ResourceCapacity(12, 12, 1)),
            _limit("tenant-b"),
        ),
    )

    assert _dispatch_job(snapshot).job_id == "fitting"


def test_candidate_bound_is_rejected_instead_of_truncated() -> None:
    candidates = tuple(_candidate(f"job-{index}", "tenant-a", index) for index in range(17))

    result = WeightedDominantResourceTimePolicy().decide(
        _snapshot((("tenant-a", _window(*candidates)),)), 0
    )

    assert result == Err(CandidateBoundExceeded("tenant-a", 17))


def test_oldest_candidate_duplicate_is_not_counted_twice() -> None:
    candidates = tuple(_candidate(f"job-{index}", "tenant-a", index) for index in range(16))
    snapshot = _snapshot((("tenant-a", _window(*candidates, oldest=candidates[0])),))

    assert _dispatch_job(snapshot).job_id == "job-0"


def test_no_decision_preserves_non_empty_continuation_cursors() -> None:
    blocked = _candidate("retry", "tenant-a", 1, retry_ready_at_ms=2)
    snapshot = _snapshot((("tenant-a", CandidateWindow((blocked,), blocked, b"cursor-a")),))

    result = WeightedDominantResourceTimePolicy().decide(snapshot, 1)

    assert result == Ok(NoDecision("no_eligible_candidate", (("tenant-a", b"cursor-a"),)))


def test_invalid_candidate_fields_return_invalid_snapshot() -> None:
    invalid = _candidate("bad", "tenant-a", 1, priority=3)

    result = WeightedDominantResourceTimePolicy().decide(
        _snapshot((("tenant-a", _window(invalid)),)), 0
    )

    assert result == Err(InvalidSnapshot("candidate bad has invalid base_priority"))


def test_same_snapshot_and_time_are_deterministic_and_not_mutated() -> None:
    a = _candidate("a", "tenant-a", 1)
    b = _candidate("b", "tenant-b", 2)
    snapshot = _snapshot((("tenant-b", _window(b)), ("tenant-a", _window(a))))
    policy = WeightedDominantResourceTimePolicy()

    first = policy.decide(snapshot, 0)
    second = policy.decide(snapshot, 0)

    assert first == second
    assert snapshot.candidates_by_tenant[0][0] == "tenant-b"
