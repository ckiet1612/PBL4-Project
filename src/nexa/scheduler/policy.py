from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, localcontext

from nexa.domain.scheduling import (
    Candidate,
    CandidateBoundExceeded,
    CandidateWindow,
    CreateReservation,
    Dispatch,
    DrainForReservation,
    Err,
    InvalidateReservation,
    InvalidSnapshot,
    NoDecision,
    Ok,
    PolicyError,
    ResourceCapacity,
    ResourceRequest,
    SchedulingDecision,
    SchedulingSnapshot,
)
from nexa.scheduler.accounting import advance_accounting, candidate_ineligibility_reason

_MAX_NORMAL_CANDIDATES = 16


def effective_priority(candidate: Candidate) -> int:
    return min(2, candidate.base_priority + candidate.eligible_wait_seconds // 60)


def _fits(request: ResourceRequest, capacity: ResourceCapacity) -> bool:
    return (
        request.cpu_millis <= capacity.cpu_millis
        and request.memory_bytes <= capacity.memory_bytes
        and request.gpu_count <= capacity.gpu_count
    )


def _subtract(left: ResourceCapacity, right: ResourceCapacity) -> ResourceCapacity:
    return ResourceCapacity(
        left.cpu_millis - right.cpu_millis,
        left.memory_bytes - right.memory_bytes,
        left.gpu_count - right.gpu_count,
    )


def _add(left: ResourceCapacity, right: ResourceCapacity) -> ResourceCapacity:
    return ResourceCapacity(
        left.cpu_millis + right.cpu_millis,
        left.memory_bytes + right.memory_bytes,
        left.gpu_count + right.gpu_count,
    )


def _request_as_capacity(request: ResourceRequest) -> ResourceCapacity:
    return ResourceCapacity(request.cpu_millis, request.memory_bytes, request.gpu_count)


def _validate_candidate(candidate: Candidate, tenant_id: str) -> InvalidSnapshot | None:
    if candidate.tenant_id != tenant_id:
        return InvalidSnapshot(f"candidate {candidate.job_id} has mismatched tenant_id")
    if candidate.job_version <= 0:
        return InvalidSnapshot(f"candidate {candidate.job_id} has invalid job_version")
    if candidate.ready_sequence < 0:
        return InvalidSnapshot(f"candidate {candidate.job_id} has invalid ready_sequence")
    if candidate.resources.cpu_millis <= 0 or candidate.resources.memory_bytes <= 0:
        return InvalidSnapshot(f"candidate {candidate.job_id} has invalid resources")
    if candidate.resources.gpu_count not in {0, 1}:
        return InvalidSnapshot(f"candidate {candidate.job_id} has invalid resources")
    if candidate.base_priority not in {0, 1, 2}:
        return InvalidSnapshot(f"candidate {candidate.job_id} has invalid base_priority")
    if candidate.eligible_wait_seconds < 0:
        return InvalidSnapshot(f"candidate {candidate.job_id} has invalid eligible_wait_seconds")
    if candidate.retry_ready_at_ms < 0:
        return InvalidSnapshot(f"candidate {candidate.job_id} has invalid retry_ready_at_ms")
    if candidate.template_version <= 0:
        return InvalidSnapshot(f"candidate {candidate.job_id} has invalid template_version")
    if candidate.tenant_active_attempts < 0 or candidate.user_active_attempts < 0:
        return InvalidSnapshot(f"candidate {candidate.job_id} has invalid active attempts")
    return None


def _validate_windows(
    snapshot: SchedulingSnapshot,
) -> tuple[dict[str, CandidateWindow], PolicyError | None]:
    windows: dict[str, CandidateWindow] = {}
    candidates_by_id: dict[str, Candidate] = {}
    for tenant_id, window in snapshot.candidates_by_tenant:
        if tenant_id in windows:
            return {}, InvalidSnapshot("candidate windows contain duplicate tenant IDs")
        if len(window.normal) > _MAX_NORMAL_CANDIDATES:
            return {}, CandidateBoundExceeded(tenant_id, len(window.normal))
        windows[tenant_id] = window
        normal_ids: set[str] = set()
        for candidate in window.normal:
            error = _validate_candidate(candidate, tenant_id)
            if error is not None:
                return {}, error
            if candidate.job_id in normal_ids:
                return {}, InvalidSnapshot(
                    f"candidate window {tenant_id} contains duplicate job IDs"
                )
            normal_ids.add(candidate.job_id)
            previous = candidates_by_id.get(candidate.job_id)
            if previous is not None and previous != candidate:
                return {}, InvalidSnapshot("candidate job ID is not globally unique")
            candidates_by_id[candidate.job_id] = candidate
        if window.oldest_eligible is not None:
            error = _validate_candidate(window.oldest_eligible, tenant_id)
            if error is not None:
                return {}, error
            previous = candidates_by_id.get(window.oldest_eligible.job_id)
            if previous is not None and previous != window.oldest_eligible:
                return {}, InvalidSnapshot(
                    f"oldest candidate {window.oldest_eligible.job_id} disagrees with normal window"
                )
            candidates_by_id[window.oldest_eligible.job_id] = window.oldest_eligible
    return windows, None


def _continuation_cursors(
    windows: dict[str, CandidateWindow],
) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (tenant_id, window.continuation_cursor)
        for tenant_id, window in sorted(windows.items())
        if window.continuation_cursor is not None
    )


