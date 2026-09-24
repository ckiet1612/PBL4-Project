"""Run with NEXA_DATABASE_URL and `python -m nexa.coordinator.main`."""

import logging
import os
import signal
from threading import Event

from nexa.coordinator.runtime import run
from nexa.coordinator.service import CoordinatorService
from nexa.infrastructure.persistence.database import create_database_engine, create_session_factory
from nexa.infrastructure.persistence.schema_guard import require_current_schema


def main() -> None:
    database_url = os.environ.get("NEXA_DATABASE_URL")
    if not database_url:
        raise SystemExit("NEXA_DATABASE_URL is required")
    logging.basicConfig(level=logging.INFO)
    engine = create_database_engine(
        database_url, connect_args={"connect_timeout": 3}, pool_timeout=3
    )
    stop = Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: stop.set())
    try:
        require_current_schema(engine)
        run(CoordinatorService(create_session_factory(engine)), stop)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
