from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class ResourceCapacity:
    cpu_millis: int
    memory_bytes: int
    gpu_count: int


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    cpu_millis: int
    memory_bytes: int
    gpu_count: int


class AllocationState(StrEnum):
    HELD = "held"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class HeldAllocation:
    allocation_id: str
    tenant_id: str
    resources: ResourceRequest
    gpu_uuids: tuple[str, ...]
    state: AllocationState


@dataclass(frozen=True, slots=True)
class TenantLedger:
    tenant_id: str
    virtual_score: Decimal
    accounted_through_ms: int
    had_eligible_demand: bool


@dataclass(frozen=True, slots=True)
class TenantPolicySnapshot:
    tenant_id: str
    weight: Decimal
    resource_quota: ResourceCapacity
    max_active_attempts: int
    max_user_active_attempts: int


class CandidateState(StrEnum):
    QUEUED = "queued"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Candidate:
    job_id: str
    tenant_id: str
    user_id: str
    job_version: int
    ready_sequence: int
    resources: ResourceRequest
    base_priority: int
    eligible_wait_seconds: int
    retry_ready_at_ms: int
    template_version: int
    required_capabilities: frozenset[str]
    tenant_active_attempts: int
    user_active_attempts: int
    state: CandidateState = CandidateState.QUEUED
    template_compatible: bool = True
    capabilities_compatible: bool = True
    policy_compatible: bool = True


@dataclass(frozen=True, slots=True)
class CandidateWindow:
    normal: tuple[Candidate, ...]
    oldest_eligible: Candidate | None
    continuation_cursor: bytes | None


@dataclass(frozen=True, slots=True)
class ReservationSnapshot:
    reservation_id: str
    job_id: str
    tenant_id: str
    policy_version: int
    invalid_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SchedulingSnapshot:
    policy_version: int
    allocatable_capacity: ResourceCapacity
    held_allocations: tuple[HeldAllocation, ...]
    tenant_ledgers: tuple[TenantLedger, ...]
    tenant_limits: tuple[TenantPolicySnapshot, ...]
    candidates_by_tenant: tuple[tuple[str, CandidateWindow], ...]
    active_reservation: ReservationSnapshot | None
    virtual_floor: Decimal


@dataclass(frozen=True, slots=True)
class Dispatch:
    job_id: str
    expected_job_version: int
    expected_policy_version: int
    reservation_id: str | None = None


@dataclass(frozen=True, slots=True)
class CreateReservation:
    job_id: str
    tenant_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class DrainForReservation:
    reservation_id: str
    job_id: str


@dataclass(frozen=True, slots=True)
class InvalidateReservation:
    reservation_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class NoDecision:
    reason: str
    continuation_cursors: tuple[tuple[str, bytes], ...] = ()


type SchedulingDecision = (
    Dispatch | CreateReservation | DrainForReservation | InvalidateReservation | NoDecision
)


@dataclass(frozen=True, slots=True)
class InvalidSnapshot:
    reason: str


@dataclass(frozen=True, slots=True)
class AccountingRegression:
    tenant_id: str
    accounted_through_ms: int
    now_ms: int


@dataclass(frozen=True, slots=True)
class DuplicateGpuUuid:
    gpu_uuid: str


@dataclass(frozen=True, slots=True)
class NonPositiveWeight:
    tenant_id: str
    weight: Decimal


@dataclass(frozen=True, slots=True)
class CandidateBoundExceeded:
    tenant_id: str
    count: int


type PolicyError = (
    InvalidSnapshot
    | AccountingRegression
    | DuplicateGpuUuid
    | NonPositiveWeight
    | CandidateBoundExceeded
)


@dataclass(frozen=True, slots=True)
class Ok[T]:
    value: T


@dataclass(frozen=True, slots=True)
class Err[E]:
    error: E
