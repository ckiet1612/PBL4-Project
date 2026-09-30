"""B13-R12: Nexa SQL functions must not depend on the caller's search_path (migration 0021).

PostgreSQL 17 runs ANALYZE/autoanalyze/REINDEX with a restricted search_path and pg_restore with an
empty one. Before 0021 the Decimal helpers called each other unqualified, so ANALYZE of
``fairness_ledgers`` and a plain pg_dump/pg_restore failed.
"""

import os
import re
import shlex
import shutil
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import MAX_EMAX, MIN_ETINY, Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from nexa.infrastructure.persistence import schema_v18
from nexa.infrastructure.persistence.schema import (
    fairness_ledgers,
    fairness_state,
    rate_buckets,
)
from nexa.infrastructure.persistence.values import decimal_to_db
from tests.integration._factories import seed_authority, seed_job, seed_tenant_graph, seed_worker
from tests.integration.test_fairness_persistence import _insert_policy
from tests.integration.test_migrations import _config
from tests.persistence.test_rem_b13_r12_decimal_bodies import load_migration

pytestmark = pytest.mark.postgres

_BEFORE = "20260928_0020"
_FIX = "20260928_0021"
_OLD_SCHEMA = "nexa_rem_b05_decimal"
_UNQUALIFIED_CALL = re.compile(r"(?<![\w.\"])nexa_\w+\s*\(")


def _seed(connection, label: str) -> None:
    graph = seed_tenant_graph(connection, label=label)
    _insert_policy(connection, graph["tenant_id"], version=1, weight=Decimal("0.5"))
    job = seed_job(connection, graph)
    worker = seed_worker(connection, label=label)
    seed_authority(connection, graph, job, worker)
    connection.execute(
        fairness_ledgers.insert(),
        {
            "tenant_id": graph["tenant_id"],
            "virtual_score": Decimal("0.3333333333333333333333"),
            "accounted_through": datetime(2026, 1, 1, tzinfo=UTC),
            "had_eligible_demand": True,
            "version": 1,
        },
    )
    connection.execute(
        rate_buckets.insert(),
        {
            "scope_type": "TENANT",
            "scope_id": f"rate-{label}",
            "tokens": Decimal("1E-20000"),
            "capacity": Decimal("2E-20000"),
            "refill_rate": Decimal("1.5"),
            "last_refill_at": datetime.now(UTC),
            "version": 1,
        },
    )


def _seed_floor(connection) -> None:
    connection.execute(
        fairness_state.insert(),
        {"singleton_key": "local", "virtual_floor": Decimal("0.125"), "version": 1},
    )


def _update_under_empty_search_path(connection) -> None:
    schema = _schema(connection)
    connection.execute(text("SET LOCAL search_path = ''"))
    connection.execute(
        text(f"UPDATE {schema}.fairness_state SET virtual_floor = '0:5:-1', version = version + 1")
    )


def _upgraded_engine(url: str, revision: str):
    command.upgrade(_config(url), revision)
    return create_engine(url)


def test_before_0021_analyze_and_empty_search_path_checks_fail(
    clean_postgres_database: str,
) -> None:
    engine = _upgraded_engine(clean_postgres_database, _BEFORE)
    try:
        with engine.begin() as connection:
            _seed(connection, "r12-before")
            _seed_floor(connection)
        with pytest.raises(DBAPIError, match="nexa_decimal_is_valid"), engine.begin() as c:
            c.execute(text("ANALYZE fairness_ledgers"))
        with pytest.raises(DBAPIError, match="nexa_decimal_is_valid"), engine.begin() as c:
            _update_under_empty_search_path(c)
    finally:
        engine.dispose()


def _schema(connection) -> str:
    return connection.execute(text("SELECT current_schema()")).scalar_one()


