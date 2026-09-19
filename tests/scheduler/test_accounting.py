from decimal import Decimal, localcontext
from fractions import Fraction

from hypothesis import assume, given
from hypothesis import strategies as st

from nexa.domain.scheduling import (
    AccountingRegression,
    AllocationState,
    Candidate,
    CandidateState,
    CandidateWindow,
    Err,
    HeldAllocation,
    InvalidSnapshot,
    NonPositiveWeight,
    Ok,
    ResourceCapacity,
    ResourceRequest,
    SchedulingSnapshot,
    TenantLedger,
    TenantPolicySnapshot,
)
from nexa.scheduler.accounting import advance_accounting


def _candidate(tenant_id: str, *, sequence: int = 1) -> Candidate:
    return Candidate(
        job_id=f"job-{tenant_id}-{sequence}",
        tenant_id=tenant_id,
        user_id=f"user-{tenant_id}",
        job_version=1,
        ready_sequence=sequence,
        resources=ResourceRequest(1, 1, 0),
        base_priority=1,
        eligible_wait_seconds=0,
        retry_ready_at_ms=0,
        template_version=1,
        required_capabilities=frozenset(),
        tenant_active_attempts=0,
        user_active_attempts=0,
        state=CandidateState.QUEUED,
    )


def _snapshot(
    *,
    capacity: ResourceCapacity,
    allocations: tuple[HeldAllocation, ...],
    ledgers: tuple[TenantLedger, ...],
    limits: tuple[TenantPolicySnapshot, ...],
    candidates: tuple[tuple[str, CandidateWindow], ...] = (),
    virtual_floor: Decimal = Decimal(0),
) -> SchedulingSnapshot:
    return SchedulingSnapshot(
        policy_version=1,
        allocatable_capacity=capacity,
        held_allocations=allocations,
        tenant_ledgers=ledgers,
        tenant_limits=limits,
        candidates_by_tenant=candidates,
        active_reservation=None,
        virtual_floor=virtual_floor,
    )


def _limits(tenant_id: str, weight: Decimal = Decimal(1)) -> TenantPolicySnapshot:
    return TenantPolicySnapshot(
        tenant_id=tenant_id,
        weight=weight,
        resource_quota=ResourceCapacity(100, 100, 1),
        max_active_attempts=2,
        max_user_active_attempts=1,
    )


def test_accounting_aggregates_allocation_vectors_before_dominant_share() -> None:
    snapshot = _snapshot(
        capacity=ResourceCapacity(8, 8, 0),
        allocations=(
            HeldAllocation(
                "a-1",
                "tenant-a",
                ResourceRequest(4, 1, 0),
                (),
                AllocationState.HELD,
            ),
            HeldAllocation(
                "a-2",
                "tenant-a",
                ResourceRequest(1, 4, 0),
                (),
                AllocationState.HELD,
            ),
        ),
        ledgers=(TenantLedger("tenant-a", Decimal(0), 0, False),),
        limits=(_limits("tenant-a"),),
    )

    result = advance_accounting(snapshot, 1000)

    assert isinstance(result, Ok)
    assert result.value.dominant_shares == (("tenant-a", Decimal("0.625")),)
    assert result.value.tenant_ledgers[0].virtual_score == Decimal("0.625")


def test_quarantined_allocation_remains_chargeable() -> None:
    snapshot = _snapshot(
        capacity=ResourceCapacity(4, 4, 0),
        allocations=(
            HeldAllocation(
                "a-1",
                "tenant-a",
                ResourceRequest(2, 1, 0),
                (),
                AllocationState.QUARANTINED,
            ),
        ),
        ledgers=(TenantLedger("tenant-a", Decimal(1), 1000, False),),
        limits=(_limits("tenant-a", Decimal(2)),),
    )

    result = advance_accounting(snapshot, 3000)

    assert isinstance(result, Ok)
    assert result.value.tenant_ledgers[0].virtual_score == Decimal("1.5")


def test_same_instant_does_not_double_charge_and_time_regression_is_typed() -> None:
    snapshot = _snapshot(
        capacity=ResourceCapacity(4, 4, 0),
        allocations=(
            HeldAllocation("a-1", "tenant-a", ResourceRequest(4, 1, 0), (), AllocationState.HELD),
        ),
        ledgers=(TenantLedger("tenant-a", Decimal(2), 1000, False),),
        limits=(_limits("tenant-a"),),
    )

    repeated = advance_accounting(snapshot, 1000)
    regressed = advance_accounting(snapshot, 999)

    assert isinstance(repeated, Ok)
    assert repeated.value.tenant_ledgers[0].virtual_score == Decimal(2)
    assert regressed == Err(AccountingRegression("tenant-a", 1000, 999))


