"""Run with NEXA_DATABASE_URL and `python -m nexa.coordinator.main`."""

import os
import signal
from threading import Event

from nexa.config import (
    ConfigError,
    coordinator_artifact_quota_bytes,
    log_settings,
    ops_bind_setting,
)
from nexa.coordinator.ops import CoordinatorOps
from nexa.coordinator.runtime import CoordinatorStatus, run
from nexa.coordinator.service import CoordinatorService
from nexa.infrastructure.persistence.database import create_database_engine, create_session_factory
from nexa.infrastructure.persistence.schema_guard import require_current_schema
from nexa.observability.logging import configure_logging


def main() -> None:
    database_url = os.environ.get("NEXA_DATABASE_URL")
    if not database_url:
        raise SystemExit("NEXA_DATABASE_URL is required")
    try:
        level, fmt = log_settings(os.environ)
        ops_bind = ops_bind_setting(os.environ)
        artifact_quota_bytes = coordinator_artifact_quota_bytes(os.environ)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None
    configure_logging("coordinator", level=level, fmt=fmt)
    engine = create_database_engine(
        database_url, connect_args={"connect_timeout": 3}, pool_timeout=3
    )
    stop = Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: stop.set())
    # The readiness probe gets its own one-connection engine so a busy tick pool
    # never makes the coordinator look unready, and the probe never delays a tick.
    ops_engine = None
    ops = None
    try:
        require_current_schema(engine)
        status = CoordinatorStatus()
        if ops_bind is not None:
            ops_engine = create_database_engine(
                database_url,
                pool_size=1,
                max_overflow=0,
                pool_timeout=1,
                connect_args={"connect_timeout": 2},
            )
            ops = CoordinatorOps(ops_bind, ops_engine, status)
            ops.start()  # bind failure stops the process (D3)
        service = CoordinatorService(
            create_session_factory(engine), artifact_quota_bytes=artifact_quota_bytes
        )
        run(service, stop, status)
    finally:
        if ops is not None:
            ops.stop()
        if ops_engine is not None:
            ops_engine.dispose()
        engine.dispose()


if __name__ == "__main__":
    main()
