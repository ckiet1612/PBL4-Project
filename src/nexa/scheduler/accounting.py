from dataclasses import dataclass, replace
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from nexa.domain.scheduling import (
    AccountingRegression,
    Candidate,
    CandidateState,
    CandidateWindow,
    DuplicateGpuUuid,
    Err,
    InvalidSnapshot,
    NonPositiveWeight,
    Ok,
    PolicyError,
    ResourceCapacity,
    ResourceRequest,
    SchedulingSnapshot,
    TenantLedger,
    TenantPolicySnapshot,
)

_DECIMAL_PRECISION = 50


@dataclass(frozen=True, slots=True)
class AccountingState:
    tenant_ledgers: tuple[TenantLedger, ...]
    virtual_floor: Decimal
    held_resources_by_tenant: tuple[tuple[str, ResourceCapacity], ...]
    dominant_shares: tuple[tuple[str, Decimal], ...]
    eligible_tenant_ids: tuple[str, ...]


def _zero_resources() -> ResourceCapacity:
    return ResourceCapacity(0, 0, 0)


def _add_resources(
    left: ResourceCapacity, right: ResourceCapacity | ResourceRequest
) -> ResourceCapacity:
    return ResourceCapacity(
        left.cpu_millis + right.cpu_millis,
        left.memory_bytes + right.memory_bytes,
        left.gpu_count + right.gpu_count,
    )


def _fits(request: ResourceCapacity | ResourceRequest, capacity: ResourceCapacity) -> bool:
    return (
        request.cpu_millis <= capacity.cpu_millis
        and request.memory_bytes <= capacity.memory_bytes
        and request.gpu_count <= capacity.gpu_count
    )


def _dominant_share(resources: ResourceCapacity, capacity: ResourceCapacity) -> Decimal:
    with localcontext() as context:
        context.prec = _DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        shares = [
            Decimal(used) / Decimal(available)
            for used, available in (
                (resources.cpu_millis, capacity.cpu_millis),
                (resources.memory_bytes, capacity.memory_bytes),
                (resources.gpu_count, capacity.gpu_count),
            )
            if available > 0
        ]
        return max(shares, default=Decimal(0))


def candidate_ineligibility_reason(
    candidate: Candidate,
    *,
    now_ms: int,
    capacity: ResourceCapacity,
    held: ResourceCapacity,
    limits: TenantPolicySnapshot,
) -> str | None:
    if candidate.state is not CandidateState.QUEUED:
        return "cancelled"
    if candidate.retry_ready_at_ms > now_ms:
        return "retry_not_ready"
    if not candidate.template_compatible:
        return "template_incompatible"
    if not candidate.capabilities_compatible:
        return "capability_incompatible"
    if not candidate.policy_compatible:
        return "policy_incompatible"
    if not _fits(candidate.resources, capacity):
        return "request_exceeds_capacity"
    if not _fits(_add_resources(held, candidate.resources), limits.resource_quota):
        return "request_exceeds_quota"
    if candidate.tenant_active_attempts >= limits.max_active_attempts:
        return "tenant_concurrency"
    if candidate.user_active_attempts >= limits.max_user_active_attempts:
        return "user_concurrency"
    return None


def _invalid_resource_capacity(capacity: ResourceCapacity) -> bool:
    return capacity.cpu_millis < 0 or capacity.memory_bytes < 0 or capacity.gpu_count < 0


def _invalid_request(request: ResourceRequest) -> bool:
    return request.cpu_millis <= 0 or request.memory_bytes <= 0 or request.gpu_count not in {0, 1}


