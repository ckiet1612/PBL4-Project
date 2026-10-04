"""Scrape-time state collectors for the API registry (docs/observability.md §1.1, D5).

Each collector reads PostgreSQL in its own READ ONLY transaction with a short
`statement_timeout` on a dedicated small engine, so a scrape can never take a REST
connection or write. Results are cached for `NEXA_METRICS_CACHE_SECONDS` and refreshed
single-flight. A failing collector exposes `nexa_collector_up{collector}=0`, counts the
error and drops its own series until the next good refresh (no stale values).
"""

import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from nexa.application.admin_queries import fairness_rows
from nexa.observability import metrics_api
from nexa.observability.fairness import jain_index
from nexa.observability.logging import log_event
from nexa.observability.metrics import guarded

_LOG = logging.getLogger(__name__)
ACTIVE_JOB_STATES = (
    "QUEUED",
    "DISPATCHING",
    "RUNNING",
    "PAUSING",
    "PAUSED",
    "RECOVERING",
    "RETRY_WAIT",
    "CANCELLING",
)
WAITING_REASONS = (
    "waiting_for_worker",
    "waiting_for_capacity",
    "waiting_for_quota",
    "waiting_for_reservation",
    "waiting_for_retry",
    "waiting_for_compatibility",
)
WORKER_HEALTH = ("STARTING", "READY", "SUSPECT", "UNAVAILABLE")
WORKER_ADMIN = ("ENABLED", "DRAINING", "DISABLED")
MODES = ("NORMAL", "ADMISSION_OFF", "WRITE_FROZEN")
COLLECTORS = metrics_api.COLLECTORS
FAIRNESS_WINDOW = timedelta(hours=1)


def read_only_transaction(session: Session, statement_timeout_ms: int) -> None:
    """Make the current transaction read-only and bound every statement in it."""
    session.execute(text("SET TRANSACTION READ ONLY"))
    session.execute(
        text("SELECT pg_catalog.set_config('statement_timeout', :value, true)"),
        {"value": str(int(statement_timeout_ms))},
    )
    session.execute(
        text("SELECT pg_catalog.set_config('lock_timeout', :value, true)"),
        {"value": str(int(statement_timeout_ms))},
    )


def _gauge(name: str, documentation: str, labels: tuple[str, ...] = ()) -> GaugeMetricFamily:
    return GaugeMetricFamily(name, documentation, labels=list(labels) if labels else None)


def _queue(session: Session) -> list[Metric]:
    jobs = _gauge("nexa_jobs", "Non-terminal jobs by state", ("state",))
    counts = dict.fromkeys(ACTIVE_JOB_STATES, 0)
    for state, count in session.execute(
        text(
            "SELECT state, count(*) FROM jobs "
            "WHERE state NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED') GROUP BY state"
        )
    ):
        if state in counts:
            counts[state] = int(count)
    for state, count in counts.items():
        jobs.add_metric([state], count)
    waiting = _gauge(
        "nexa_jobs_waiting", "Non-terminal jobs by waiting reason", ("waiting_reason",)
    )
    reasons = dict.fromkeys(WAITING_REASONS, 0)
    for reason, count in session.execute(
        text(
            "SELECT waiting_reason, count(*) FROM jobs "
            "WHERE state NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED') "
            "AND waiting_reason IS NOT NULL GROUP BY waiting_reason"
        )
    ):
        if reason in reasons:
            reasons[reason] = int(count)
    for reason, count in reasons.items():
        waiting.add_metric([reason], count)
    oldest = session.execute(
        text(
            "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - min(eligible_since))), 0) "
            "FROM jobs WHERE state = 'QUEUED'"
        )
    ).scalar_one()
    outstanding = session.execute(
        text(
            "SELECT COALESCE(sum(outstanding), 0) FROM admission_counters "
            "WHERE scope_type = 'GLOBAL'"
        )
    ).scalar_one()
    limit = session.execute(
        text("SELECT global_outstanding_limit FROM policy_versions WHERE is_current")
    ).scalar_one_or_none()
    age = _gauge("nexa_queue_oldest_age_seconds", "Age of the oldest eligible queued job (DB time)")
    age.add_metric([], max(0.0, float(oldest)))
    current = _gauge("nexa_queue_outstanding", "Global outstanding jobs counter")
    current.add_metric([], int(outstanding))
    families: list[Metric] = [jobs, waiting, age, current]
    if limit is not None:
        bound = _gauge("nexa_queue_outstanding_limit", "Global outstanding job limit")
        bound.add_metric([], int(limit))
        families.append(bound)
    return families