def test_analyze_reindex_and_empty_search_path_checks_succeed(clean_postgres_database: str) -> None:
    engine = _upgraded_engine(clean_postgres_database, "head")
    try:
        with engine.begin() as connection:
            _seed(connection, "r12-after")
            _seed_floor(connection)
        with engine.begin() as connection:
            connection.execute(text("ANALYZE fairness_ledgers"))
            connection.execute(text("ANALYZE"))
            connection.execute(text("REINDEX INDEX ix_fairness_ledgers_score"))
        with engine.begin() as connection:
            schema = _schema(connection)
            _update_under_empty_search_path(connection)
            connection.execute(
                text(
                    f"UPDATE {schema}.rate_buckets SET tokens = '0:1:-20000', version = version + 1"
                )
            )
            with (
                pytest.raises(DBAPIError, match="tokens_within_capacity"),
                connection.begin_nested(),
            ):
                connection.execute(
                    text(
                        f"UPDATE {schema}.rate_buckets SET tokens = '0:3:-20000', "
                        "version = version + 1"
                    )
                )
            # Trigger functions (including deferred constraint triggers, which run at commit under
            # the same empty search_path) resolve Nexa objects through their pinned search_path.
            connection.execute(
                text(f"UPDATE {schema}.tenants SET enabled = NOT enabled, version = version + 1")
            )
    finally:
        engine.dispose()


def _function_rows(connection) -> dict[str, tuple[str, list[str] | None, str]]:
    rows = connection.execute(
        text(
            # oidvectortypes spells the arguments as the schema_v18 declarations do ("a, b").
            "SELECT p.proname || '(' || pg_catalog.oidvectortypes(p.proargtypes) || ')' "
            "AS signature, n.nspname, p.proconfig, p.prosrc "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE p.proname LIKE 'nexa\\_%' AND n.nspname = current_schema()"
        )
    ).all()
    return {row.signature: (row.nspname, row.proconfig, row.prosrc) for row in rows}


def test_every_nexa_function_matches_the_schema_v18_declaration(
    clean_postgres_database: str,
) -> None:
    engine = _upgraded_engine(clean_postgres_database, "head")
    try:
        with engine.connect() as connection:
            functions = _function_rows(connection)
    finally:
        engine.dispose()
    declared = (
        set(schema_v18.QUALIFIED_DECIMAL_FUNCTIONS)
        | set(schema_v18.CATALOG_ONLY_FUNCTIONS)
        | set(schema_v18.SEARCH_PATH_PINNED_FUNCTIONS)
    )
    assert set(functions) == declared
    for signature in schema_v18.SEARCH_PATH_PINNED_FUNCTIONS:
        schema, config, _ = functions[signature]
        assert config == [f"search_path=pg_catalog, {schema}, pg_temp"], signature
    for signature in schema_v18.QUALIFIED_DECIMAL_FUNCTIONS:
        schema, config, body = functions[signature]
        assert config is None, signature
        assert f'"{schema}".nexa_decimal_' in body, signature
        assert _UNQUALIFIED_CALL.search(body) is None, signature
    for signature in schema_v18.CATALOG_ONLY_FUNCTIONS:
        _, config, body = functions[signature]
        assert config is None, signature
        assert "nexa_" not in body and not re.search(r"\bFROM\b", body, re.I), signature


def _helper_plans(connection) -> list[str]:
    # A column argument, unlike a constant, cannot be folded away before the plan prints.
    connection.execute(text("CREATE TEMPORARY TABLE IF NOT EXISTS r12_plan (v text)"))
    plans = []
    for signature in schema_v18.QUALIFIED_DECIMAL_FUNCTIONS:
        name, arguments = signature.split("(")
        call = f"{name}({', '.join('v' for _ in arguments.rstrip(')').split(','))})"
        plan = connection.execute(
            text(f"EXPLAIN (VERBOSE, COSTS OFF) SELECT {call} FROM r12_plan")
        ).scalars()
        plans.append("\n".join(plan))
    return plans


