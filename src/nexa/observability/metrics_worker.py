"""Worker process metrics (docs/observability.md §1.3). Labels are closed sets; no IDs.

`outcome` is what this worker reported and the API acknowledged. A log overflow stops
the runner with reason FAILURE, so it is counted as INTERNAL (B19-R19); a cancel the
runner obeyed is reported the same way and is not a separate outcome.
"""

from prometheus_client import Counter, Gauge

from nexa.observability.metrics import closed, guarded, new_registry, preinitialize

REGISTRY = new_registry()

EXECUTION_OUTCOMES = (
    "SUCCEEDED",
    "PAUSED",
    "INFRASTRUCTURE",
    "TIMEOUT",
    "OOM",
    "INVALID_INPUT",
    "INCOMPATIBLE",
    "INTERNAL",
)
LOOP_OPERATIONS = ("heartbeat", "renew", "reconcile", "poll", "ipc", "result", "stats")
READY_CHECKS = ("reconciled", "heartbeat", "docker")
_LOOP_NAMES = {
    "heartbeat_once": "heartbeat",
    "renew_once": "renew",
    "reconcile_once": "reconcile",
    "_poll_once": "poll",
    "_ipc_once": "ipc",
    "_result_once": "result",
}

EXECUTIONS = Counter(
    "nexa_worker_executions_total",
    "Attempt outcomes this worker reported and the API acknowledged",
    ("outcome",),
    registry=REGISTRY,
)
OOM = Counter(
    "nexa_worker_container_oom_total",
    "Workload containers reported as OOM-killed",
    registry=REGISTRY,
)
MEMORY_RATIO = Gauge(
    "nexa_worker_container_memory_ratio_max",
    "Largest memory usage/limit among running workload containers (last sample)",
    registry=REGISTRY,
)
CPU_RATIO = Gauge(
    "nexa_worker_container_cpu_ratio_max",
    "Largest CPU used/CPU granted among running workload containers; ~1 means at quota",
    registry=REGISTRY,
)
LOOP_FAILURES = Counter(
    "nexa_worker_loop_failures_total",
    "Worker loop iterations that failed or timed out",
    ("operation",),
    registry=REGISTRY,
)
WORKER_READY = Gauge(
    "nexa_worker_ready",
    "This worker reported READY in its last accepted heartbeat (1)",
    registry=REGISTRY,
)
READY = Gauge("nexa_ready", "Readiness check result (1 ok)", ("check",), registry=REGISTRY)

preinitialize(EXECUTIONS, EXECUTION_OUTCOMES)
preinitialize(LOOP_FAILURES, LOOP_OPERATIONS)


def execution(outcome: str) -> None:
    label = closed(outcome, EXECUTION_OUTCOMES, "INTERNAL")
    guarded(lambda: EXECUTIONS.labels(label).inc())
    if label == "OOM":
        guarded(OOM.inc)


def loop_failed(operation_name: str) -> None:
    name = _LOOP_NAMES.get(operation_name, operation_name)
    guarded(lambda: LOOP_FAILURES.labels(closed(name, LOOP_OPERATIONS, "poll")).inc())


def container_usage(memory_ratio: float, cpu_ratio: float) -> None:
    guarded(MEMORY_RATIO.set, memory_ratio)
    guarded(CPU_RATIO.set, cpu_ratio)


def set_ready(checks: dict[str, str]) -> None:
    for name in READY_CHECKS:
        value = 1 if checks.get(name) in {"ok", "skip"} else 0
        guarded(lambda name=name, value=value: READY.labels(name).set(value))