def test_weight_and_capacity_changes_apply_only_to_following_interval() -> None:
    initial = _snapshot(
        capacity=ResourceCapacity(4, 4, 0),
        allocations=(
            HeldAllocation("a-1", "tenant-a", ResourceRequest(2, 1, 0), (), AllocationState.HELD),
        ),
        ledgers=(TenantLedger("tenant-a", Decimal(0), 0, False),),
        limits=(_limits("tenant-a"),),
    )
    first = advance_accounting(initial, 1000)
    assert isinstance(first, Ok)
    changed = _snapshot(
        capacity=ResourceCapacity(8, 4, 0),
        allocations=initial.held_allocations,
        ledgers=first.value.tenant_ledgers,
        limits=(_limits("tenant-a", Decimal(2)),),
    )

    second = advance_accounting(changed, 2000)

    assert isinstance(second, Ok)
    assert first.value.tenant_ledgers[0].virtual_score == Decimal("0.5")
    assert second.value.tenant_ledgers[0].virtual_score == Decimal("0.625")


def test_release_boundary_stops_future_charge() -> None:
    initial = _snapshot(
        capacity=ResourceCapacity(4, 4, 0),
        allocations=(
            HeldAllocation("a-1", "tenant-a", ResourceRequest(4, 4, 0), (), AllocationState.HELD),
        ),
        ledgers=(TenantLedger("tenant-a", Decimal(0), 0, False),),
        limits=(_limits("tenant-a"),),
    )
    first = advance_accounting(initial, 1000)
    assert isinstance(first, Ok)
    released = _snapshot(
        capacity=initial.allocatable_capacity,
        allocations=(),
        ledgers=first.value.tenant_ledgers,
        limits=initial.tenant_limits,
    )

    second = advance_accounting(released, 5000)

    assert isinstance(second, Ok)
    assert second.value.tenant_ledgers[0].virtual_score == Decimal(1)


def test_inactive_tenant_is_raised_to_existing_virtual_floor() -> None:
    candidate = _candidate("tenant-a")
    snapshot = _snapshot(
        capacity=ResourceCapacity(8, 8, 0),
        allocations=(),
        ledgers=(TenantLedger("tenant-a", Decimal(1), 0, False),),
        limits=(_limits("tenant-a"),),
        candidates=(("tenant-a", CandidateWindow((candidate,), candidate, None)),),
        virtual_floor=Decimal(3),
    )

    result = advance_accounting(snapshot, 0)

    assert isinstance(result, Ok)
    assert result.value.tenant_ledgers == (TenantLedger("tenant-a", Decimal(3), 0, True),)
    assert result.value.virtual_floor == Decimal(3)


def test_simultaneous_activation_and_empty_backlog_keep_floor_monotone() -> None:
    candidate_a = _candidate("tenant-a", sequence=1)
    candidate_b = _candidate("tenant-b", sequence=2)
    active = _snapshot(
        capacity=ResourceCapacity(8, 8, 0),
        allocations=(),
        ledgers=(
            TenantLedger("tenant-a", Decimal(1), 0, False),
            TenantLedger("tenant-b", Decimal(4), 0, False),
        ),
        limits=(_limits("tenant-a"), _limits("tenant-b")),
        candidates=(
            ("tenant-a", CandidateWindow((candidate_a,), candidate_a, None)),
            ("tenant-b", CandidateWindow((candidate_b,), candidate_b, None)),
        ),
        virtual_floor=Decimal(3),
    )
    activated = advance_accounting(active, 0)
    assert isinstance(activated, Ok)
    empty = _snapshot(
        capacity=active.allocatable_capacity,
        allocations=(),
        ledgers=activated.value.tenant_ledgers,
        limits=active.tenant_limits,
        virtual_floor=activated.value.virtual_floor,
    )

    result = advance_accounting(empty, 1000)

    assert isinstance(result, Ok)
    assert [ledger.virtual_score for ledger in activated.value.tenant_ledgers] == [
        Decimal(3),
        Decimal(4),
    ]
    assert result.value.virtual_floor == Decimal(3)
    assert all(not ledger.had_eligible_demand for ledger in result.value.tenant_ledgers)