def test_sql_decimal_helpers_keep_their_planner_shape(clean_postgres_database: str) -> None:
    """0021 qualifies these helpers instead of pinning ``SET search_path`` on them.

    A ``SET`` clause would forbid SQL-function inlining and add a GUC save/restore per call.
    The STRICT SQL helpers are not inlined before 0021 either (an ``AND`` body is not provably
    strict), so the contract is an unchanged plan, not a newly inlined one.
    """
    engine = _upgraded_engine(clean_postgres_database, _BEFORE)
    try:
        with engine.connect() as connection:
            before = _helper_plans(connection)
    finally:
        engine.dispose()
    engine = _upgraded_engine(clean_postgres_database, "head")
    try:
        with engine.connect() as connection:
            after = _helper_plans(connection)
    finally:
        engine.dispose()
    assert after == before
    assert all("Output: nexa_decimal_" in plan for plan in after)


def test_upgrade_downgrade_upgrade_restores_b05_functions_exactly(
    clean_postgres_database: str,
) -> None:
    engine = _upgraded_engine(clean_postgres_database, _BEFORE)
    config = _config(clean_postgres_database)
    try:
        with engine.begin() as connection:
            _seed(connection, "r12-roundtrip")
        with engine.connect() as connection:
            before = _function_rows(connection)
        command.upgrade(config, _FIX)
        with engine.connect() as connection:
            upgraded = _function_rows(connection)
        assert upgraded != before
        command.downgrade(config, _BEFORE)
        with engine.connect() as connection:
            assert _function_rows(connection) == before
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert _function_rows(connection) == upgraded
            connection.execute(text("ANALYZE fairness_ledgers"))
    finally:
        engine.dispose()


# --- semantics: B05 bodies (migration 0001) versus the 0021 bodies --------------------------


def _b05_decimal_statements() -> list[str]:
    module = load_migration("20260919_0001_b05_initial")
    captured: list[str] = []
    module.op = SimpleNamespace(execute=captured.append)
    module._create_decimal_functions()
    return captured


def _install_b05_copy(connection) -> None:
    connection.execute(text(f"DROP SCHEMA IF EXISTS {_OLD_SCHEMA} CASCADE"))
    connection.execute(text(f"CREATE SCHEMA {_OLD_SCHEMA}"))
    connection.execute(text(f"SET LOCAL search_path = {_OLD_SCHEMA}"))
    for statement in _b05_decimal_statements():
        connection.exec_driver_sql(statement)


_DIGITS = st.one_of(
    st.just("0"),
    st.from_regex(r"[1-9][0-9]{0,40}", fullmatch=True),
    st.from_regex(r"0[0-9]{1,3}", fullmatch=True),
)
_EXPONENT = st.one_of(
    st.integers(min_value=MIN_ETINY - 3, max_value=MAX_EMAX + 3),
    st.integers(min_value=-60, max_value=60),
    st.sampled_from([MIN_ETINY, MAX_EMAX, MAX_EMAX - 1, 0, -1]),
)
_ENCODED = st.one_of(
    st.builds(lambda s, d, e: f"{s}:{d}:{e}", st.sampled_from("01"), _DIGITS, _EXPONENT),
    st.decimals(allow_nan=False, allow_infinity=False).map(lambda value: decimal_to_db(abs(value))),
    st.text(alphabet="01:-9xe ", max_size=12),
    st.sampled_from(["0:0:0", "1:0:0", "0:1:-0", "0:00:0", "0:1:" + "9" * 30, "2:1:0", ""]),
)
_UNARY = (
    "nexa_decimal_is_valid",
    "nexa_decimal_is_nonnegative",
    "nexa_decimal_is_positive",
    "nexa_decimal_zero_rank",
    "nexa_decimal_adjusted_exponent",
    "nexa_decimal_normalized_significand",
)


def _outcome(connection, expression: str, parameters: dict) -> tuple[str, object]:
    try:
        with connection.begin_nested():
            return ("value", connection.execute(text(f"SELECT {expression}"), parameters).one()[0])
    except DBAPIError as error:
        return ("error", getattr(error.orig, "sqlstate", None))


@pytest.fixture
def decimal_pair_connection(clean_postgres_database: str):
    engine = _upgraded_engine(clean_postgres_database, "head")
    with engine.connect() as connection:
        transaction = connection.begin()
        _install_b05_copy(connection)
        connection.execute(text("SET LOCAL search_path = ''"))
        yield connection, _schema_of_head(connection)
        transaction.rollback()
    engine.dispose()


