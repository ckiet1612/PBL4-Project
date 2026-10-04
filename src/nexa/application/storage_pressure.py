"""Storage watermark, byte-quota pre-check and store error wire codes (B19-R06/R07/R08).

| operation | u < high | high <= u < critical | u >= critical |
|---|---|---|---|
| admission (submitJob/createSweep) | accept | 503 storage_pressure | 503 storage_pressure |
| user upload | accept | 503 storage_pressure | 503 storage_pressure |
| worker upload (checkpoint/result/chunk) | accept | accept | 503 storage_pressure |

The disk reading is taken before the transaction (statvfs cached for at most one
second per process) and enforced inside it after the idempotency replay, so a
replay still returns its stored response on a full disk. A failed statvfs fails
closed with 503 dependency_unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexa.application.errors import ApplicationError
from nexa.config import Settings
from nexa.infrastructure.artifacts.store import ArtifactError
from nexa.infrastructure.persistence.schema import artifact_storage_counters
from nexa.observability import metrics_api
from nexa.observability.logging import log_event

ADMISSION = "admission"
USER_UPLOAD = "user_upload"
WORKER_UPLOAD = "worker_upload"
CAPACITY_CACHE_SECONDS = 1.0
# A stream re-checks the watermark after every STREAM_CHECK_BYTES received.
STREAM_CHECK_BYTES = 1024 * 1024
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StorageReading:
    total: int
    free: int

    def used_percent(self, extra_bytes: int = 0) -> float:
        return ((self.total - self.free + extra_bytes) / self.total) * 100


def unavailable() -> ApplicationError:
    return ApplicationError(
        code="dependency_unavailable",
        status=503,
        message="Artifact storage capacity cannot be checked",
        retry_after=1,
    )


def pressure() -> ApplicationError:
    return ApplicationError(
        code="storage_pressure",
        status=503,
        message="Artifact storage is under pressure",
        retry_after=1,
    )


def read_storage(store, *, fresh: bool = False) -> StorageReading | ApplicationError:
    """Read capacity outside any transaction; an error is returned to enforce after replay."""
    try:
        total, free, _used = store.disk_usage(
            max_age_seconds=0.0 if fresh else CAPACITY_CACHE_SECONDS
        )
    except ArtifactError:
        return unavailable()
    return StorageReading(total, free)


def enforce_watermark(
    reading: StorageReading | ApplicationError,
    operation: str,
    settings: Settings,
    *,
    extra_bytes: int = 0,
) -> None:
    if isinstance(reading, ApplicationError):
        metrics_api.storage_rejection("unavailable", operation)
        raise reading
    used = reading.used_percent(extra_bytes)
    critical = settings.storage_critical_watermark_percent
    threshold = critical if operation == WORKER_UPLOAD else settings.storage_high_watermark_percent
    if used >= threshold:
        reason = "critical_watermark" if used >= critical else "high_watermark"
        metrics_api.storage_rejection(reason, operation)
        raise pressure()


def enforce_byte_quota(session: Session, tenant_id, settings: Settings) -> None:
    """Admission pre-check: no lock and no reservation; upload reserves exactly."""
    row = (
        session.execute(
            select(
                artifact_storage_counters.c.committed_bytes,
                artifact_storage_counters.c.reserved_bytes,
            ).where(artifact_storage_counters.c.tenant_id == tenant_id)
        )
        .mappings()
        .one_or_none()
    )
    used = 0 if row is None else int(row["committed_bytes"]) + int(row["reserved_bytes"])
    if used >= settings.tenant_artifact_quota_bytes:
        metrics_api.storage_rejection("quota", ADMISSION)
        raise ApplicationError(
            code="quota_exceeded",
            status=429,
            message="Tenant artifact quota exceeded",
            retry_after=1,
        )


def store_failure(exc: ArtifactError, operation: str) -> ApplicationError:
    """Map a store error to a wire ErrorCode; internal store codes never reach clients."""
    if exc.code == "payload_too_large":
        return ApplicationError(code="payload_too_large", status=413, message=exc.message)
    if exc.code == "checksum_mismatch":
        return ApplicationError(code="checksum_mismatch", status=422, message=exc.message)
    if exc.code in {"size_mismatch", "validation_failed"}:
        return ApplicationError(code="validation_failed", status=422, message=exc.message)
    if exc.code == "storage_full":
        metrics_api.storage_rejection("enospc", operation)
        return pressure()
    # The client only sees dependency_unavailable; the internal code stays in the log
    # (never the message, which may name a path) for the operator (B19-RV04).
    log_event(
        _LOG, logging.WARNING, "artifact_store_unavailable", code=exc.code, operation=operation
    )
    return ApplicationError(
        code="dependency_unavailable",
        status=503,
        message="Artifact storage is unavailable",
        retry_after=1,
    )
