"""Coordinator process metrics (docs/observability.md §1.2). Labels are closed sets; no IDs."""

from prometheus_client import Counter, Gauge, Histogram

from nexa.observability.metrics import closed, guarded, new_registry, preinitialize

REGISTRY = new_registry()

DECISION_OUTCOMES = ("offer", "reservation", "invalidate", "none")
MAINTENANCE_STEPS = ("reap", "promote", "sweep", "quota")
QUOTA_TRANSITIONS = ("blocked", "unblocked")
READY_CHECKS = ("database", "schema")

LEADER = Gauge(
    "nexa_coordinator_leader", "This process holds coordinator leadership (1)", registry=REGISTRY
)
TICK_DURATION = Histogram(
    "nexa_coordinator_tick_duration_seconds",
    "Duration of one scheduling tick, including maintenance probes",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)
DECISIONS = Counter(
    "nexa_coordinator_decisions_total",
    "Scheduling decisions after their transaction committed",
    ("outcome",),
    registry=REGISTRY,
)
DISPATCH_WAIT = Histogram(
    "nexa_coordinator_dispatch_wait_seconds",
    "DB-time wait from queue eligibility to the committed offer",
    buckets=(0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 3600.0, 14400.0, 86400.0),
    registry=REGISTRY,
)
LEASE_EXPIRED = Counter(
    "nexa_lease_expired_total", "Expired leases revoked and fenced by the reaper", registry=REGISTRY
)
MAINTENANCE_FAILURES = Counter(
    "nexa_coordinator_maintenance_failures_total",
    "Maintenance steps that failed and will be retried on the next probe",
    ("step",),
    registry=REGISTRY,
)
QUOTA_BLOCKED = Counter(
    "nexa_coordinator_quota_blocked_total",
    "Tenants whose queued jobs started or stopped waiting for artifact byte quota",
    ("transition",),
    registry=REGISTRY,
)
READY = Gauge("nexa_ready", "Readiness check result (1 ok)", ("check",), registry=REGISTRY)

preinitialize(DECISIONS, DECISION_OUTCOMES)
preinitialize(MAINTENANCE_FAILURES, MAINTENANCE_STEPS)
preinitialize(QUOTA_BLOCKED, QUOTA_TRANSITIONS)


def leader(is_leader: bool) -> None:
    guarded(LEADER.set, 1 if is_leader else 0)


def tick(seconds: float) -> None:
    guarded(TICK_DURATION.observe, seconds)


def decision(outcome: str) -> None:
    guarded(lambda: DECISIONS.labels(closed(outcome, DECISION_OUTCOMES, "none")).inc())


def dispatch_wait(seconds: float) -> None:
    guarded(DISPATCH_WAIT.observe, max(0.0, seconds))


def leases_expired(count: int) -> None:
    if count > 0:
        guarded(LEASE_EXPIRED.inc, count)


def maintenance_failed(step: str) -> None:
    guarded(lambda: MAINTENANCE_FAILURES.labels(closed(step, MAINTENANCE_STEPS, "sweep")).inc())


def quota_transition(transition: str, count: int = 1) -> None:
    if count > 0:
        guarded(
            lambda: QUOTA_BLOCKED.labels(closed(transition, QUOTA_TRANSITIONS, "blocked")).inc(
                count
            )
        )


def set_ready(checks: dict[str, str]) -> None:
    for name in READY_CHECKS:
        value = 1 if checks.get(name) in {"ok", "skip"} else 0
        guarded(lambda name=name, value=value: READY.labels(name).set(value))
