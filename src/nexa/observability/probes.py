"""Database and schema readiness checks shared by the API and coordinator ops listeners."""

from collections.abc import Callable

from sqlalchemy import Engine, text

from nexa.infrastructure.persistence.schema import SCHEMA_GENERATION
from nexa.infrastructure.persistence.schema_guard import current_schema_head

DATABASE_TIMEOUT_MS = 1000


def schema_is_current(connection, head: str) -> bool:
    """The DB is at this code's Alembic head and schema generation (Connection or Session)."""
    present = connection.execute(
        text(
            "SELECT to_regclass('alembic_version') IS NOT NULL "
            "AND to_regclass('nexa_schema_metadata') IS NOT NULL"
        )
    ).scalar_one()
    if not present:
        return False
    revision = connection.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalar_one_or_none()
    generation = connection.execute(
        text("SELECT schema_generation FROM nexa_schema_metadata WHERE singleton_key = 'nexa'")
    ).scalar_one_or_none()
    return revision == head and generation == SCHEMA_GENERATION


class DatabaseProbe:
    """`database`: SELECT 1 under a statement timeout; `schema`: DB is at this code's head."""

    def __init__(
        self,
        engine: Engine,
        *,
        timeout_ms: int = DATABASE_TIMEOUT_MS,
        head: Callable[[], str] = current_schema_head,
    ) -> None:
        self._engine = engine
        self._timeout_ms = timeout_ms
        self._head = head()

    def check(self) -> dict[str, str]:
        try:
            with self._engine.connect() as connection, connection.begin():
                connection.execute(
                    text("SELECT pg_catalog.set_config('statement_timeout', :value, true)"),
                    {"value": str(self._timeout_ms)},
                )
                connection.execute(text("SELECT 1")).scalar_one()
                current = schema_is_current(connection, self._head)
        except Exception:  # noqa: BLE001 - any connection/timeout failure is not ready
            return {"database": "fail", "schema": "skip"}
        return {"database": "ok", "schema": "ok" if current else "fail"}