def _validate_and_aggregate(
    snapshot: SchedulingSnapshot,
) -> (
    tuple[
        dict[str, TenantLedger],
        dict[str, TenantPolicySnapshot],
        dict[str, ResourceCapacity],
    ]
    | PolicyError
):
    if snapshot.policy_version <= 0:
        return InvalidSnapshot("policy_version must be positive")
    if (
        not isinstance(snapshot.virtual_floor, Decimal)
        or not snapshot.virtual_floor.is_finite()
        or snapshot.virtual_floor < 0
    ):
        return InvalidSnapshot("virtual_floor must be finite and non-negative")
    if _invalid_resource_capacity(snapshot.allocatable_capacity):
        return InvalidSnapshot("allocatable capacity must be non-negative")

    ledgers = {ledger.tenant_id: ledger for ledger in snapshot.tenant_ledgers}
    limits = {limit.tenant_id: limit for limit in snapshot.tenant_limits}
    if len(ledgers) != len(snapshot.tenant_ledgers):
        return InvalidSnapshot("tenant ledgers contain duplicate tenant IDs")
    if len(limits) != len(snapshot.tenant_limits):
        return InvalidSnapshot("tenant limits contain duplicate tenant IDs")
    if set(ledgers) != set(limits):
        return InvalidSnapshot("tenant ledger and limit sets differ")
    for tenant_id, ledger in ledgers.items():
        if (
            not isinstance(ledger.virtual_score, Decimal)
            or not ledger.virtual_score.is_finite()
            or ledger.virtual_score < 0
        ):
            return InvalidSnapshot(
                f"tenant {tenant_id} virtual_score must be finite and non-negative"
            )
        if type(ledger.accounted_through_ms) is not int or ledger.accounted_through_ms < 0:
            return InvalidSnapshot(f"tenant {tenant_id} accounted_through_ms must be non-negative")
    for tenant_id, limit in limits.items():
        if (
            not isinstance(limit.weight, Decimal)
            or not limit.weight.is_finite()
            or limit.weight <= 0
        ):
            return NonPositiveWeight(tenant_id, limit.weight)
        if _invalid_resource_capacity(limit.resource_quota):
            return InvalidSnapshot(f"tenant {tenant_id} quota must be non-negative")
        if limit.max_active_attempts <= 0 or limit.max_user_active_attempts <= 0:
            return InvalidSnapshot(f"tenant {tenant_id} concurrency limits must be positive")

    held = {tenant_id: _zero_resources() for tenant_id in ledgers}
    total = _zero_resources()
    seen_gpu_uuids: set[str] = set()
    for allocation in snapshot.held_allocations:
        if allocation.tenant_id not in held:
            return InvalidSnapshot("held allocation references an unknown tenant")
        if _invalid_request(allocation.resources):
            return InvalidSnapshot("held allocation contains an invalid resource vector")
        if len(allocation.gpu_uuids) != allocation.resources.gpu_count:
            return InvalidSnapshot("held allocation GPU UUID count does not match request")
        for gpu_uuid in allocation.gpu_uuids:
            if gpu_uuid in seen_gpu_uuids:
                return DuplicateGpuUuid(gpu_uuid)
            seen_gpu_uuids.add(gpu_uuid)
        held[allocation.tenant_id] = _add_resources(
            held[allocation.tenant_id], allocation.resources
        )
        total = _add_resources(total, allocation.resources)

    if not _fits(total, snapshot.allocatable_capacity):
        return InvalidSnapshot("held allocations exceed allocatable capacity")
    for tenant_id, resources in held.items():
        if not _fits(resources, limits[tenant_id].resource_quota):
            return InvalidSnapshot(f"tenant {tenant_id} held resources exceed quota")
    return ledgers, limits, held


def advance_accounting(
    snapshot: SchedulingSnapshot, now_ms: int
) -> Ok[AccountingState] | Err[PolicyError]:
    if type(now_ms) is not int or now_ms < 0:
        return Err(InvalidSnapshot("now_ms must be a non-negative integer"))
    validated = _validate_and_aggregate(snapshot)
    if not isinstance(validated, tuple):
        return Err(validated)
    ledgers, limits, held = validated

    dominant_shares = {
        tenant_id: _dominant_share(resources, snapshot.allocatable_capacity)
        for tenant_id, resources in held.items()
    }
    updated: dict[str, TenantLedger] = {}
    with localcontext() as context:
        context.prec = _DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        for tenant_id, ledger in ledgers.items():
            if now_ms < ledger.accounted_through_ms:
                return Err(AccountingRegression(tenant_id, ledger.accounted_through_ms, now_ms))
            elapsed_ms = now_ms - ledger.accounted_through_ms
            charge = (
                dominant_shares[tenant_id]
                * Decimal(elapsed_ms)
                / Decimal(1000)
                / limits[tenant_id].weight
            )
            updated[tenant_id] = replace(
                ledger,
                virtual_score=ledger.virtual_score + charge,
                accounted_through_ms=now_ms,
            )

    windows: dict[str, CandidateWindow] = {}
    for tenant_id, window in snapshot.candidates_by_tenant:
        if tenant_id in windows:
            return Err(InvalidSnapshot("candidate windows contain duplicate tenant IDs"))
        if tenant_id not in limits:
            return Err(InvalidSnapshot("candidate window references an unknown tenant"))
        windows[tenant_id] = window

    eligible_tenants: list[str] = []
    for tenant_id, window_object in windows.items():
        window = window_object
        candidates = list(window.normal)
        if window.oldest_eligible is not None:
            candidates.append(window.oldest_eligible)
        unique_candidates = {candidate.job_id: candidate for candidate in candidates}
        if any(
            candidate_ineligibility_reason(
                candidate,
                now_ms=now_ms,
                capacity=snapshot.allocatable_capacity,
                held=held[tenant_id],
                limits=limits[tenant_id],
            )
            is None
            for candidate in unique_candidates.values()
        ):
            eligible_tenants.append(tenant_id)

    for tenant_id, ledger in tuple(updated.items()):
        has_demand = tenant_id in eligible_tenants
        score = ledger.virtual_score
        if has_demand and not ledger.had_eligible_demand:
            score = max(score, snapshot.virtual_floor)
        updated[tenant_id] = replace(
            ledger,
            virtual_score=score,
            had_eligible_demand=has_demand,
        )

    virtual_floor = snapshot.virtual_floor
    if eligible_tenants:
        virtual_floor = max(
            virtual_floor,
            min(updated[tenant_id].virtual_score for tenant_id in eligible_tenants),
        )

    tenant_order = tuple(sorted(updated))
    return Ok(
        AccountingState(
            tenant_ledgers=tuple(updated[tenant_id] for tenant_id in tenant_order),
            virtual_floor=virtual_floor,
            held_resources_by_tenant=tuple(
                (tenant_id, held[tenant_id]) for tenant_id in tenant_order
            ),
            dominant_shares=tuple(
                (tenant_id, dominant_shares[tenant_id]) for tenant_id in tenant_order
            ),
            eligible_tenant_ids=tuple(sorted(eligible_tenants)),
        )
    )
