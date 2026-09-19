import hashlib
import json
import random
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks.simulator.model import (
    JobSpec,
    ModelError,
    ResourceVector,
    SimulationConfig,
    TenantSpec,
)


class TraceError(ValueError):
    """Raised when a trace definition is malformed or ambiguous."""


@dataclass(frozen=True, slots=True)
class FairnessWindow:
    start_ms: int
    end_ms: int
    tenant_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GeneratorGroup:
    group_id: str
    tenant_id: str
    count: int
    ready_sequence_start: int
    arrival_start_ms: int
    arrival_step_ms: int
    arrival_jitter_ms: int
    duration_choices_ms: tuple[int, ...]
    resource_choices: tuple[ResourceVector, ...]
    priority: int


@dataclass(frozen=True, slots=True)
class TraceDefinition:
    version: int
    trace_id: str
    config: SimulationConfig
    fairness_window: FairnessWindow
    jobs: tuple[JobSpec, ...]
    generator_groups: tuple[GeneratorGroup, ...]
    trace_checksum: str


@dataclass(frozen=True, slots=True)
class MaterializedTrace:
    version: int
    trace_id: str
    seed: int
    config: SimulationConfig
    fairness_window: FairnessWindow
    jobs: tuple[JobSpec, ...]
    trace_checksum: str
    materialized_checksum: str


