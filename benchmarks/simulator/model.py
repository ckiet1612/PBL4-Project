from dataclasses import dataclass
from enum import StrEnum


class ModelError(ValueError):
    """Raised when simulator input violates its closed data model."""


@dataclass(frozen=True, slots=True)
class ResourceVector:
    cpu_millis: int
    memory_bytes: int
    gpu_count: int

    def __post_init__(self) -> None:
        if self.cpu_millis < 0 or self.memory_bytes < 0 or self.gpu_count < 0:
            raise ModelError("resource components must be non-negative")

    @classmethod
    def zero(cls) -> "ResourceVector":
        return cls(0, 0, 0)

    def add(self, other: "ResourceVector") -> "ResourceVector":
        return ResourceVector(
            self.cpu_millis + other.cpu_millis,
            self.memory_bytes + other.memory_bytes,
            self.gpu_count + other.gpu_count,
        )

    def subtract(self, other: "ResourceVector") -> "ResourceVector":
        if not other.fits_within(self):
            raise ModelError("resource subtraction would underflow")
        return ResourceVector(
            self.cpu_millis - other.cpu_millis,
            self.memory_bytes - other.memory_bytes,
            self.gpu_count - other.gpu_count,
        )

    def fits_within(self, capacity: "ResourceVector") -> bool:
        return (
            self.cpu_millis <= capacity.cpu_millis
            and self.memory_bytes <= capacity.memory_bytes
            and self.gpu_count <= capacity.gpu_count
        )


@dataclass(frozen=True, slots=True)
class TenantSpec:
    tenant_id: str
    weight: int
    quota: ResourceVector
    max_running_jobs: int
    order: int

    def __post_init__(self) -> None:
        if self.weight <= 0:
            raise ModelError("tenant weight must be positive")


@dataclass(frozen=True, slots=True)
class JobSpec:
    job_id: str
    tenant_id: str
    ready_sequence: int
    arrival_ms: int
    resources: ResourceVector
    duration_ms: int
    priority: int = 1

    def __post_init__(self) -> None:
        if self.duration_ms <= 0:
            raise ModelError("job duration must be positive")


class JobState(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    INFEASIBLE = "infeasible"


@dataclass(frozen=True, slots=True)
class Allocation:
    job_id: str
    tenant_id: str
    resources: ResourceVector
    gpu_uuids: tuple[str, ...]
    start_ms: int
    release_ms: int

    def __post_init__(self) -> None:
        if self.gpu_uuids != tuple(sorted(self.gpu_uuids)):
            raise ModelError("allocation GPU UUIDs must be sorted")
        if len(set(self.gpu_uuids)) != len(self.gpu_uuids):
            raise ModelError("allocation contains duplicate GPU UUID")
        if len(self.gpu_uuids) != self.resources.gpu_count:
            raise ModelError("allocation GPU UUID count must match requested GPU count")


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    capacity: ResourceVector
    gpu_uuids: tuple[str, ...]
    tenants: tuple[TenantSpec, ...]

    def __post_init__(self) -> None:
        if len(set(self.gpu_uuids)) != len(self.gpu_uuids):
            raise ModelError("simulation config contains a duplicate GPU UUID")
        object.__setattr__(self, "gpu_uuids", tuple(sorted(self.gpu_uuids)))
        ordered_tenants = tuple(sorted(self.tenants, key=lambda item: item.order))
        object.__setattr__(self, "tenants", ordered_tenants)
