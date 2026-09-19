from dataclasses import dataclass, field
from fractions import Fraction
from math import gcd

from benchmarks.simulator.model import JobSpec, ResourceVector
from benchmarks.simulator.policy import SchedulingSnapshot


@dataclass(slots=True)
class FifoPolicy:
    name: str = "fifo"
    version: str = "1"

    def reset(self) -> None:
        return None

    def choose(self, snapshot: SchedulingSnapshot) -> str | None:
        if not snapshot.candidates:
            return None
        selected = min(snapshot.candidates, key=lambda job: (job.ready_sequence, job.job_id))
        return selected.job_id


def _candidates_by_tenant(snapshot: SchedulingSnapshot) -> dict[str, tuple[JobSpec, ...]]:
    grouped: dict[str, list[JobSpec]] = {}
    for job in snapshot.candidates:
        grouped.setdefault(job.tenant_id, []).append(job)
    return {
        tenant_id: tuple(sorted(jobs, key=lambda job: (job.ready_sequence, job.job_id)))
        for tenant_id, jobs in grouped.items()
    }


def _dominant_share(resources: ResourceVector, capacity: ResourceVector) -> Fraction:
    shares = [
        Fraction(used, available)
        for used, available in (
            (resources.cpu_millis, capacity.cpu_millis),
            (resources.memory_bytes, capacity.memory_bytes),
            (resources.gpu_count, capacity.gpu_count),
        )
        if available > 0
    ]
    return max(shares, default=Fraction(0))


@dataclass(slots=True)
class RoundRobinPolicy:
    name: str = "rr"
    version: str = "1"
    _cursor: int = field(default=0, init=False, repr=False)
    _tenant_order: tuple[str, ...] = field(default=(), init=False, repr=False)

    def reset(self) -> None:
        self._cursor = 0
        self._tenant_order = ()

    def choose(self, snapshot: SchedulingSnapshot) -> str | None:
        order = tuple(tenant.tenant_id for tenant in snapshot.tenants)
        if order != self._tenant_order:
            self._tenant_order = order
            self._cursor = 0
        grouped = _candidates_by_tenant(snapshot)
        for offset in range(len(order)):
            index = (self._cursor + offset) % len(order)
            tenant_id = order[index]
            candidates = grouped.get(tenant_id)
            if candidates:
                self._cursor = (index + 1) % len(order)
                return candidates[0].job_id
        return None


@dataclass(slots=True)
class WeightedRoundRobinPolicy:
    name: str = "wrr"
    version: str = "1"
    _cursor: int = field(default=0, init=False, repr=False)
    _signature: tuple[tuple[str, int], ...] = field(default=(), init=False, repr=False)
    _cycle: tuple[str, ...] = field(default=(), init=False, repr=False)

    def reset(self) -> None:
        self._cursor = 0
        self._signature = ()
        self._cycle = ()

    def choose(self, snapshot: SchedulingSnapshot) -> str | None:
        signature = tuple((tenant.tenant_id, tenant.weight) for tenant in snapshot.tenants)
        if signature != self._signature:
            self._signature = signature
            weight_divisor = gcd(*(weight for _, weight in signature))
            self._cycle = tuple(
                tenant_id
                for tenant_id, weight in signature
                for _ in range(weight // weight_divisor)
            )
            self._cursor = 0
        grouped = _candidates_by_tenant(snapshot)
        for offset in range(len(self._cycle)):
            index = (self._cursor + offset) % len(self._cycle)
            tenant_id = self._cycle[index]
            candidates = grouped.get(tenant_id)
            if candidates:
                self._cursor = (index + 1) % len(self._cycle)
                return candidates[0].job_id
        return None


@dataclass(slots=True)
class DeficitRoundRobinPolicy:
    name: str = "drr"
    version: str = "1"
    _cursor: int = field(default=0, init=False, repr=False)
    _tenant_order: tuple[str, ...] = field(default=(), init=False, repr=False)
    _deficits: dict[str, Fraction] = field(default_factory=dict, init=False, repr=False)
    _active_tenant: str | None = field(default=None, init=False, repr=False)

    def reset(self) -> None:
        self._cursor = 0
        self._tenant_order = ()
        self._deficits = {}
        self._active_tenant = None

    def deficit_for(self, tenant_id: str) -> Fraction:
        return self._deficits.get(tenant_id, Fraction(0))

    def choose(self, snapshot: SchedulingSnapshot) -> str | None:
        order = tuple(tenant.tenant_id for tenant in snapshot.tenants)
        if order != self._tenant_order:
            self._tenant_order = order
            self._cursor = 0
            self._deficits = {tenant_id: Fraction(0) for tenant_id in order}
            self._active_tenant = None
        grouped = _candidates_by_tenant(snapshot)
        if not grouped:
            return None
        weights = {tenant.tenant_id: tenant.weight for tenant in snapshot.tenants}
        max_weight = max(weights.values())

        if self._active_tenant is not None:
            candidates = grouped.get(self._active_tenant)
            if candidates:
                selected = candidates[0]
                cost = _dominant_share(selected.resources, snapshot.capacity)
                if cost <= self._deficits[self._active_tenant]:
                    self._deficits[self._active_tenant] -= cost
                    return selected.job_id
            self._active_tenant = None

        while True:
            tenant_id = order[self._cursor]
            self._cursor = (self._cursor + 1) % len(order)
            candidates = grouped.get(tenant_id)
            if not candidates:
                continue
            self._deficits[tenant_id] += Fraction(weights[tenant_id], max_weight)
            selected = candidates[0]
            cost = _dominant_share(selected.resources, snapshot.capacity)
            if cost <= self._deficits[tenant_id]:
                self._deficits[tenant_id] -= cost
                self._active_tenant = tenant_id
                return selected.job_id


@dataclass(slots=True)
class DominantResourceFairnessPolicy:
    name: str = "drf"
    version: str = "1"

    def reset(self) -> None:
        return None

    def choose(self, snapshot: SchedulingSnapshot) -> str | None:
        grouped = _candidates_by_tenant(snapshot)
        if not grouped:
            return None
        tenant_specs = {tenant.tenant_id: tenant for tenant in snapshot.tenants}
        allocated = {tenant_id: ResourceVector.zero() for tenant_id in tenant_specs}
        for allocation in snapshot.held_allocations:
            allocated[allocation.tenant_id] = allocated[allocation.tenant_id].add(
                allocation.resources
            )

        tenant_id = min(
            grouped,
            key=lambda candidate_tenant_id: (
                _dominant_share(
                    allocated[candidate_tenant_id],
                    snapshot.capacity,
                ),
                -tenant_specs[candidate_tenant_id].weight,
                grouped[candidate_tenant_id][0].ready_sequence,
                candidate_tenant_id,
            ),
        )
        return grouped[tenant_id][0].job_id
