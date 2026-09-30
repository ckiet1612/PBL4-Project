"""B13-R12: migration 0021 changes only the qualification of Decimal helper calls."""

import importlib.util
import re
from pathlib import Path
from types import ModuleType, SimpleNamespace

from nexa.infrastructure.persistence import schema, schema_v17, schema_v18

_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
_UNQUALIFIED_CALL = re.compile(r"(?<![\w.\"])nexa_\w+\s*\(")
_DEFINITION = re.compile(r"CREATE FUNCTION (?P<header>.+?) AS \$\$(?P<body>.*?)\$\$", re.DOTALL)


def load_migration(stem: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"nexa_rem_{stem}", _VERSIONS / f"{stem}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def b05_decimal_definitions() -> dict[str, tuple[str, str]]:
    """Return ``name -> (header, body)`` for the Decimal helpers created by migration 0001."""
    module = load_migration("20260919_0001_b05_initial")
    statements: list[str] = []
    module.op = SimpleNamespace(execute=statements.append)
    module._create_decimal_functions()
    definitions = {}
    for statement in statements:
        for match in _DEFINITION.finditer(statement):
            header = match["header"].strip()
            definitions[header.split("(", 1)[0]] = (header, match["body"])
    return definitions


def test_unqualified_rendering_reproduces_b05_bodies_byte_for_byte() -> None:
    fix = load_migration("20260928_0021_rem_decimal_search_path")
    old = b05_decimal_definitions()
    for signature in schema_v18.QUALIFIED_DECIMAL_FUNCTIONS:
        name = signature.split("(", 1)[0]
        header, _ = fix._DECIMAL_DEFINITIONS[signature]
        assert re.sub(r"\s+", " ", header) == re.sub(r"\s+", " ", old[name][0]), signature
        assert fix.render_decimal_body(signature, "") == old[name][1], signature


def test_qualified_bodies_call_no_helper_through_the_search_path() -> None:
    fix = load_migration("20260928_0021_rem_decimal_search_path")
    for signature in schema_v18.QUALIFIED_DECIMAL_FUNCTIONS:
        body = fix.render_decimal_body(signature, '"tenant schema".')
        assert _UNQUALIFIED_CALL.search(body) is None, signature
        assert '"tenant schema".nexa_decimal_is_valid(' in body, signature


def test_every_b05_decimal_helper_is_classified() -> None:
    names = set(b05_decimal_definitions())
    declared = {
        signature.split("(", 1)[0]
        for signature in (
            *schema_v18.QUALIFIED_DECIMAL_FUNCTIONS,
            *schema_v18.CATALOG_ONLY_FUNCTIONS,
        )
    }
    assert names <= declared
    assert "nexa_decimal_is_valid" in names


def test_schema_v18_keeps_the_v17_tables() -> None:
    assert schema_v18.SCHEMA_GENERATION == schema_v17.SCHEMA_GENERATION
    assert set(schema_v18.metadata.tables) == set(schema_v17.metadata.tables)
    # Migration 0022 (B14-OBS-01) adds only the artifact store identity after v18.
    assert set(schema.metadata.tables) == set(schema_v18.metadata.tables) | {
        "artifact_store_identity"
    }
    classified = (
        schema_v18.QUALIFIED_DECIMAL_FUNCTIONS
        + schema_v18.CATALOG_ONLY_FUNCTIONS
        + schema_v18.SEARCH_PATH_PINNED_FUNCTIONS
    )
    assert len(classified) == len(set(classified))
