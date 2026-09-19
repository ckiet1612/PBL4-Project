from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType

from benchmarks.simulator import SIMULATOR_VERSION
from benchmarks.simulator.engine import SimulationResult, TimelineEvent
from benchmarks.simulator.model import Allocation, JobState, ResourceVector


class MetricsError(ValueError):
    """Raised when a metric cannot be computed from the declared inputs."""


@dataclass(frozen=True, slots=True)
class FairnessAssessment:
    start_ms: int
    end_ms: int
    included_tenants: tuple[str, ...]
    excluded_tenants: Mapping[str, str]
    weighted_jain: Fraction | None


def _fraction_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _resource_data(resources: ResourceVector) -> dict[str, int]:
    return {
        "cpu_millis": resources.cpu_millis,
        "memory_bytes": resources.memory_bytes,
        "gpu_count": resources.gpu_count,
    }


def nearest_rank(values: Sequence[int], percentile: int) -> int | None:
    if not values:
        return None
    if not 1 <= percentile <= 100:
        raise MetricsError("percentile must be between 1 and 100")
    ordered = sorted(values)
    rank = (percentile * len(ordered) + 99) // 100
    return ordered[rank - 1]


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


def dominant_resource_time(
    allocations: Sequence[Allocation],
    capacity: ResourceVector,
    *,
    start_ms: int,
    end_ms: int,
) -> dict[str, Fraction]:
    if end_ms <= start_ms:
        raise MetricsError("resource-time window must have positive duration")
    clipped = [
        (allocation, max(start_ms, allocation.start_ms), min(end_ms, allocation.release_ms))
        for allocation in allocations
        if min(end_ms, allocation.release_ms) > max(start_ms, allocation.start_ms)
    ]
    boundaries = sorted(
        {
            point
            for _, overlap_start, overlap_end in clipped
            for point in (overlap_start, overlap_end)
        }
    )
    measured: dict[str, Fraction] = {}
    for interval_start, interval_end in zip(boundaries, boundaries[1:], strict=False):
        held_by_tenant: dict[str, ResourceVector] = {}
        for allocation, overlap_start, overlap_end in clipped:
            if overlap_start <= interval_start and overlap_end >= interval_end:
                held_by_tenant[allocation.tenant_id] = held_by_tenant.get(
                    allocation.tenant_id, ResourceVector.zero()
                ).add(allocation.resources)
        elapsed_ms = interval_end - interval_start
        for tenant_id, held in held_by_tenant.items():
            measured[tenant_id] = measured.get(tenant_id, Fraction(0)) + (
                _dominant_share(held, capacity) * elapsed_ms
            )
    return measured


def weighted_jain(resource_time: Mapping[str, Fraction], weights: Mapping[str, int]) -> Fraction:
    tenant_ids = tuple(sorted(resource_time))
    if not tenant_ids:
        raise MetricsError("weighted Jain requires at least one tenant")
    if set(tenant_ids) != set(weights):
        raise MetricsError("resource-time and weight tenant sets must match")
    normalized: list[Fraction] = []
    for tenant_id in tenant_ids:
        weight = weights[tenant_id]
        if weight <= 0:
            raise MetricsError("tenant weights must be positive")
        normalized.append(resource_time[tenant_id] / weight)
    denominator = len(normalized) * sum(value * value for value in normalized)
    if denominator == 0:
        raise MetricsError("weighted Jain is undefined when all resource-time values are zero")
    return sum(normalized) ** 2 / denominator


def _continuously_demanding(result: SimulationResult, tenant_id: str) -> bool:
    window = result.trace.fairness_window
    intervals = sorted(
        (
            max(window.start_ms, record.arrival_ms),
            min(window.end_ms, record.completion_ms),
        )
        for record in result.job_records
        if record.tenant_id == tenant_id
        and record.state is JobState.COMPLETED
        and record.completion_ms is not None
        and record.arrival_ms < window.end_ms
        and record.completion_ms > window.start_ms
    )
    cursor = window.start_ms
    for start_ms, end_ms in intervals:
        if start_ms > cursor:
            return False
        cursor = max(cursor, end_ms)
        if cursor >= window.end_ms:
            return True
    return False


