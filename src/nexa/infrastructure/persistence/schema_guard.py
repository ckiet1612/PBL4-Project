from enum import StrEnum
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text

from nexa.infrastructure.persistence.schema import SCHEMA_GENERATION


class SchemaCompatibility(StrEnum):
    MISSING = "missing"
    OLD = "old"
    CURRENT = "current"
    NEW = "new"
    UNKNOWN = "unknown"


class SchemaCompatibilityError(RuntimeError):
    """Raised when a database cannot safely serve the current application."""


def inspect_schema_compatibility(
    engine: Engine,
    *,
    alembic_config_path: str | Path = "alembic.ini",
    supported_generation: int = SCHEMA_GENERATION,
) -> SchemaCompatibility:
    if supported_generation < 1:
        raise ValueError("supported_generation must be positive")
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        if not {"alembic_version", "nexa_schema_metadata"} <= tables:
            return SchemaCompatibility.MISSING
        metadata_row = connection.execute(
            text("SELECT schema_generation FROM nexa_schema_metadata WHERE singleton_key = 'nexa'")
        ).one_or_none()
        revision_row = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).one_or_none()
    if metadata_row is None or revision_row is None:
        return SchemaCompatibility.MISSING

    generation = int(metadata_row.schema_generation)
    if generation < supported_generation:
        return SchemaCompatibility.OLD
    if generation > supported_generation:
        return SchemaCompatibility.NEW

    config = Config(str(alembic_config_path))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    if len(heads) != 1:
        return SchemaCompatibility.UNKNOWN
    revision = str(revision_row.version_num)
    if revision == heads[0]:
        return SchemaCompatibility.CURRENT
    try:
        known_revision = scripts.get_revision(revision)
    except Exception:
        known_revision = None
    if known_revision is not None:
        return SchemaCompatibility.OLD
    return SchemaCompatibility.UNKNOWN


def require_current_schema(engine: Engine) -> None:
    compatibility = inspect_schema_compatibility(engine)
    if compatibility is not SchemaCompatibility.CURRENT:
        raise SchemaCompatibilityError(
            f"Database schema is {compatibility.value}; "
            "explicit migration or operator action is required"
        )
