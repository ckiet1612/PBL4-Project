"""Shared Prometheus helpers: closed label sets, after-commit events, guarded updates.

Every process (API, coordinator, worker) owns one `CollectorRegistry` declared in its
own module; nothing uses the global default registry. Event metrics run only after the
SQLAlchemy session commits (`after_commit`) and are dropped on rollback, so a retried
transaction never counts twice. A failure inside metric code is swallowed and counted
in `nexa_metrics_internal_errors_total`; it never changes a request, tick or callback.
"""

import itertools
import logging
from collections.abc import Callable, Iterable
from contextlib import suppress
from typing import Any

from prometheus_client import CollectorRegistry, Counter, generate_latest
from sqlalchemy import event
from sqlalchemy.orm import Session

_LOG = logging.getLogger(__name__)
_PENDING = "nexa_metrics_after_commit"

# Registered into every process registry (a collector may belong to several).
INTERNAL_ERRORS = Counter(
    "nexa_metrics_internal_errors_total",
    "Failures inside metric code; never affects the operation being measured",
    registry=None,
)

HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
STATUS_CLASSES = ("1xx", "2xx", "3xx", "4xx", "5xx")


def new_registry() -> CollectorRegistry:
    registry = CollectorRegistry(auto_describe=True)
    registry.register(INTERNAL_ERRORS)
    return registry


def internal_error() -> None:
    with suppress(Exception):  # the error counter itself must never raise
        INTERNAL_ERRORS.inc()


def guarded(update: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
    """Run one metric update; swallow and count any failure."""
    try:
        update(*args, **kwargs)
    except Exception:  # noqa: BLE001 - metrics never fail the measured operation
        internal_error()


def closed(value: object, allowed: Iterable[str], fallback: str) -> str:
    """Map a label value into its closed set; anything unknown becomes `fallback`."""
    text = str(value) if value is not None else fallback
    return text if text in allowed else fallback


def preinitialize(metric: Any, *label_sets: Iterable[str]) -> None:
    """Expose every closed label combination at 0.

    A labelled counter series that first appears at 1 is invisible to `increase()` and
    `rate()`, so an alert on it would miss the first event (B19-RV03).
    """
    for values in itertools.product(*label_sets):
        metric.labels(*values)


def method_label(method: object) -> str:
    """HTTP method in its closed set; shared by the access log and the metrics (B19-RV01)."""
    return closed(method, HTTP_METHODS, "OTHER")


def after_commit(session: Session, update: Callable[[], Any]) -> None:
    """Queue `update` to run once the session's outermost transaction commits.

    An update queued inside a savepoint is dropped if that savepoint rolls back.
    """
    try:
        scope = session.get_nested_transaction()
        session.info.setdefault(_PENDING, []).append((scope, update))
    except Exception:  # noqa: BLE001 - e.g. a test double without `info`
        internal_error()


def _within(scope: Any, ended: Any) -> bool:
    while scope is not None:
        if scope is ended:
            return True
        scope = scope.parent
    return False


def _fire(session: Session) -> None:
    if session.in_nested_transaction():
        return  # a released savepoint; the outer transaction may still roll back
    pending = session.info.pop(_PENDING, None)
    for _scope, update in pending or ():
        guarded(update)


def _discard(session: Session, previous_transaction: Any) -> None:
    pending = session.info.get(_PENDING)
    if not pending:
        return
    if not previous_transaction.nested:
        session.info.pop(_PENDING, None)
        return
    session.info[_PENDING] = [
        (scope, update) for scope, update in pending if not _within(scope, previous_transaction)
    ]


if not event.contains(Session, "after_commit", _fire):
    event.listen(Session, "after_commit", _fire)
    event.listen(Session, "after_soft_rollback", _discard)


def exposition(registry: CollectorRegistry) -> bytes:
    """Prometheus text format 0.0.4 for one process registry."""
    return generate_latest(registry)


def series_count(registry: CollectorRegistry) -> int:
    return sum(len(family.samples) for family in registry.collect())


def label_values(registry: CollectorRegistry) -> dict[str, set[str]]:
    """All label names with every value currently exposed (tests and allowlists)."""
    values: dict[str, set[str]] = {}
    for family in registry.collect():
        for sample in family.samples:
            for name, value in sample.labels.items():
                values.setdefault(name, set()).add(value)
    return values
