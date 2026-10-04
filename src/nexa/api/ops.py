"""API ops listener wiring: /livez, /readyz and /metrics on NEXA_OPS_BIND (B19-R03)."""

import logging
import time
from collections.abc import Callable
from contextlib import suppress

from nexa.api.collectors import ApiStateCollector
from nexa.config import Settings
from nexa.infrastructure.artifacts.store import FilesystemArtifactStore
from nexa.infrastructure.persistence.database import create_database_engine
from nexa.observability import metrics_api
from nexa.observability.logging import log_event
from nexa.observability.metrics import exposition, internal_error
from nexa.observability.ops_server import CachedReadiness, OpsServer, ReadinessReport
from nexa.observability.probes import DatabaseProbe

_LOG = logging.getLogger(__name__)
LIVENESS_STALE_SECONDS = 30.0


class ApiOps:
    """Owns the small ops engine, the state collector and the listener."""

    def __init__(
        self,
        settings: Settings,
        store: FilesystemArtifactStore,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if settings.ops_bind is None:
            raise ValueError("ops listener is disabled")
        self._settings = settings
        self._store = store
        self._clock = clock
        self._beat = clock()
        # Two connections at most: one readiness probe and one collector refresh, both
        # single-flight; REST never shares this pool.
        self._engine = create_database_engine(
            settings.database_url,
            pool_size=2,
            max_overflow=0,
            pool_timeout=1,
            connect_args={"connect_timeout": 2},
        )
        self._database = DatabaseProbe(self._engine)
        self._collector = ApiStateCollector(
            self._engine,
            disk_usage=store.disk_usage,
            cache_seconds=settings.metrics_cache_seconds,
            statement_timeout_ms=settings.metrics_statement_timeout_ms,
        )
        self._ready = CachedReadiness(self._probe)
        self._server = OpsServer(
            settings.ops_bind,
            live=self.live,
            ready=self._ready,
            metrics=lambda: exposition(metrics_api.REGISTRY),
            on_internal_error=internal_error,
            on_ready_failure=lambda: metrics_api.set_ready({}),
        )

    @property
    def port(self) -> int:
        return self._server.port

    def beat(self) -> None:
        """Called by an event-loop task every second; a stale beat means not live."""
        self._beat = self._clock()

    def live(self) -> bool:
        return self._clock() - self._beat < LIVENESS_STALE_SECONDS

    def _probe(self) -> ReadinessReport:
        checks = self._database.check()
        try:
            self._store.check_readiness(
                critical_watermark_percent=self._settings.storage_critical_watermark_percent
            )
            checks["storage"] = "ok"
        except Exception as exc:  # noqa: BLE001 - every storage failure is not ready
            checks["storage"] = "fail"
            log_event(_LOG, logging.WARNING, "readiness_storage_failed", reason=type(exc).__name__)
        metrics_api.set_ready(checks)
        return ReadinessReport(checks)

    def start(self) -> None:
        metrics_api.configure_watermarks(
            self._settings.storage_high_watermark_percent,
            self._settings.storage_critical_watermark_percent,
        )
        metrics_api.REGISTRY.register(self._collector)
        try:
            self._server.start()
        except BaseException:
            metrics_api.REGISTRY.unregister(self._collector)
            self._engine.dispose()
            raise

    def stop(self) -> None:
        self._server.stop()
        with suppress(KeyError):
            metrics_api.REGISTRY.unregister(self._collector)
        self._engine.dispose()
