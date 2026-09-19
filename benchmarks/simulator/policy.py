from dataclasses import dataclass
from typing import Protocol

from benchmarks.simulator.model import Allocation, JobSpec, ResourceVector, TenantSpec


@dataclass(frozen=True, slots=True)
class SchedulingSnapshot:
    now_ms: int
    capacity: ResourceVector
    remaining_capacity: ResourceVector
    free_gpu_uuids: tuple[str, ...]
    candidates: tuple[JobSpec, ...]
    held_allocations: tuple[Allocation, ...]
    tenants: tuple[TenantSpec, ...]


class SchedulerPolicy(Protocol):
    name: str
    version: str

    def choose(self, snapshot: SchedulingSnapshot) -> str | None: ...