def _schema_of_head(connection) -> str:
    return connection.execute(
        text(
            "SELECT n.nspname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE p.proname = 'nexa_decimal_compare' AND n.nspname <> :old"
        ),
        {"old": _OLD_SCHEMA},
    ).scalar_one()


@settings(
    max_examples=400,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(left=_ENCODED, right=_ENCODED)
def test_0021_decimal_helpers_match_b05_bodies(decimal_pair_connection, left, right) -> None:
    connection, schema = decimal_pair_connection
    parameters = {"left": left, "right": right}
    for name in _UNARY:
        # The B05 copy runs with its own schema on the search_path (as B05 assumed); the 0021
        # bodies run with an empty search_path and must still agree.
        connection.execute(text(f"SET LOCAL search_path = {_OLD_SCHEMA}"))
        old = _outcome(connection, f"{_OLD_SCHEMA}.{name}(:left)", parameters)
        connection.execute(text("SET LOCAL search_path = ''"))
        new = _outcome(connection, f'"{schema}".{name}(:left)', parameters)
        assert new == old, name
    connection.execute(text(f"SET LOCAL search_path = {_OLD_SCHEMA}"))
    old = _outcome(connection, f"{_OLD_SCHEMA}.nexa_decimal_compare(:left, :right)", parameters)
    connection.execute(text("SET LOCAL search_path = ''"))
    new = _outcome(connection, f'"{schema}".nexa_decimal_compare(:left, :right)', parameters)
    assert new == old


# --- pg_dump / pg_restore without any workaround --------------------------------------------


def _pg_tool(name: str) -> list[str] | None:
    prefix = os.environ.get("NEXA_TEST_PG_CLIENT_PREFIX", "").strip()
    if prefix:
        return [*shlex.split(prefix), name]
    binary = shutil.which(name)
    return [binary] if binary else None


def _tool_arguments(url: str) -> tuple[list[str], dict[str, str]]:
    parsed = make_url(url)
    environment = dict(os.environ)
    if os.environ.get("NEXA_TEST_PG_CLIENT_PREFIX", "").strip():
        # The prefix runs the client inside the PostgreSQL container over its local socket.
        return ["-U", parsed.username or "postgres", "-d", parsed.database or ""], environment
    environment["PGPASSWORD"] = parsed.password or ""
    return [
        "-h",
        parsed.host or "127.0.0.1",
        "-p",
        str(parsed.port or 5432),
        "-U",
        parsed.username or "postgres",
        "-d",
        parsed.database or "",
    ], environment


@contextmanager
def _restore_target(base_url: str):
    name = f"nexa_b05_test_restore_{uuid4().hex}"
    admin = create_engine(make_url(base_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
        yield make_url(base_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.dispose()


def _catalog_snapshot(connection) -> dict[str, object]:
    tables = connection.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY 1")
    ).scalars()
    content = {}
    for table in tables:
        content[table] = connection.execute(
            text(
                f"SELECT count(*), md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), '')) "
                f'FROM "{table}" t'
            )
        ).one()
    constraints = connection.execute(
        text(
            "SELECT conrelid::regclass::text, conname, contype, convalidated, "
            "pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE connamespace = current_schema()::regnamespace ORDER BY 1, 2"
        )
    ).all()
    indexes = connection.execute(
        text(
            "SELECT c.relname, i.indisvalid, i.indisready, pg_get_indexdef(i.indexrelid) "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relnamespace = current_schema()::regnamespace ORDER BY 1"
        )
    ).all()
    triggers = connection.execute(
        text(
            "SELECT tgrelid::regclass::text, tgname, tgenabled, pg_get_triggerdef(oid) "
            "FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1, 2"
        )
    ).all()
    functions = _function_rows(connection)
    return {
        "content": content,
        "constraints": constraints,
        "indexes": indexes,
        "triggers": triggers,
        "functions": functions,
    }


def _dump_and_restore(source_url: str, target_url: str) -> subprocess.CompletedProcess:
    dump, restore = _pg_tool("pg_dump"), _pg_tool("pg_restore")
    if dump is None or restore is None:
        pytest.skip("pg_dump/pg_restore unavailable; set NEXA_TEST_PG_CLIENT_PREFIX")
    source_args, source_env = _tool_arguments(source_url)
    archive = subprocess.run(
        [*dump, "--format=custom", "--no-owner", *source_args],
        env=source_env,
        capture_output=True,
        check=True,
    ).stdout
    target_args, target_env = _tool_arguments(target_url)
    return subprocess.run(
        [*restore, "--exit-on-error", "--no-owner", *target_args],
        input=archive,
        env=target_env,
        capture_output=True,
        check=False,
    )


def _seed_for_restore(url: str, revision: str) -> None:
    engine = _upgraded_engine(url, revision)
    try:
        with engine.begin() as connection:
            for index in range(3):
                _seed(connection, f"r12-restore-{index}")
            _seed_floor(connection)
    finally:
        engine.dispose()


def test_before_0021_plain_restore_fails(clean_postgres_database: str) -> None:
    _seed_for_restore(clean_postgres_database, _BEFORE)
    with _restore_target(clean_postgres_database) as target:
        restored = _dump_and_restore(clean_postgres_database, target)
    assert restored.returncode != 0
    assert b"nexa_decimal_is_valid" in restored.stderr


_CAST = re.compile(r"::(?:character varying|text)(?:\[\])?")


def _canonical_definition(definition: str) -> str:
    """Drop the varchar/text casts and grouping that PostgreSQL re-deparses on restore.

    A CHECK/partial-index ``col = ANY ((ARRAY['A'::varchar])::text[])`` is re-parsed from the dump
    as ``col = ANY (ARRAY[('A'::varchar)::text])``: the same predicate, different text.
    """
    return re.sub(r"[()\s]", "", _CAST.sub("", definition))


def _canonical(snapshot: dict[str, object]) -> dict[str, object]:
    return {
        **snapshot,
        "constraints": [
            (*row[:-1], _canonical_definition(row[-1])) for row in snapshot["constraints"]
        ],
        "indexes": [(*row[:-1], _canonical_definition(row[-1])) for row in snapshot["indexes"]],
    }


def _restored_snapshot(source_url: str, target_url: str) -> dict[str, object]:
    restored = _dump_and_restore(source_url, target_url)
    assert restored.returncode == 0, restored.stderr.decode(errors="replace")[-2000:]
    assert restored.stderr == b""
    engine = create_engine(target_url)
    try:
        with engine.connect() as connection:
            snapshot = _catalog_snapshot(connection)
            connection.execute(text("ANALYZE"))
    finally:
        engine.dispose()
    return snapshot


def test_plain_dump_restore_preserves_every_object_and_row(clean_postgres_database: str) -> None:
    _seed_for_restore(clean_postgres_database, "head")
    source = create_engine(clean_postgres_database)
    try:
        with source.connect() as connection:
            expected = _catalog_snapshot(connection)
    finally:
        source.dispose()
    with _restore_target(clean_postgres_database) as first:
        restored = _restored_snapshot(clean_postgres_database, first)
        with _restore_target(clean_postgres_database) as second:
            restored_again = _restored_snapshot(first, second)
    # Every row, function and trigger is byte-identical; constraint/index text only differs by
    # PostgreSQL's cast deparse, and a second round trip is an exact fixed point.
    assert {key: restored[key] for key in ("content", "triggers", "functions")} == {
        key: expected[key] for key in ("content", "triggers", "functions")
    }
    assert _canonical(restored) == _canonical(expected)
    assert restored_again == restored


def test_restored_constraints_still_reject_invalid_values(clean_postgres_database: str) -> None:
    _seed_for_restore(clean_postgres_database, "head")
    with _restore_target(clean_postgres_database) as target:
        _restored_snapshot(clean_postgres_database, target)
        engine = create_engine(target)
        try:
            with (
                engine.connect() as connection,
                pytest.raises(DBAPIError, match="ck_allocations_state"),
            ):
                connection.execute(text("UPDATE allocations SET state = 'BOGUS'"))
        finally:
            engine.dispose()
