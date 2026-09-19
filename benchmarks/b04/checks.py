from decimal import Decimal

from nexa.domain.scheduling import (
    Candidate,
    CandidateState,
    CandidateWindow,
    CreateReservation,
    Dispatch,
    InvalidateReservation,
    Ok,
    ReservationSnapshot,
    ResourceCapacity,
    ResourceRequest,
    SchedulingSnapshot,
    TenantLedger,
    TenantPolicySnapshot,
)
from nexa.scheduler.policy import WeightedDominantResourceTimePolicy, effective_priority


def _candidate(age: int) -> Candidate:
    return Candidate(
        job_id="boundary-job",
        tenant_id="tenant-a",
        user_id="user-a",
        job_version=1,
        ready_sequence=1,
        resources=ResourceRequest(1, 1, 0),
        base_priority=0,
        eligible_wait_seconds=age,
        retry_ready_at_ms=0,
        template_version=1,
        required_capabilities=frozenset({"cpu"}),
        tenant_active_attempts=0,
        user_active_attempts=0,
        state=CandidateState.QUEUED,
    )


def _snapshot(
    candidate: Candidate,
    reservation: ReservationSnapshot | None = None,
) -> SchedulingSnapshot:
    return SchedulingSnapshot(
        policy_version=1,
        allocatable_capacity=ResourceCapacity(1, 1, 0),
        held_allocations=(),
        tenant_ledgers=(TenantLedger("tenant-a", Decimal(0), 0, True),),
        tenant_limits=(
            TenantPolicySnapshot(
                "tenant-a",
                Decimal(1),
                ResourceCapacity(1, 1, 0),
                1,
                1,
            ),
        ),
        candidates_by_tenant=(("tenant-a", CandidateWindow((candidate,), candidate, None)),),
        active_reservation=reservation,
        virtual_floor=Decimal(0),
    )


def build_policy_contract_checks() -> dict[str, object]:
    ages = (59, 60, 119, 120, 121)
    aging = [
        {
            "eligible_wait_seconds": age,
            "effective_priority": effective_priority(_candidate(age)),
        }
        for age in ages
    ]
    policy = WeightedDominantResourceTimePolicy()
    reservation_boundary: list[dict[str, object]] = []
    for age in (119, 120):
        result = policy.decide(_snapshot(_candidate(age)), 0)
        if not isinstance(result, Ok):
            raise RuntimeError(f"boundary policy check failed: {result}")
        if isinstance(result.value, Dispatch):
            decision_type = "dispatch"
        elif isinstance(result.value, CreateReservation):
            decision_type = "create_reservation"
        else:
            raise RuntimeError(f"unexpected boundary decision: {result.value}")
        reservation_boundary.append({"eligible_wait_seconds": age, "decision_type": decision_type})

    invalidation = []
    candidate = _candidate(120)
    for reason in (
        "cancelled",
        "no_longer_eligible",
        "policy_incompatible",
        "capability_incompatible",
    ):
        reservation = ReservationSnapshot(
            "reservation-1",
            candidate.job_id,
            candidate.tenant_id,
            1,
            invalid_reason=reason,
        )
        result = policy.decide(_snapshot(candidate, reservation), 0)
        if not isinstance(result, Ok) or not isinstance(result.value, InvalidateReservation):
            raise RuntimeError(f"invalidation policy check failed: {result}")
        invalidation.append(
            {
                "reason": reason,
                "decision_type": "invalidate_reservation",
                "preserved_reason": result.value.reason,
            }
        )

    return {
        "aging": aging,
        "reservation_boundary": reservation_boundary,
        "invalidation": invalidation,
    }
