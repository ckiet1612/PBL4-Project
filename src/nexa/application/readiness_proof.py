"""Readiness proof for reopening NORMAL from ADMISSION_OFF (B19-R02).

state-machines.md: ADMISSION_OFF -> NORMAL needs "DB/schema/storage/capability
healthy; worker reconciled and enabled/READY". The storage probe (fsync, below the
critical watermark) runs before the mode transaction; DB, schema and every worker
are re-read inside the transaction that commits the mode, with the worker rows held
FOR SHARE so a heartbeat cannot change them between the check and the commit. No
Docker call is made. QUARANTINED allocations are not a condition: they stay charged
to capacity, so dispatch remains safe; that condition belongs to enabling a worker.

Freeze and restore proofs stay fail-closed until B21.
"""

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexa.application.admin_workers import worker_readiness_failure
from nexa.config import Settings
from nexa.infrastructure.artifacts.store import ArtifactError, FilesystemArtifactStore
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.locking import clock_timestamp
from nexa.infrastructure.persistence.schema_guard import current_schema_head
from nexa.observability.logging import log_event
from nexa.observability.probes import schema_is_current

_LOG = logging.getLogger(__name__)


class ReadinessRecoveryProofProvider:
    def __init__(
        self,
        settings: Settings,
        store: FilesystemArtifactStore,
        *,
        head: Callable[[], str] = current_schema_head,
    ) -> None:
        self._settings = settings
        self._store = store
        self._head = head()

    def storage_ready(self) -> bool:
        """Durable write probe below the critical watermark; call outside any transaction."""
        try:
            self._store.check_readiness(
                critical_watermark_percent=self._settings.storage_critical_watermark_percent
            )
        except ArtifactError:
            log_event(_LOG, logging.WARNING, "mode_reopen_refused", failed="storage")
            return False
        return True

    def freeze_ready(self, session: Session) -> bool:
        return False  # B21

    def restore_verified(self, session: Session) -> bool:
        return False  # B21

    def readiness_verified(self, session: Session) -> bool:
        failed = self._failure(session)
        if failed is not None:
            # The condition goes to the log only; the 409 keeps its fixed message.
            log_event(_LOG, logging.WARNING, "mode_reopen_refused", failed=failed)
        return failed is None

    def _failure(self, session: Session) -> str | None:
        if not schema_is_current(session, self._head):
            return "schema"
        workers = (
            session.execute(
                select(s.workers).order_by(s.workers.c.worker_id).with_for_update(read=True)
            )
            .mappings()
            .all()
        )
        if not workers:
            return "no_worker"
        now = clock_timestamp(session)
        for worker in workers:
            if worker["admin_state"] != "ENABLED":
                return "worker_not_enabled"
            if worker["health"] != "READY":
                return "worker_not_ready"
            failure = worker_readiness_failure(session, worker, now)
            if failure is not None:
                return _FAILURE_CODES[failure]
        return None


_FAILURE_CODES = {
    "The worker has no fresh heartbeat": "worker_heartbeat_stale",
    "The latest worker heartbeat did not pass the READY checks": "worker_heartbeat_not_ready",
    "The current worker incarnation is not reconciled": "worker_not_reconciled",
    "The worker inventory does not pass capability checks": "worker_inventory",
}