def _allocation(session: Session) -> list[Metric]:
    allocations = _gauge("nexa_allocations", "Unreleased allocations by state", ("state",))
    resource = _gauge(
        "nexa_allocated_resource", "Resources held by unreleased allocations", ("resource",)
    )
    totals = {"cpu_millis": 0, "memory_bytes": 0, "gpu": 0}
    counts = {"HELD": 0, "QUARANTINED": 0}
    for state, count, cpu, memory, gpu in session.execute(
        text(
            "SELECT state, count(*), COALESCE(sum(cpu_millis), 0), "
            "COALESCE(sum(memory_bytes), 0), COALESCE(sum(gpu_count), 0) "
            "FROM allocations WHERE state IN ('HELD', 'QUARANTINED') GROUP BY state"
        )
    ):
        counts[state] = int(count)
        totals["cpu_millis"] += int(cpu)
        totals["memory_bytes"] += int(memory)
        totals["gpu"] += int(gpu)
    for state, count in counts.items():
        allocations.add_metric([state], count)
    for name, value in totals.items():
        resource.add_metric([name], value)
    reservations = session.execute(
        text("SELECT count(*) FROM reservations WHERE invalidated_at IS NULL")
    ).scalar_one()
    quarantine_age = session.execute(
        text(
            "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - min(quarantined_at))), 0) "
            "FROM allocations WHERE state = 'QUARANTINED'"
        )
    ).scalar_one()
    active = _gauge("nexa_reservations_active", "Active local reservations")
    active.add_metric([], int(reservations))
    oldest = _gauge(
        "nexa_quarantine_oldest_age_seconds", "Age of the oldest quarantined allocation"
    )
    oldest.add_metric([], max(0.0, float(quarantine_age)))
    return [allocations, resource, active, oldest]


def _fairness(session: Session) -> list[Metric]:
    to_at = session.execute(text("SELECT date_trunc('milliseconds', now())")).scalar_one()
    rows = fairness_rows(
        session,
        from_at=to_at - FAIRNESS_WINDOW,
        to_at=to_at,
        bucket_seconds=int(FAIRNESS_WINDOW.total_seconds()),
        tenant_id=None,
    )
    service: dict[Any, float] = {}
    dominant = 0.0
    for row in rows:
        service[row.tenant_id] = service.get(row.tenant_id, 0.0) + float(row.normalized_service)
        dominant += float(row.dominant_resource_time)
    families: list[Metric] = []
    drt = _gauge(
        "nexa_fairness_dominant_resource_seconds",
        "System dominant resource-time charged in the window",
        ("window",),
    )
    drt.add_metric(["1h"], dominant)
    families.append(drt)
    index = jain_index(service.values())
    if index is not None:
        jain = _gauge(
            "nexa_fairness_jain_index",
            "Jain index of weighted normalized service across tenants that ran",
            ("window",),
        )
        jain.add_metric(["1h"], index)
        families.append(jain)
    return families


def _checkpoint(session: Session) -> list[Metric]:
    # A resumed attempt inherits the job's checkpoints; its age counts from the later of
    # the newest checkpoint and its own start, so recovery downtime is not reported as
    # a stale checkpoint (B19-RV11). GREATEST ignores a NULL (no checkpoint yet).
    age = session.execute(
        text(
            "SELECT COALESCE(max(EXTRACT(EPOCH FROM (now() - GREATEST("
            "(SELECT max(c.created_at) FROM checkpoints c WHERE c.job_id = a.job_id), "
            "COALESCE(a.started_at, a.created_at))))), 0) "
            "FROM attempts a JOIN job_specs js ON js.job_id = a.job_id "
            "WHERE a.state IN ('RUNNING', 'CHECKPOINTING') "
            "AND js.checkpoint_interval_seconds IS NOT NULL"
        )
    ).scalar_one()
    gauge = _gauge(
        "nexa_checkpoint_age_seconds",
        "Largest time since the last committed checkpoint or the attempt start, whichever is "
        "later, among running checkpointable attempts",
    )
    gauge.add_metric([], max(0.0, float(age)))
    return [gauge]


