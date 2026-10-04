"""Worker ops listener: /livez, /readyz (reconciled, heartbeat, docker) and /metrics (B19-R03).

Readiness mirrors what the worker can prove locally: reconciliation of its current
incarnation finished with nothing pending, the API accepted a heartbeat within 30 s,
and the Docker daemon answers. It never contacts the API itself.
"""

from typing import Protocol

from nexa.observability import metrics_worker
from nexa.observability.metrics import exposition, guarded, internal_error
from nexa.observability.ops_server import CachedReadiness, OpsServer, ReadinessReport

DOCKER_PING_TIMEOUT_SECONDS = 3.0


class _Agent(Protocol):
    def live(self) -> bool: ...
    def readiness_checks(self) -> dict[str, str]: ...
    def reported_ready(self) -> bool: ...


class _Docker(Protocol):
    def ping(self, *, timeout_seconds: float = ...) -> bool: ...


class WorkerOps:
    def __init__(self, bind: tuple[str, int], agent: _Agent, docker: _Docker) -> None:
        self._agent = agent
        self._docker = docker
        guarded(metrics_worker.WORKER_READY.set_function, self._reported_ready)
        self._server = OpsServer(
            bind,
            live=agent.live,
            ready=CachedReadiness(self._probe),
            metrics=lambda: exposition(metrics_worker.REGISTRY),
            on_internal_error=internal_error,
            on_ready_failure=lambda: metrics_worker.set_ready({}),
        )

    @property
    def port(self) -> int:
        return self._server.port

    def _reported_ready(self) -> float:
        return 1.0 if self._agent.reported_ready() else 0.0

    def _probe(self) -> ReadinessReport:
        checks = dict(self._agent.readiness_checks())
        checks["docker"] = (
            "ok" if self._docker.ping(timeout_seconds=DOCKER_PING_TIMEOUT_SECONDS) else "fail"
        )
        metrics_worker.set_ready(checks)
        return ReadinessReport(checks)

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()
