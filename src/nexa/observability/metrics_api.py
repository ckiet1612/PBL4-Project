"""API process metrics (docs/observability.md §1.1). Labels are closed sets; no IDs."""

from prometheus_client import Counter, Gauge, Histogram

from nexa.observability.metrics import (
    STATUS_CLASSES,
    closed,
    guarded,
    method_label,
    new_registry,
    preinitialize,
)

REGISTRY = new_registry()

ERROR_CODES = frozenset(
    {
        "authentication_required",
        "invalid_csrf",
        "permission_denied",
        "resource_not_found",
        "validation_failed",
        "invalid_cursor",
        "idempotency_conflict",
        "idempotency_in_progress",
        "one_time_secret_unavailable",
        "precondition_required",
        "version_conflict",
        "state_conflict",
        "infeasible_request",
        "rate_limited",
        "quota_exceeded",
        "queue_full",
        "dependency_unavailable",
        "checksum_mismatch",
        "payload_too_large",
        "storage_pressure",
        "stale_authority",
        "lease_expired",
        "callback_replayed",
        "internal_error",
    }
)
ADMISSION_KINDS = ("job", "sweep")
ADMISSION_OUTCOMES = ("accepted", "replayed", "rejected")
STORAGE_REJECTION_REASONS = (
    "high_watermark",
    "critical_watermark",
    "quota",
    "enospc",
    "unavailable",
)
STORAGE_OPERATIONS = ("admission", "user_upload", "worker_upload")
CHECKSUM_OPERATIONS = ("upload", "restore", "download")
CALLBACK_REJECTION_CODES = (
    "stale_authority",
    "lease_expired",
    "callback_replayed",
    "state_conflict",
    "version_conflict",
)
FAILURE_CLASSES = ("INFRASTRUCTURE", "TIMEOUT", "OOM", "INVALID_INPUT", "INCOMPATIBLE", "INTERNAL")
RESTORE_OUTCOMES = ("selected", "fallback", "none", "failed")
GC_KINDS = ("staging", "orphan")
GC_ERROR_KINDS = ("staging", "orphan", "expire", "reconcile")
READY_CHECKS = ("database", "schema", "storage")
COLLECTORS = ("queue", "allocation", "fairness", "checkpoint", "worker", "storage")
UNMATCHED_ROUTE = "unmatched"