def test_non_positive_weight_returns_its_contract_error() -> None:
    snapshot = _snapshot(
        capacity=ResourceCapacity(1, 1, 0),
        allocations=(),
        ledgers=(TenantLedger("tenant-a", Decimal(0), 0, False),),
        limits=(_limits("tenant-a", Decimal(0)),),
    )

    assert advance_accounting(snapshot, 0) == Err(NonPositiveWeight("tenant-a", Decimal(0)))


def test_non_finite_weights_return_a_typed_error() -> None:
    for weight in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        snapshot = _snapshot(
            capacity=ResourceCapacity(1, 1, 0),
            allocations=(),
            ledgers=(TenantLedger("tenant-a", Decimal(0), 0, False),),
            limits=(_limits("tenant-a", weight),),
        )

        assert advance_accounting(snapshot, 0) == Err(NonPositiveWeight("tenant-a", weight))


def test_non_finite_or_negative_score_and_floor_are_rejected() -> None:
    for score in (Decimal("NaN"), Decimal("Infinity"), Decimal("-1")):
        snapshot = _snapshot(
            capacity=ResourceCapacity(1, 1, 0),
            allocations=(),
            ledgers=(TenantLedger("tenant-a", score, 0, False),),
            limits=(_limits("tenant-a"),),
        )
        assert advance_accounting(snapshot, 0) == Err(
            InvalidSnapshot("tenant tenant-a virtual_score must be finite and non-negative")
        )

    snapshot = _snapshot(
        capacity=ResourceCapacity(1, 1, 0),
        allocations=(),
        ledgers=(TenantLedger("tenant-a", Decimal(0), 0, False),),
        limits=(_limits("tenant-a"),),
        virtual_floor=Decimal("NaN"),
    )
    assert advance_accounting(snapshot, 0) == Err(
        InvalidSnapshot("virtual_floor must be finite and non-negative")
    )


def test_negative_accounting_timestamps_are_rejected() -> None:
    negative_ledger = _snapshot(
        capacity=ResourceCapacity(1, 1, 0),
        allocations=(),
        ledgers=(TenantLedger("tenant-a", Decimal(0), -1, False),),
        limits=(_limits("tenant-a"),),
    )
    valid = _snapshot(
        capacity=ResourceCapacity(1, 1, 0),
        allocations=(),
        ledgers=(TenantLedger("tenant-a", Decimal(0), 0, False),),
        limits=(_limits("tenant-a"),),
    )

    assert advance_accounting(negative_ledger, 0) == Err(
        InvalidSnapshot("tenant tenant-a accounted_through_ms must be non-negative")
    )
    assert advance_accounting(valid, -1) == Err(
        InvalidSnapshot("now_ms must be a non-negative integer")
    )


@given(
    cpu_capacity=st.integers(min_value=1, max_value=10_000),
    memory_capacity=st.integers(min_value=1, max_value=10_000),
    cpu_used=st.integers(min_value=1, max_value=10_000),
    memory_used=st.integers(min_value=1, max_value=10_000),
    weight=st.integers(min_value=1, max_value=20),
    elapsed_ms=st.integers(min_value=0, max_value=20_000),
)
def test_decimal_accounting_matches_independent_fraction_oracle(
    cpu_capacity: int,
    memory_capacity: int,
    cpu_used: int,
    memory_used: int,
    weight: int,
    elapsed_ms: int,
) -> None:
    assume(cpu_used <= cpu_capacity)
    assume(memory_used <= memory_capacity)
    snapshot = _snapshot(
        capacity=ResourceCapacity(cpu_capacity, memory_capacity, 0),
        allocations=(
            HeldAllocation(
                "a-1",
                "tenant-a",
                ResourceRequest(cpu_used, memory_used, 0),
                (),
                AllocationState.HELD,
            ),
        ),
        ledgers=(TenantLedger("tenant-a", Decimal(0), 0, False),),
        limits=(
            TenantPolicySnapshot(
                tenant_id="tenant-a",
                weight=Decimal(weight),
                resource_quota=ResourceCapacity(cpu_capacity, memory_capacity, 0),
                max_active_attempts=2,
                max_user_active_attempts=1,
            ),
        ),
    )
    result = advance_accounting(snapshot, elapsed_ms)
    assert isinstance(result, Ok)
    expected = max(
        Fraction(cpu_used, cpu_capacity),
        Fraction(memory_used, memory_capacity),
    ) * Fraction(elapsed_ms, 1000 * weight)
    with localcontext() as context:
        context.prec = 50
        expected_decimal = Decimal(expected.numerator) / Decimal(expected.denominator)

    assert abs(result.value.tenant_ledgers[0].virtual_score - expected_decimal) < Decimal("1e-45")