def _max_window_share(result: SimulationResult, tenant_id: str) -> Fraction:
    window = result.trace.fairness_window
    tenant = next(item for item in result.trace.config.tenants if item.tenant_id == tenant_id)
    window_records = [
        record
        for record in result.job_records
        if record.tenant_id == tenant_id
        and record.infeasible_reason is None
        and record.arrival_ms < window.end_ms
        and (record.completion_ms is None or record.completion_ms > window.start_ms)
    ]
    states: set[tuple[ResourceVector, int]] = {(ResourceVector.zero(), 0)}
    for record in window_records:
        additions: set[tuple[ResourceVector, int]] = set()
        for resources, count in states:
            if count >= tenant.max_running_jobs:
                continue
            combined = resources.add(record.resources)
            if combined.fits_within(tenant.quota) and combined.fits_within(
                result.trace.config.capacity
            ):
                additions.add((combined, count + 1))
        states.update(additions)
    return max(
        (_dominant_share(resources, result.trace.config.capacity) for resources, _ in states),
        default=Fraction(0),
    )


def assess_fairness_window(result: SimulationResult) -> FairnessAssessment:
    window = result.trace.fairness_window
    exclusions: dict[str, str] = {}
    candidates: list[str] = []
    for tenant_id in window.tenant_ids:
        quota_blocked = any(
            record.tenant_id == tenant_id
            and record.infeasible_reason == "request_exceeds_tenant_quota"
            and window.start_ms <= record.arrival_ms < window.end_ms
            for record in result.job_records
        )
        if quota_blocked:
            exclusions[tenant_id] = "quota_does_not_permit_window_workload"
        elif not _continuously_demanding(result, tenant_id):
            exclusions[tenant_id] = "not_continuously_demanding"
        else:
            candidates.append(tenant_id)

    tenants = {tenant.tenant_id: tenant for tenant in result.trace.config.tenants}
    max_window_shares = {
        tenant_id: _max_window_share(result, tenant_id) for tenant_id in candidates
    }
    included = candidates
    while len(included) >= 2:
        included_weight = sum(tenants[tenant_id].weight for tenant_id in included)
        quota_limited = [
            tenant_id
            for tenant_id in included
            if max_window_shares[tenant_id] < Fraction(tenants[tenant_id].weight, included_weight)
        ]
        if not quota_limited:
            break
        for tenant_id in quota_limited:
            exclusions[tenant_id] = "quota_or_concurrency_does_not_permit_weighted_share"
        quota_limited_set = set(quota_limited)
        included = [tenant_id for tenant_id in included if tenant_id not in quota_limited_set]

    resource_time = dominant_resource_time(
        result.allocations,
        result.trace.config.capacity,
        start_ms=window.start_ms,
        end_ms=window.end_ms,
    )
    included_resource_time = {
        tenant_id: resource_time.get(tenant_id, Fraction(0)) for tenant_id in included
    }
    weights = {tenant_id: tenants[tenant_id].weight for tenant_id in included_resource_time}
    jain: Fraction | None = None
    if len(included_resource_time) >= 2 and any(included_resource_time.values()):
        jain = weighted_jain(included_resource_time, weights)
    return FairnessAssessment(
        start_ms=window.start_ms,
        end_ms=window.end_ms,
        included_tenants=tuple(included),
        excluded_tenants=MappingProxyType(exclusions),
        weighted_jain=jain,
    )


def _event_data(event: TimelineEvent) -> dict[str, object]:
    return {
        "time_ms": event.time_ms,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "job_id": event.job_id,
        "tenant_id": event.tenant_id,
        "resources": _resource_data(event.resources),
        "gpu_uuids": list(event.gpu_uuids),
        "reason": event.reason,
    }


def _timeline_data(result: SimulationResult, event_type: str) -> list[dict[str, object]]:
    return [_event_data(event) for event in result.timeline if event.event_type == event_type]