def _worker(session: Session) -> list[Metric]:
    workers = _gauge("nexa_workers", "Workers by health and admin state", ("health", "admin_state"))
    counts = {(health, admin): 0 for health in WORKER_HEALTH for admin in WORKER_ADMIN}
    for health, admin, count in session.execute(
        text("SELECT health, admin_state, count(*) FROM workers GROUP BY health, admin_state")
    ):
        if (health, admin) in counts:
            counts[(health, admin)] = int(count)
    for (health, admin), count in counts.items():
        workers.add_metric([health, admin], count)
    heartbeat = session.execute(
        text(
            "SELECT EXTRACT(EPOCH FROM (now() - min(last_heartbeat_at))) FROM workers "
            "WHERE admin_state = 'ENABLED' AND last_heartbeat_at IS NOT NULL"
        )
    ).scalar_one_or_none()
    mode = session.execute(
        text("SELECT operational_mode FROM policy_versions WHERE is_current")
    ).scalar_one_or_none()
    families: list[Metric] = [workers]
    if heartbeat is not None:
        age = _gauge(
            "nexa_worker_heartbeat_age_seconds", "Largest heartbeat age of an enabled worker"
        )
        age.add_metric([], max(0.0, float(heartbeat)))
        families.append(age)
    modes = _gauge("nexa_operational_mode", "Current operational mode (1 = active)", ("mode",))
    for name in MODES:
        modes.add_metric([name], 1 if name == mode else 0)
    families.append(modes)
    return families


class ApiStateCollector(Collector):
    def __init__(
        self,
        engine: Engine,
        *,
        disk_usage: Callable[[], tuple[int, int, float]],
        cache_seconds: int,
        statement_timeout_ms: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._disk_usage = disk_usage
        self._ttl = cache_seconds
        self._timeout_ms = statement_timeout_ms
        self._clock = clock
        self._lock = threading.Lock()
        self._refreshed_at: float | None = None
        self._families: dict[str, list[Metric]] = {}
        self._status: dict[str, tuple[int, float]] = {}
        self._readers: dict[str, Callable[[Session], list[Metric]]] = {
            "queue": _queue,
            "allocation": _allocation,
            "fairness": _fairness,
            "checkpoint": _checkpoint,
            "worker": _worker,
            "storage": self._storage,
        }

    def describe(self) -> list[Metric]:
        return []  # dynamic families; never query the database at registration

    def collect(self) -> Iterator[Metric]:
        self.refresh_if_stale()
        up = _gauge("nexa_collector_up", "Last collector refresh succeeded (1)", ("collector",))
        duration = _gauge(
            "nexa_collector_duration_seconds", "Duration of the last refresh", ("collector",)
        )
        for name in COLLECTORS:
            state, seconds = self._status.get(name, (0, 0.0))
            up.add_metric([name], state)
            duration.add_metric([name], seconds)
        yield up
        yield duration
        for name in COLLECTORS:
            yield from self._families.get(name, ())

    def refresh_if_stale(self) -> None:
        with self._lock:
            now = self._clock()
            if self._refreshed_at is not None and now - self._refreshed_at < self._ttl:
                return
            for name in COLLECTORS:
                self._refresh(name)
            self._refreshed_at = self._clock()

    def _refresh(self, name: str) -> None:
        started = time.perf_counter()
        try:
            with Session(self._engine) as session, session.begin():
                read_only_transaction(session, self._timeout_ms)
                families = self._readers[name](session)
        except Exception as exc:  # noqa: BLE001 - one collector failing must not fail a scrape
            self._families.pop(name, None)
            self._status[name] = (0, time.perf_counter() - started)
            guarded(metrics_api.COLLECTOR_ERRORS.labels(name).inc)
            log_event(
                _LOG,
                logging.WARNING,
                "metrics_collector_failed",
                collector=name,
                reason=type(exc).__name__,
            )
            return
        self._families[name] = families
        self._status[name] = (1, time.perf_counter() - started)

    def _storage(self, session: Session) -> list[Metric]:
        total, free, _used_percent = self._disk_usage()
        committed, reserved = session.execute(
            text(
                "SELECT COALESCE(sum(committed_bytes), 0), COALESCE(sum(reserved_bytes), 0) "
                "FROM artifact_storage_counters"
            )
        ).one()
        used = _gauge("nexa_storage_used_ratio", "Used fraction of the artifact filesystem")
        used.add_metric([], (total - free) / total if total > 0 else 0.0)
        free_bytes = _gauge("nexa_storage_free_bytes", "Free bytes on the artifact filesystem")
        free_bytes.add_metric([], free)
        committed_bytes = _gauge(
            "nexa_storage_committed_bytes", "Committed artifact bytes across all tenants"
        )
        committed_bytes.add_metric([], int(committed))
        reserved_bytes = _gauge(
            "nexa_storage_reserved_bytes", "Reserved upload bytes across all tenants"
        )
        reserved_bytes.add_metric([], int(reserved))
        return [used, free_bytes, committed_bytes, reserved_bytes]
