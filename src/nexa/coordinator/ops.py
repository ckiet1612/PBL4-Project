"""Coordinator ops listener: /livez, /readyz (database, schema, role) and /metrics (B19-R03)."""

from sqlalchemy import Engine

from nexa.coordinator.runtime import CoordinatorStatus
from nexa.observability import metrics_coordinator
from nexa.observability.metrics import exposition, internal_error
from nexa.observability.ops_server import CachedReadiness, OpsServer, ReadinessReport
from nexa.observability.probes import DatabaseProbe


class CoordinatorOps:
    """`role` is reported but never fails readiness: a standby is a healthy coordinator."""

    def __init__(self, bind: tuple[str, int], engine: Engine, status: CoordinatorStatus) -> None:
        self._status = status
        self._database = DatabaseProbe(engine)
        self._server = OpsServer(
            bind,
            live=status.live,
            ready=CachedReadiness(self._probe),
            metrics=lambda: exposition(metrics_coordinator.REGISTRY),
            on_internal_error=internal_error,
            on_ready_failure=lambda: metrics_coordinator.set_ready({}),
        )

    @property
    def port(self) -> int:
        return self._server.port

    def _probe(self) -> ReadinessReport:
        checks = self._database.check()
        metrics_coordinator.set_ready(checks)
        return ReadinessReport(checks, {"role": "leader" if self._status.leader else "standby"})

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()