def canonical_json_bytes(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise TraceError("value is not canonical JSON data") from exc
    return rendered.encode("utf-8")


def _checksum(value: object) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TraceError(f"{label} must be an object with string keys")
    return value


def _closed_fields(
    value: Mapping[str, Any],
    *,
    label: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    unknown = sorted(set(value) - required - optional)
    if unknown:
        raise TraceError(f"{label} has unknown field(s): {', '.join(unknown)}")
    missing = sorted(required - set(value))
    if missing:
        raise TraceError(f"{label} is missing field(s): {', '.join(missing)}")


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise TraceError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise TraceError(f"{label} must be at least {minimum}")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TraceError(f"{label} must be a non-empty string")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise TraceError(f"{label} must be an array")
    return value


def _resource(value: object, label: str) -> ResourceVector:
    data = _mapping(value, label)
    _closed_fields(
        data,
        label=label,
        required=frozenset({"cpu_millis", "memory_bytes", "gpu_count"}),
    )
    try:
        return ResourceVector(
            cpu_millis=_integer(data["cpu_millis"], f"{label}.cpu_millis", minimum=0),
            memory_bytes=_integer(data["memory_bytes"], f"{label}.memory_bytes", minimum=0),
            gpu_count=_integer(data["gpu_count"], f"{label}.gpu_count", minimum=0),
        )
    except ModelError as exc:
        raise TraceError(str(exc)) from exc


def _job(value: object, label: str) -> JobSpec:
    data = _mapping(value, label)
    required = frozenset(
        {
            "job_id",
            "tenant_id",
            "ready_sequence",
            "arrival_ms",
            "resources",
            "duration_ms",
        }
    )
    _closed_fields(data, label=label, required=required, optional=frozenset({"priority"}))
    try:
        return JobSpec(
            job_id=_identifier(data["job_id"], f"{label}.job_id"),
            tenant_id=_identifier(data["tenant_id"], f"{label}.tenant_id"),
            ready_sequence=_integer(data["ready_sequence"], f"{label}.ready_sequence", minimum=0),
            arrival_ms=_integer(data["arrival_ms"], f"{label}.arrival_ms", minimum=0),
            resources=_resource(data["resources"], f"{label}.resources"),
            duration_ms=_integer(data["duration_ms"], f"{label}.duration_ms", minimum=1),
            priority=_integer(data.get("priority", 1), f"{label}.priority", minimum=0),
        )
    except ModelError as exc:
        raise TraceError(str(exc)) from exc


def _generator(value: object, label: str) -> GeneratorGroup:
    data = _mapping(value, label)
    required = frozenset(
        {
            "group_id",
            "tenant_id",
            "count",
            "ready_sequence_start",
            "arrival_start_ms",
            "arrival_step_ms",
            "arrival_jitter_ms",
            "duration_choices_ms",
            "resource_choices",
        }
    )
    _closed_fields(data, label=label, required=required, optional=frozenset({"priority"}))
    duration_values = _array(data["duration_choices_ms"], f"{label}.duration_choices_ms")
    if not duration_values:
        raise TraceError(f"{label}.duration_choices_ms must not be empty")
    resource_values = _array(data["resource_choices"], f"{label}.resource_choices")
    if not resource_values:
        raise TraceError(f"{label}.resource_choices must not be empty")
    return GeneratorGroup(
        group_id=_identifier(data["group_id"], f"{label}.group_id"),
        tenant_id=_identifier(data["tenant_id"], f"{label}.tenant_id"),
        count=_integer(data["count"], f"{label}.count", minimum=1),
        ready_sequence_start=_integer(
            data["ready_sequence_start"], f"{label}.ready_sequence_start", minimum=0
        ),
        arrival_start_ms=_integer(data["arrival_start_ms"], f"{label}.arrival_start_ms", minimum=0),
        arrival_step_ms=_integer(data["arrival_step_ms"], f"{label}.arrival_step_ms", minimum=0),
        arrival_jitter_ms=_integer(
            data["arrival_jitter_ms"], f"{label}.arrival_jitter_ms", minimum=0
        ),
        duration_choices_ms=tuple(
            _integer(item, f"{label}.duration_choices_ms", minimum=1) for item in duration_values
        ),
        resource_choices=tuple(
            _resource(item, f"{label}.resource_choices") for item in resource_values
        ),
        priority=_integer(data.get("priority", 1), f"{label}.priority", minimum=0),
    )


def load_trace(path: Path) -> TraceDefinition:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TraceError(f"cannot read trace: {path.name}") from exc
    data = _mapping(raw, "trace")
    required = frozenset(
        {
            "version",
            "trace_id",
            "capacity",
            "tenants",
            "fairness_window",
            "jobs",
            "generator_groups",
        }
    )
    _closed_fields(data, label="trace", required=required)
    version = _integer(data["version"], "trace.version", minimum=1)
    if version != 1:
        raise TraceError(f"unsupported trace version: {version}")

    capacity_data = _mapping(data["capacity"], "trace.capacity")
    _closed_fields(
        capacity_data,
        label="trace.capacity",
        required=frozenset({"cpu_millis", "memory_bytes", "gpu_uuids"}),
    )
    gpu_values = _array(capacity_data["gpu_uuids"], "trace.capacity.gpu_uuids")
    gpu_uuids = tuple(_identifier(item, "trace.capacity.gpu_uuids") for item in gpu_values)
    capacity = ResourceVector(
        _integer(capacity_data["cpu_millis"], "trace.capacity.cpu_millis", minimum=0),
        _integer(capacity_data["memory_bytes"], "trace.capacity.memory_bytes", minimum=0),
        len(gpu_uuids),
    )

    tenants: list[TenantSpec] = []
    for index, item in enumerate(_array(data["tenants"], "trace.tenants")):
        tenant_data = _mapping(item, f"trace.tenants[{index}]")
        _closed_fields(
            tenant_data,
            label=f"trace.tenants[{index}]",
            required=frozenset({"tenant_id", "weight", "quota", "max_running_jobs", "order"}),
        )
        try:
            tenants.append(
                TenantSpec(
                    tenant_id=_identifier(
                        tenant_data["tenant_id"], f"trace.tenants[{index}].tenant_id"
                    ),
                    weight=_integer(
                        tenant_data["weight"], f"trace.tenants[{index}].weight", minimum=1
                    ),
                    quota=_resource(tenant_data["quota"], f"trace.tenants[{index}].quota"),
                    max_running_jobs=_integer(
                        tenant_data["max_running_jobs"],
                        f"trace.tenants[{index}].max_running_jobs",
                        minimum=1,
                    ),
                    order=_integer(
                        tenant_data["order"], f"trace.tenants[{index}].order", minimum=0
                    ),
                )
            )
        except ModelError as exc:
            raise TraceError(str(exc)) from exc

    tenant_ids = [tenant.tenant_id for tenant in tenants]
    tenant_orders = [tenant.order for tenant in tenants]
    if len(set(tenant_ids)) != len(tenant_ids):
        raise TraceError("trace contains duplicate tenant ID")
    if len(set(tenant_orders)) != len(tenant_orders):
        raise TraceError("trace contains duplicate tenant order")
    try:
        config = SimulationConfig(capacity, gpu_uuids, tuple(tenants))
    except ModelError as exc:
        raise TraceError(str(exc)) from exc

    window_data = _mapping(data["fairness_window"], "trace.fairness_window")
    _closed_fields(
        window_data,
        label="trace.fairness_window",
        required=frozenset({"start_ms", "end_ms", "tenant_ids"}),
    )
    start_ms = _integer(window_data["start_ms"], "fairness_window.start_ms", minimum=0)
    end_ms = _integer(window_data["end_ms"], "fairness_window.end_ms", minimum=1)
    if end_ms <= start_ms:
        raise TraceError("fairness window end_ms must be greater than start_ms")
    window_tenants = tuple(
        _identifier(item, "fairness_window.tenant_ids")
        for item in _array(window_data["tenant_ids"], "fairness_window.tenant_ids")
    )
    if not window_tenants or not set(window_tenants).issubset(tenant_ids):
        raise TraceError("fairness window must reference configured tenants")
    fairness_window = FairnessWindow(start_ms, end_ms, window_tenants)

    jobs = tuple(
        _job(item, f"trace.jobs[{index}]")
        for index, item in enumerate(_array(data["jobs"], "trace.jobs"))
    )
    groups = tuple(
        _generator(item, f"trace.generator_groups[{index}]")
        for index, item in enumerate(_array(data["generator_groups"], "trace.generator_groups"))
    )
    if any(job.tenant_id not in tenant_ids for job in jobs):
        raise TraceError("trace job references an unknown tenant")
    if any(group.tenant_id not in tenant_ids for group in groups):
        raise TraceError("trace generator group references an unknown tenant")
    group_ids = [group.group_id for group in groups]
    if len(set(group_ids)) != len(group_ids):
        raise TraceError("trace contains duplicate generator group ID")

    return TraceDefinition(
        version=version,
        trace_id=_identifier(data["trace_id"], "trace.trace_id"),
        config=config,
        fairness_window=fairness_window,
        jobs=jobs,
        generator_groups=groups,
        trace_checksum=_checksum(raw),
    )


def _resource_data(resources: ResourceVector) -> dict[str, int]:
    return {
        "cpu_millis": resources.cpu_millis,
        "memory_bytes": resources.memory_bytes,
        "gpu_count": resources.gpu_count,
    }


def _job_data(job: JobSpec) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "tenant_id": job.tenant_id,
        "ready_sequence": job.ready_sequence,
        "arrival_ms": job.arrival_ms,
        "resources": _resource_data(job.resources),
        "duration_ms": job.duration_ms,
        "priority": job.priority,
    }


def materialize_trace(definition: TraceDefinition, seed: int) -> MaterializedTrace:
    if type(seed) is not int:
        raise TraceError("seed must be an integer")
    generator = random.Random(seed)
    jobs = list(definition.jobs)
    for group in definition.generator_groups:
        for index in range(group.count):
            jitter = generator.randint(0, group.arrival_jitter_ms)
            jobs.append(
                JobSpec(
                    job_id=f"{group.group_id}-{index:04d}",
                    tenant_id=group.tenant_id,
                    ready_sequence=group.ready_sequence_start + index,
                    arrival_ms=group.arrival_start_ms + group.arrival_step_ms * index + jitter,
                    resources=generator.choice(group.resource_choices),
                    duration_ms=generator.choice(group.duration_choices_ms),
                    priority=group.priority,
                )
            )
    job_ids = [job.job_id for job in jobs]
    if len(set(job_ids)) != len(job_ids):
        raise TraceError("materialized trace contains duplicate job ID")
    ready_sequences = [job.ready_sequence for job in jobs]
    if len(set(ready_sequences)) != len(ready_sequences):
        raise TraceError("materialized trace contains duplicate ready_sequence")
    ordered_jobs = tuple(sorted(jobs, key=lambda item: (item.ready_sequence, item.job_id)))
    materialized_data = {
        "version": definition.version,
        "trace_id": definition.trace_id,
        "seed": seed,
        "trace_checksum": definition.trace_checksum,
        "capacity": _resource_data(definition.config.capacity),
        "gpu_uuids": list(definition.config.gpu_uuids),
        "tenants": [
            {
                "tenant_id": tenant.tenant_id,
                "weight": tenant.weight,
                "quota": _resource_data(tenant.quota),
                "max_running_jobs": tenant.max_running_jobs,
                "order": tenant.order,
            }
            for tenant in definition.config.tenants
        ],
        "fairness_window": {
            "start_ms": definition.fairness_window.start_ms,
            "end_ms": definition.fairness_window.end_ms,
            "tenant_ids": list(definition.fairness_window.tenant_ids),
        },
        "jobs": [_job_data(job) for job in ordered_jobs],
    }
    return MaterializedTrace(
        version=definition.version,
        trace_id=definition.trace_id,
        seed=seed,
        config=definition.config,
        fairness_window=definition.fairness_window,
        jobs=ordered_jobs,
        trace_checksum=definition.trace_checksum,
        materialized_checksum=_checksum(materialized_data),
    )