HTTP_REQUESTS = Counter(
    "nexa_http_requests_total",
    "REST requests by route template, method and status class",
    ("route", "method", "status_class"),
    registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "nexa_http_request_duration_seconds",
    "REST request duration by route template and method",
    ("route", "method"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)
ADMISSION = Counter(
    "nexa_admission_total",
    "Job and sweep admission decisions; reason is the ErrorCode or none",
    ("kind", "outcome", "reason"),
    registry=REGISTRY,
)
STORAGE_REJECTIONS = Counter(
    "nexa_storage_rejections_total",
    "Requests refused by a storage watermark, byte quota, ENOSPC or unavailable storage",
    ("reason", "operation"),
    registry=REGISTRY,
)
CHECKSUM_ERRORS = Counter(
    "nexa_checksum_errors_total",
    "Checksum mismatches detected by operation",
    ("operation",),
    registry=REGISTRY,
)
CALLBACK_REJECTED = Counter(
    "nexa_worker_callback_rejected_total",
    "Worker callbacks rejected as stale, expired, replayed or conflicting",
    ("route", "code"),
    registry=REGISTRY,
)
RETRY_SCHEDULED = Counter(
    "nexa_retry_scheduled_total",
    "Automatic retries scheduled after commit, by failure class",
    ("reason",),
    registry=REGISTRY,
)
RESTORE = Counter(
    "nexa_restore_total",
    "Restore decisions committed with a claim",
    ("outcome",),
    registry=REGISTRY,
)
UPLOADS_EXPIRED = Counter(
    "nexa_upload_sessions_expired_total",
    "Upload sessions expired by GC G1",
    registry=REGISTRY,
)
GC_DELETED = Counter("nexa_gc_deleted_total", "Files deleted by GC", ("kind",), registry=REGISTRY)
GC_BYTES_DELETED = Counter(
    "nexa_gc_bytes_deleted_total", "Bytes deleted by GC", ("kind",), registry=REGISTRY
)
GC_ERRORS = Counter(
    "nexa_gc_errors_total", "GC and reconciliation pass failures", ("kind",), registry=REGISTRY
)
GC_LAST_RUN = Gauge(
    "nexa_gc_last_run_timestamp_seconds",
    "Unix time of the last completed GC pass",
    registry=REGISTRY,
)
STAGING_FILES = Gauge("nexa_storage_staging_files", "Staging files seen by GC", registry=REGISTRY)
STAGING_BYTES = Gauge("nexa_storage_staging_bytes", "Staging bytes seen by GC", registry=REGISTRY)
ORPHAN_FILES = Gauge(
    "nexa_storage_orphan_files", "Orphan blobs found by the last G2 scan", registry=REGISTRY
)
ORPHAN_BYTES = Gauge(
    "nexa_storage_orphan_bytes", "Orphan bytes found by the last G2 scan", registry=REGISTRY
)
RETAINED_ARTIFACTS = Gauge(
    "nexa_storage_retained_unpublished_artifacts",
    "Committed artifacts kept but never published as a checkpoint or result (B19-R05)",
    registry=REGISTRY,
)
RETAINED_BYTES = Gauge(
    "nexa_storage_retained_unpublished_bytes",
    "Bytes of retained unpublished artifacts (B19-R05)",
    registry=REGISTRY,
)
COUNTER_DRIFT = Gauge(
    "nexa_storage_counter_drift_tenants",
    "Tenants whose committed byte counter differs from the artifact sum",
    registry=REGISTRY,
)
RECONCILE_LAST = Gauge(
    "nexa_storage_reconcile_last_timestamp_seconds",
    "Unix time of the last storage counter reconciliation",
    registry=REGISTRY,
)
WATERMARK = Gauge(
    "nexa_storage_watermark_ratio",
    "Configured storage watermark",
    ("level",),
    registry=REGISTRY,
)
READY = Gauge("nexa_ready", "Readiness check result (1 ok)", ("check",), registry=REGISTRY)
COLLECTOR_ERRORS = Counter(
    "nexa_collector_errors_total",
    "Collector refresh failures",
    ("collector",),
    registry=REGISTRY,
)

preinitialize(STORAGE_REJECTIONS, STORAGE_REJECTION_REASONS, STORAGE_OPERATIONS)
preinitialize(CHECKSUM_ERRORS, CHECKSUM_OPERATIONS)
preinitialize(RETRY_SCHEDULED, FAILURE_CLASSES)
preinitialize(RESTORE, RESTORE_OUTCOMES)
preinitialize(GC_DELETED, GC_KINDS)
preinitialize(GC_BYTES_DELETED, GC_KINDS)
preinitialize(GC_ERRORS, GC_ERROR_KINDS)
preinitialize(COLLECTOR_ERRORS, COLLECTORS)


def status_class(status: int) -> str:
    return closed(f"{status // 100}xx", STATUS_CLASSES, "5xx")


def observe_http(route: str, method: str, status: int, seconds: float) -> None:
    method = method_label(method)
    guarded(lambda: HTTP_REQUESTS.labels(route, method, status_class(status)).inc())
    guarded(lambda: HTTP_DURATION.labels(route, method).observe(seconds))


def admission(kind: str, outcome: str, reason: str | None = None) -> None:
    guarded(
        lambda: ADMISSION.labels(
            closed(kind, ADMISSION_KINDS, "job"),
            closed(outcome, ADMISSION_OUTCOMES, "rejected"),
            closed(reason, ERROR_CODES, "none"),
        ).inc()
    )


def storage_rejection(reason: str, operation: str) -> None:
    guarded(
        lambda: STORAGE_REJECTIONS.labels(
            closed(reason, STORAGE_REJECTION_REASONS, "unavailable"),
            closed(operation, STORAGE_OPERATIONS, "admission"),
        ).inc()
    )


def checksum_error(operation: str) -> None:
    guarded(lambda: CHECKSUM_ERRORS.labels(closed(operation, CHECKSUM_OPERATIONS, "upload")).inc())


def callback_rejected(route: str, code: str) -> None:
    if code in CALLBACK_REJECTION_CODES:
        guarded(lambda: CALLBACK_REJECTED.labels(route, code).inc())


def retry_scheduled(failure_class: str) -> None:
    reason = closed(failure_class, FAILURE_CLASSES, "INTERNAL")
    guarded(lambda: RETRY_SCHEDULED.labels(reason).inc())


def restore(outcome: str) -> None:
    guarded(lambda: RESTORE.labels(closed(outcome, RESTORE_OUTCOMES, "none")).inc())


def set_ready(checks: dict[str, str]) -> None:
    for name in READY_CHECKS:
        value = 1 if checks.get(name) in {"ok", "skip"} else 0
        guarded(lambda name=name, value=value: READY.labels(name).set(value))


def configure_watermarks(high_percent: int, critical_percent: int) -> None:
    guarded(WATERMARK.labels("high").set, high_percent / 100)
    guarded(WATERMARK.labels("critical").set, critical_percent / 100)
