import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from nexa.infrastructure.persistence.schema_guard import (
    SchemaCompatibility,
    SchemaCompatibilityError,
    inspect_schema_compatibility,
    require_current_schema,
)

pytestmark = pytest.mark.postgres


def _upgrade(url: str) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")


def test_missing_schema_is_reported_without_auto_migration(clean_postgres_database: str) -> None:
    engine = create_engine(clean_postgres_database)
    try:
        assert inspect_schema_compatibility(engine) is SchemaCompatibility.MISSING
        assert "jobs" not in inspect(engine).get_table_names()
        with pytest.raises(SchemaCompatibilityError, match="missing"):
            require_current_schema(engine)
    finally:
        engine.dispose()


def test_current_old_new_and_unknown_schema_states_are_distinct(
    clean_postgres_database: str,
) -> None:
    _upgrade(clean_postgres_database)
    engine = create_engine(clean_postgres_database)
    try:
        assert inspect_schema_compatibility(engine) is SchemaCompatibility.CURRENT
        assert (
            inspect_schema_compatibility(engine, supported_generation=2) is SchemaCompatibility.OLD
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE nexa_schema_metadata SET schema_generation = 2 "
                    "WHERE singleton_key = 'nexa'"
                )
            )
        assert inspect_schema_compatibility(engine) is SchemaCompatibility.NEW
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE nexa_schema_metadata SET schema_generation = 1 "
                    "WHERE singleton_key = 'nexa'"
                )
            )
            connection.execute(text("UPDATE alembic_version SET version_num = 'unknown_revision'"))
        assert inspect_schema_compatibility(engine) is SchemaCompatibility.UNKNOWN
    finally:
        engine.dispose()
