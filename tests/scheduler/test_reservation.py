from dataclasses import replace
from decimal import Decimal

import pytest

from nexa.domain.scheduling import (
    AllocationState,
    Candidate,
    CandidateState,
    CandidateWindow,
    CreateReservation,
    Dispatch,
    DrainForReservation,
    HeldAllocation,
    InvalidateReservation,
    NoDecision,
    Ok,
    ReservationSnapshot,
    ResourceCapacity,
    ResourceRequest,
    SchedulingSnapshot,
    TenantLedger,
    TenantPolicySnapshot,
)
from nexa.scheduler.policy import WeightedDominantResourceTimePolicy

_CAPACITY = ResourceCapacity(8, 8, 0)
_QUOTA = ResourceCapacity(16, 16, 0)


def _candidate(
    job_id: str,
    tenant_id: str,
    sequence: int,
    *,
    age: int = 120,
    cpu: int = 8,
    compatible: bool = True,
    policy_compatible: bool = True,
    state: CandidateState = CandidateState.QUEUED,
) -> Candidate:
    return Candidate(
        job_id=job_id,
        tenant_id=tenant_id,
        user_id=f"user-{tenant_id}",
        job_version=sequence + 1,
        ready_sequence=sequence,
        resources=ResourceRequest(cpu, 1, 0),
        base_priority=1,
        eligible_wait_seconds=age,
        retry_ready_at_ms=0,
        template_version=1,
        required_capabilities=frozenset({"cpu"}),
        tenant_active_attempts=0,
        user_active_attempts=0,
        state=state,
        capabilities_compatible=compatible,
        policy_compatible=policy_compatible,
    )


def _snapshot(
    windows: tuple[tuple[str, CandidateWindow], ...],
    *,
    scores: dict[str, str] | None = None,
    allocations: tuple[HeldAllocation, ...] = (),
    reservation: ReservationSnapshot | None = None,
    policy_version: int = 5,
) -> SchedulingSnapshot:
    tenant_ids = tuple(tenant_id for tenant_id, _ in windows)
    score_values = scores or {}
    return SchedulingSnapshot(
        policy_version=policy_version,
        allocatable_capacity=_CAPACITY,
        held_allocations=allocations,
        tenant_ledgers=tuple(
            TenantLedger(tenant_id, Decimal(score_values.get(tenant_id, "0")), 0, True)
            for tenant_id in tenant_ids
        ),
        tenant_limits=tuple(
            TenantPolicySnapshot(tenant_id, Decimal(1), _QUOTA, 2, 1) for tenant_id in tenant_ids
        ),
        candidates_by_tenant=windows,
        active_reservation=reservation,
        virtual_floor=Decimal(0),
    )


def _decide(snapshot: SchedulingSnapshot):
    result = WeightedDominantResourceTimePolicy().decide(snapshot, 0)
    assert isinstance(result, Ok)
    return result.value


def test_reservation_uses_fair_tenant_not_global_oldest_job() -> None:
    globally_oldest = _candidate("old-a", "tenant-a", 1, age=300)
    fair_tenant_job = _candidate("fair-b", "tenant-b", 20, age=120)
    snapshot = _snapshot(
        (
            ("tenant-a", CandidateWindow((globally_oldest,), globally_oldest, None)),
            ("tenant-b", CandidateWindow((fair_tenant_job,), fair_tenant_job, None)),
        ),
        scores={"tenant-a": "10", "tenant-b": "1"},
    )

    assert _decide(snapshot) == CreateReservation("fair-b", "tenant-b", "eligible_wait_threshold")


def test_oldest_eligible_outside_normal_window_can_create_reservation() -> None:
    normal = tuple(
        _candidate(f"small-{index}", "tenant-a", index + 10, age=0, cpu=1) for index in range(16)
    )
    oldest = _candidate("large", "tenant-a", 1, age=120)
    snapshot = _snapshot((("tenant-a", CandidateWindow(normal, oldest, b"continue")),))

    assert _decide(snapshot) == CreateReservation("large", "tenant-a", "eligible_wait_threshold")


def test_active_reservation_drains_without_switching_to_new_small_job() -> None:
    protected = _candidate("large", "tenant-a", 1, cpu=8)
    small = _candidate("small", "tenant-b", 2, age=500, cpu=1)
    allocation = HeldAllocation(
        "held-small",
        "tenant-b",
        ResourceRequest(1, 1, 0),
        (),
        AllocationState.HELD,
    )
    reservation = ReservationSnapshot("reservation-1", "large", "tenant-a", 5)
    snapshot = _snapshot(
        (
            ("tenant-a", CandidateWindow((), protected, None)),
            ("tenant-b", CandidateWindow((small,), small, None)),
        ),
        allocations=(allocation,),
        reservation=reservation,
    )

    assert _decide(snapshot) == DrainForReservation("reservation-1", "large")


def test_active_reservation_dispatches_and_references_reservation_when_fit() -> None:
    protected = _candidate("large", "tenant-a", 1, cpu=8)
    reservation = ReservationSnapshot("reservation-1", "large", "tenant-a", 5)
    snapshot = _snapshot(
        (("tenant-a", CandidateWindow((), protected, None)),),
        reservation=reservation,
    )

    assert _decide(snapshot) == Dispatch("large", 2, 5, "reservation-1")


@pytest.mark.parametrize(
    "reason",
    ["cancelled", "no_longer_eligible", "policy_incompatible", "capability_incompatible"],
)
def test_coordinator_supplied_reservation_invalidation_reason_is_preserved(reason: str) -> None:
    protected = _candidate("large", "tenant-a", 1)
    reservation = ReservationSnapshot(
        "reservation-1", "large", "tenant-a", 5, invalid_reason=reason
    )
    snapshot = _snapshot(
        (("tenant-a", CandidateWindow((), protected, None)),),
        reservation=reservation,
    )

    assert _decide(snapshot) == InvalidateReservation("reservation-1", reason)


def test_missing_or_now_incompatible_reserved_candidate_is_invalidated() -> None:
    missing = ReservationSnapshot("reservation-1", "missing", "tenant-a", 5)
    empty = _snapshot((("tenant-a", CandidateWindow((), None, None)),), reservation=missing)
    assert _decide(empty) == InvalidateReservation("reservation-1", "no_longer_eligible")

    incompatible = _candidate("large", "tenant-a", 1, compatible=False)
    active = replace(missing, job_id="large")
    incompatible_snapshot = _snapshot(
        (("tenant-a", CandidateWindow((), incompatible, None)),), reservation=active
    )
    assert _decide(incompatible_snapshot) == InvalidateReservation(
        "reservation-1", "capability_incompatible"
    )


def test_reservation_policy_version_mismatch_is_invalidated() -> None:
    protected = _candidate("large", "tenant-a", 1)
    reservation = ReservationSnapshot("reservation-1", "large", "tenant-a", 4)
    snapshot = _snapshot(
        (("tenant-a", CandidateWindow((), protected, None)),),
        reservation=reservation,
        policy_version=5,
    )

    assert _decide(snapshot) == InvalidateReservation("reservation-1", "policy_incompatible")


def test_infeasible_oldest_job_never_creates_a_reservation() -> None:
    impossible = _candidate("impossible", "tenant-a", 1, age=1000, cpu=9)
    snapshot = _snapshot((("tenant-a", CandidateWindow((), impossible, None)),))

    assert _decide(snapshot) == NoDecision("no_eligible_candidate")