@dataclass(frozen=True, slots=True)
class WeightedDominantResourceTimePolicy:
    name: str = "nexa"
    version: str = "1"

    def decide(
        self, snapshot: SchedulingSnapshot, now_ms: int
    ) -> Ok[SchedulingDecision] | Err[PolicyError]:
        windows, window_error = _validate_windows(snapshot)
        if window_error is not None:
            return Err(window_error)
        accounted = advance_accounting(snapshot, now_ms)
        if isinstance(accounted, Err):
            return accounted
        state = accounted.value
        ledgers = {ledger.tenant_id: ledger for ledger in state.tenant_ledgers}
        limits = {limit.tenant_id: limit for limit in snapshot.tenant_limits}
        held_by_tenant = dict(state.held_resources_by_tenant)
        dominant_shares = dict(state.dominant_shares)

        total_held = ResourceCapacity(0, 0, 0)
        for resources in held_by_tenant.values():
            total_held = _add(total_held, resources)
        free = _subtract(snapshot.allocatable_capacity, total_held)

        eligible_normal: dict[str, tuple[Candidate, ...]] = {}
        eligible_all: dict[str, tuple[Candidate, ...]] = {}
        candidates_by_id: dict[str, Candidate] = {}
        for tenant_id, window in windows.items():
            if tenant_id not in limits:
                return Err(InvalidSnapshot("candidate window references an unknown tenant"))
            unique = {candidate.job_id: candidate for candidate in window.normal}
            if window.oldest_eligible is not None:
                unique[window.oldest_eligible.job_id] = window.oldest_eligible
            candidates_by_id.update(unique)
            eligible_all[tenant_id] = tuple(
                candidate
                for candidate in unique.values()
                if candidate_ineligibility_reason(
                    candidate,
                    now_ms=now_ms,
                    capacity=snapshot.allocatable_capacity,
                    held=held_by_tenant[tenant_id],
                    limits=limits[tenant_id],
                )
                is None
            )
            eligible_normal[tenant_id] = tuple(
                candidate
                for candidate in window.normal
                if candidate_ineligibility_reason(
                    candidate,
                    now_ms=now_ms,
                    capacity=snapshot.allocatable_capacity,
                    held=held_by_tenant[tenant_id],
                    limits=limits[tenant_id],
                )
                is None
            )

        reservation = snapshot.active_reservation
        if reservation is not None:
            if reservation.invalid_reason is not None:
                return Ok(
                    InvalidateReservation(reservation.reservation_id, reservation.invalid_reason)
                )
            if reservation.policy_version != snapshot.policy_version:
                return Ok(InvalidateReservation(reservation.reservation_id, "policy_incompatible"))
            candidate = candidates_by_id.get(reservation.job_id)
            if candidate is None or candidate.tenant_id != reservation.tenant_id:
                return Ok(InvalidateReservation(reservation.reservation_id, "no_longer_eligible"))
            invalid_reason = candidate_ineligibility_reason(
                candidate,
                now_ms=now_ms,
                capacity=snapshot.allocatable_capacity,
                held=held_by_tenant[reservation.tenant_id],
                limits=limits[reservation.tenant_id],
            )
            if invalid_reason is not None:
                reservation_reason = {
                    "cancelled": "cancelled",
                    "capability_incompatible": "capability_incompatible",
                    "template_incompatible": "template_incompatible",
                    "policy_incompatible": "policy_incompatible",
                }.get(invalid_reason, "no_longer_eligible")
                return Ok(InvalidateReservation(reservation.reservation_id, reservation_reason))
            if _fits(candidate.resources, free):
                return Ok(
                    Dispatch(
                        job_id=candidate.job_id,
                        expected_job_version=candidate.job_version,
                        expected_policy_version=snapshot.policy_version,
                        reservation_id=reservation.reservation_id,
                    )
                )
            return Ok(DrainForReservation(reservation.reservation_id, candidate.job_id))

        tenant_ids = [tenant_id for tenant_id, items in eligible_all.items() if items]
        with localcontext() as context:
            context.prec = 50
            context.rounding = ROUND_HALF_EVEN
            tenant_ids.sort(
                key=lambda tenant_id: (
                    ledgers[tenant_id].virtual_score,
                    dominant_shares[tenant_id] / limits[tenant_id].weight,
                    min(candidate.ready_sequence for candidate in eligible_all[tenant_id]),
                    tenant_id,
                )
            )

        for tenant_id in tenant_ids:
            oldest = windows[tenant_id].oldest_eligible
            if (
                oldest is not None
                and oldest.eligible_wait_seconds >= 120
                and any(candidate.job_id == oldest.job_id for candidate in eligible_all[tenant_id])
            ):
                return Ok(
                    CreateReservation(
                        job_id=oldest.job_id,
                        tenant_id=tenant_id,
                        reason="eligible_wait_threshold",
                    )
                )
            fitting = [
                candidate
                for candidate in eligible_normal[tenant_id]
                if _fits(candidate.resources, free)
            ]
            if not fitting:
                continue
            candidate = min(
                fitting,
                key=lambda item: (
                    -effective_priority(item),
                    item.ready_sequence,
                    item.job_id,
                ),
            )
            return Ok(
                Dispatch(
                    job_id=candidate.job_id,
                    expected_job_version=candidate.job_version,
                    expected_policy_version=snapshot.policy_version,
                )
            )

        reason = "no_eligible_candidate" if not tenant_ids else "no_candidate_fits_free_resources"
        return Ok(NoDecision(reason, _continuation_cursors(windows)))
