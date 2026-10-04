"""Storage GC G1/G2 and counter reconciliation, run by the API process (B19 6.E–6.H).

The API is the only process with the artifact volume. One pass at a time runs per
database (session advisory lock); each pass is bounded in items and seconds and
stops early once `stop()` is called. Steps run in this order and fail independently:

- G1 expire: `ArtifactService.expire_uploads` moves overdue ACTIVE sessions to
  EXPIRED and releases their reservation, audited as SYSTEM.
- G1 staging: a staging file older than the staging TTL, not held open by this
  process and not the staged key of an ACTIVE session is removed. A session expires
  `staging_ttl` after it began and its file was created after that, so a live upload
  is never older than the TTL; the ACTIVE check also covers sessions not yet expired.
  Staged keys come from fresh UUIDv7s, so an old file can never become ACTIVE again.
- G2 orphan: a committed blob older than the orphan TTL with no `artifacts` row of
  any state is removed under the exclusive per-blob advisory lock; the metadata
  commit holds the same lock shared and checks the blob exists before inserting, so
  either the row wins (G2 sees it and skips) or G2 wins (the commit fails 503 and
  releases its reservation). The unlink is a short file operation inside the locked
  transaction by design; a crash after the unlink loses only the audit row.
- Reconcile: read-only counter drift and retained unpublished bytes (B19-R05/R14).

GC never changes artifact state, never removes a blob that has metadata and never
prunes checkpoints (B19-R05). `issue_gc_token`/`delete_unreferenced` are not used:
an orphan has no metadata checksum to claim, and hashing every candidate would read
the whole blob; the advisory lock and the in-lock row check give the same guarantee.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Engine, func, insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from nexa.application.artifact_service import GC_ACTOR_ID, ArtifactService
from nexa.application.errors import ApplicationError
from nexa.application.storage_checks import counter_drift, retained_unpublished
from nexa.config import Settings
from nexa.infrastructure.artifacts.store import ArtifactError, StoredFile
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import GC_RUN_LOCK_CLASS, lock_blob_key
from nexa.infrastructure.persistence.schema import artifacts, audit_records, upload_sessions
from nexa.infrastructure.persistence.transactions import (
    TransactionRetryExhausted,
    run_transaction,
)
from nexa.observability import metrics_api
from nexa.observability.logging import log_event
from nexa.observability.metrics import guarded

_LOG = logging.getLogger(__name__)
_FAILURES = (ArtifactError, ApplicationError, SQLAlchemyError, TransactionRetryExhausted)
_LOOKUP_BATCH = 500


@dataclass(slots=True)
class GcPass:
    expired: int = 0
    staging_deleted: int = 0
    staging_bytes_deleted: int = 0
    orphans_deleted: int = 0
    orphan_bytes_deleted: int = 0
    drift_tenants: int | None = None
    failed: tuple[str, ...] = ()


class _Budget:
    def __init__(self, items: int, deadline: float, clock: Callable[[], float], stopped):
        self.items = items
        self._deadline = deadline
        self._clock = clock
        self._stopped = stopped

    def spent(self) -> bool:
        return self.items <= 0 or self._stopped.is_set() or self._clock() >= self._deadline

    def take(self) -> None:
        self.items -= 1


class StorageGc:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        artifacts: ArtifactService,
        *,
        max_items: int = 500,
        max_seconds: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._artifacts = artifacts
        self._store = artifacts.store
        self._session_factory = artifacts.session_factory
        self._max_items = max_items
        self._max_seconds = max_seconds
        self._monotonic = monotonic
        self._wall = wall
        self._stopped = threading.Event()

    def stop(self) -> None:
        """Make a running pass return at its next item (process shutdown)."""
        self._stopped.set()

    def run_once(self) -> GcPass | None:
        """One bounded pass; None when another pass holds the run lock."""
        with self._engine.connect() as connection:
            key = (GC_RUN_LOCK_CLASS, 0)
            locked = connection.execute(select(func.pg_try_advisory_lock(*key))).scalar_one()
            connection.commit()  # the session-level lock outlives the transaction
            if not locked:
                return None
            try:
                return self._pass()
            finally:
                try:
                    connection.execute(select(func.pg_advisory_unlock(*key)))
                    connection.commit()
                except SQLAlchemyError:
                    # Never return a connection that may still hold the lock to the pool.
                    connection.invalidate()

    def _pass(self) -> GcPass:
        budget = _Budget(
            self._max_items, self._monotonic() + self._max_seconds, self._monotonic, self._stopped
        )
        result = GcPass()
        failed: list[str] = []
        for kind, step in (
            ("expire", self._expire),
            ("staging", self._sweep_staging),
            ("orphan", self._sweep_orphans),
            ("reconcile", self._reconcile),
        ):
            try:
                step(budget, result)
            except _FAILURES as exc:
                failed.append(kind)
                guarded(metrics_api.GC_ERRORS.labels(kind).inc)
                log_event(
                    _LOG, logging.WARNING, "gc_step_failed", kind=kind, error=type(exc).__name__
                )
        result.failed = tuple(failed)
        if not failed:
            # Only a fully successful pass counts, so a failing GC also trips NexaGcStalled.
            guarded(metrics_api.GC_LAST_RUN.set, self._wall())
        log_event(
            _LOG,
            logging.INFO,
            "gc_pass",
            expired=result.expired,
            staging_deleted=result.staging_deleted,
            staging_bytes_deleted=result.staging_bytes_deleted,
            orphans_deleted=result.orphans_deleted,
            orphan_bytes_deleted=result.orphan_bytes_deleted,
            drift_tenants=result.drift_tenants,
            failed=list(failed),
        )
        return result

    def _expire(self, budget: _Budget, result: GcPass) -> None:
        while not budget.spent():
            limit = min(budget.items, 1000)
            expired = self._artifacts.expire_uploads(limit=limit)
            result.expired += expired
            budget.items -= expired
            if expired < limit:
                return

    def _old_files(
        self, area: str, ttl_seconds: int, budget: _Budget
    ) -> tuple[list[StoredFile], tuple[int, int] | None]:
        """Files older than the TTL, and (files, bytes) seen if the scan completed."""
        now = self._wall()
        old: list[StoredFile] = []
        seen_files = seen_bytes = 0
        for file in self._store.scan(area):
            if budget.spent() or len(old) >= budget.items:
                return old, None
            seen_files += 1
            seen_bytes += file.size_bytes
            if file.age_seconds(now) > ttl_seconds:
                old.append(file)
        return old, (seen_files, seen_bytes)

    def _sweep_staging(self, budget: _Budget, result: GcPass) -> None:
        old, seen = self._old_files("staging", self._settings.staging_ttl_seconds, budget)
        active: set[str] = set()
        for start in range(0, len(old), _LOOKUP_BATCH):
            keys = [file.key for file in old[start : start + _LOOKUP_BATCH]]
            with self._session_factory() as session:
                active.update(
                    session.execute(
                        select(upload_sessions.c.staged_key).where(
                            upload_sessions.c.staged_key.in_(keys),
                            upload_sessions.c.state == "ACTIVE",
                        )
                    ).scalars()
                )
        deleted = deleted_bytes = 0
        for file in old:
            if budget.spent():
                break
            budget.take()
            if file.key not in active and self._store.remove_scanned(file):
                deleted += 1
                deleted_bytes += file.size_bytes
        if seen is not None:
            # What stays after this pass; a truncated scan leaves the last value.
            guarded(metrics_api.STAGING_FILES.set, seen[0] - deleted)
            guarded(metrics_api.STAGING_BYTES.set, seen[1] - deleted_bytes)
        if not deleted:
            return
        result.staging_deleted += deleted
        result.staging_bytes_deleted += deleted_bytes
        guarded(metrics_api.GC_DELETED.labels("staging").inc, deleted)
        guarded(metrics_api.GC_BYTES_DELETED.labels("staging").inc, deleted_bytes)

        def audit(session: Session) -> None:
            session.execute(
                insert(audit_records).values(
                    audit_id=new_uuid7(),
                    actor_type="SYSTEM",
                    actor_id=GC_ACTOR_ID,
                    tenant_id=None,
                    action="artifact.gc.staging",
                    target_type="ARTIFACT_STORE",
                    target_id="staging",
                    reason="Expired staging files removed",
                    safe_metadata={"files": deleted, "bytes": deleted_bytes},
                )
            )

        # The files are already gone: a failed audit write is a GC error, not a rollback.
        run_transaction(self._session_factory, audit)

    def _sweep_orphans(self, budget: _Budget, result: GcPass) -> None:
        old, _seen = self._old_files("blobs", self._settings.orphan_ttl_seconds, budget)
        referenced: set[str] = set()
        for start in range(0, len(old), _LOOKUP_BATCH):
            keys = [file.key for file in old[start : start + _LOOKUP_BATCH]]
            with self._session_factory() as session:
                referenced.update(
                    session.execute(
                        select(artifacts.c.blob_key).where(artifacts.c.blob_key.in_(keys))
                    ).scalars()
                )
        orphans = [file for file in old if file.key not in referenced]
        if _seen is not None:
            guarded(metrics_api.ORPHAN_FILES.set, len(orphans))
            guarded(metrics_api.ORPHAN_BYTES.set, sum(file.size_bytes for file in orphans))
        for file in orphans:
            if budget.spent():
                return
            budget.take()
            if run_transaction(self._session_factory, lambda s, f=file: self._remove_orphan(s, f)):
                result.orphans_deleted += 1
                result.orphan_bytes_deleted += file.size_bytes
                guarded(metrics_api.GC_DELETED.labels("orphan").inc)
                guarded(metrics_api.GC_BYTES_DELETED.labels("orphan").inc, file.size_bytes)

    def _remove_orphan(self, session: Session, file: StoredFile) -> bool:
        lock_blob_key(session, file.key, exclusive=True)
        if session.execute(
            select(artifacts.c.artifact_id).where(artifacts.c.blob_key == file.key)
        ).first():
            return False  # the metadata commit won; never touch a blob with a row
        if not self._store.remove_scanned(file):
            return False
        session.execute(
            insert(audit_records).values(
                audit_id=new_uuid7(),
                actor_type="SYSTEM",
                actor_id=GC_ACTOR_ID,
                tenant_id=None,
                action="artifact.gc.orphan",
                target_type="BLOB",
                target_id=file.key,
                reason="Unreferenced committed blob removed",
                safe_metadata={"bytes": file.size_bytes},
            )
        )
        return True

    def _reconcile(self, _budget: _Budget, result: GcPass) -> None:
        with self._session_factory() as session:
            drift = counter_drift(session)
            retained, retained_bytes = retained_unpublished(session)
            session.rollback()
        result.drift_tenants = len(drift)
        guarded(metrics_api.COUNTER_DRIFT.set, len(drift))
        guarded(metrics_api.RETAINED_ARTIFACTS.set, retained)
        guarded(metrics_api.RETAINED_BYTES.set, retained_bytes)
        guarded(metrics_api.RECONCILE_LAST.set, self._wall())
        if drift:
            log_event(_LOG, logging.WARNING, "storage_counter_drift", tenants=len(drift))