def build_raw_result(result: SimulationResult) -> dict[str, object]:
    completed = [record for record in result.job_records if record.state is JobState.COMPLETED]
    infeasible = [record for record in result.job_records if record.state is JobState.INFEASIBLE]
    waits = [
        record.dispatch_ms - record.arrival_ms
        for record in completed
        if record.dispatch_ms is not None
    ]
    simulation_runtime_ms = max((event.time_ms for event in result.timeline), default=0)
    throughput = (
        Fraction(len(completed) * 1_000, simulation_runtime_ms)
        if simulation_runtime_ms > 0
        else Fraction(0)
    )
    all_resource_time = dominant_resource_time(
        result.allocations,
        result.trace.config.capacity,
        start_ms=0,
        end_ms=max(1, simulation_runtime_ms),
    )
    assessment = assess_fairness_window(result)
    fairness_resource_time = dominant_resource_time(
        result.allocations,
        result.trace.config.capacity,
        start_ms=assessment.start_ms,
        end_ms=assessment.end_ms,
    )
    tenants = {tenant.tenant_id: tenant for tenant in result.trace.config.tenants}

    return {
        "schema_version": 1,
        "simulator_version": SIMULATOR_VERSION,
        "baseline": {
            "name": result.baseline_name,
            "version": result.baseline_version,
            "cost": {
                "policy_decisions": result.policy_decisions,
                "candidate_evaluations": result.candidate_evaluations,
            },
        },
        "provenance": {
            "seed": result.trace.seed,
            "trace_id": result.trace.trace_id,
            "trace_checksum": result.trace.trace_checksum,
            "materialized_checksum": result.trace.materialized_checksum,
        },
        "config": {
            "capacity": _resource_data(result.trace.config.capacity),
            "gpu_uuids": list(result.trace.config.gpu_uuids),
            "tenants": [
                {
                    "tenant_id": tenant.tenant_id,
                    "weight": tenant.weight,
                    "quota": _resource_data(tenant.quota),
                    "max_running_jobs": tenant.max_running_jobs,
                    "order": tenant.order,
                }
                for tenant in result.trace.config.tenants
            ],
        },
        "counts": {
            "arrived": len(result.job_records),
            "accepted": len(result.job_records) - len(infeasible),
            "rejected": len(infeasible),
            "completed": len(completed),
            "infeasible": len(infeasible),
        },
        "jobs": [
            {
                "job_id": record.job_id,
                "tenant_id": record.tenant_id,
                "ready_sequence": record.ready_sequence,
                "arrival_ms": record.arrival_ms,
                "resources": _resource_data(record.resources),
                "duration_ms": record.duration_ms,
                "state": record.state.value,
                "dispatch_ms": record.dispatch_ms,
                "completion_ms": record.completion_ms,
                "wait_ms": (
                    record.dispatch_ms - record.arrival_ms
                    if record.dispatch_ms is not None
                    else None
                ),
                "infeasible_reason": record.infeasible_reason,
            }
            for record in result.job_records
        ],
        "event_timeline": [_event_data(event) for event in result.timeline],
        "dispatch_timeline": _timeline_data(result, "dispatch"),
        "allocation_timeline": [
            {
                "job_id": allocation.job_id,
                "tenant_id": allocation.tenant_id,
                "start_ms": allocation.start_ms,
                "release_ms": allocation.release_ms,
                "resources": _resource_data(allocation.resources),
                "gpu_uuids": list(allocation.gpu_uuids),
            }
            for allocation in result.allocations
        ],
        "gpu_allocation_timeline": [
            {
                "job_id": allocation.job_id,
                "tenant_id": allocation.tenant_id,
                "start_ms": allocation.start_ms,
                "release_ms": allocation.release_ms,
                "gpu_uuids": list(allocation.gpu_uuids),
            }
            for allocation in result.allocations
            if allocation.gpu_uuids
        ],
        "dominant_resource_time_share_ms": {
            tenant_id: _fraction_text(all_resource_time.get(tenant_id, Fraction(0)))
            for tenant_id in sorted(tenants)
        },
        "fairness": {
            "formula": "x_i=dominant_resource_time_i/weight_i;J=(sum(x_i)^2)/(n*sum(x_i^2))",
            "window_start_ms": assessment.start_ms,
            "window_end_ms": assessment.end_ms,
            "included_tenants": list(assessment.included_tenants),
            "excluded_tenants": dict(assessment.excluded_tenants),
            "resource_time_share_ms": {
                tenant_id: _fraction_text(fairness_resource_time.get(tenant_id, Fraction(0)))
                for tenant_id in result.trace.fairness_window.tenant_ids
            },
            "weights": {
                tenant_id: tenants[tenant_id].weight
                for tenant_id in result.trace.fairness_window.tenant_ids
            },
            "weighted_jain": (
                _fraction_text(assessment.weighted_jain)
                if assessment.weighted_jain is not None
                else None
            ),
        },
        "metrics": {
            "throughput_jobs_per_second": _fraction_text(throughput),
            "max_wait_ms": max(waits, default=None),
            "p50_wait_ms": nearest_rank(waits, 50),
            "p95_wait_ms": nearest_rank(waits, 95),
            "event_count": len(result.timeline),
            "simulation_runtime_ms": simulation_runtime_ms,
        },
        "invariant_violations": list(result.invariant_violations),
        "starvation": {
            "undispatched_job_ids": [
                record.job_id
                for record in result.job_records
                if record.state not in {JobState.COMPLETED, JobState.INFEASIBLE}
            ]
        },
    }
