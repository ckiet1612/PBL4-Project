"""B16 migration 0020: sweep expansion columns, immutability, inference extent, retention index."""

import pytest
from alembic import command
from sqlalchemy import CheckConstraint, MetaData, create_engine, insert, inspect, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.schema import AddConstraint, CreateIndex

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence import schema_v17
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.integration._factories import seed_authority, seed_job, seed_tenant_graph, seed_worker
from tests.integration.test_migrations import _config

pytestmark = pytest.mark.postgres

_HASH = "sha256:" + "1" * 64
_TRIGGERS = {"trg_sweep_parents_guard", "trg_sweep_children_immutable"}


def _parent_values(graph, **changes):
    return {
        "sweep_id": new_uuid7(),
        "tenant_id": graph["tenant_id"],
        "submitter_user_id": graph["user_id"],
        "base_spec_checksum": _HASH,
        "idempotency_context": str(new_uuid7()),
        "child_count": 1,
        "request_hash": _HASH,
        "base_spec": {"template_id": "pytorch-cifar10-cnn"},
        "expansion": [{"parameters": {"seed": 1}, "parameter_hash": _HASH}],
        **changes,
    }


def _triggers(connection):
    return set(
        connection.execute(
            text(
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgrelid IN "
                "('sweep_parents'::regclass, 'sweep_children'::regclass)"
            )
        ).scalars()
    )


def test_b16_upgrade_is_additive_and_downgrade_restores(clean_postgres_database: str) -> None:
    config = _config(clean_postgres_database)
    command.upgrade(config, "20260926_0019")
    engine = create_engine(clean_postgres_database)
    try:
        with engine.connect() as connection:
            database = inspect(connection)
            before_columns = {c["name"] for c in database.get_columns("sweep_parents")}
            before_checks = {c["name"] for c in database.get_check_constraints("sweep_parents")}
            assert "inference_extents" not in database.get_table_names()
            assert _triggers(connection) == set()
        command.upgrade(config, "20260928_0020")
        with engine.connect() as connection:
            database = inspect(connection)
            columns = {c["name"]: c for c in database.get_columns("sweep_parents")}
            assert set(columns) - before_columns == {"request_hash", "base_spec", "expansion"}
            assert not any(columns[name]["nullable"] for name in set(columns) - before_columns)
            checks = {c["name"] for c in database.get_check_constraints("sweep_parents")}
            assert checks - before_checks == {
                "ck_sweep_parents_request_hash",
                "ck_sweep_parents_expansion_length",
            }
            assert {c["name"] for c in database.get_columns("inference_extents")} == {
                "job_id",
                "tenant_id",
                "item_count",
                "chunk_size",
                "source_attempt_id",
                "source_job_fence",
                "recorded_at",
            }
            indexes = {i["name"] for i in database.get_indexes("idempotency_records")}
            assert "ix_idempotency_records_b16_sweep_parent" in indexes
            assert _triggers(connection) == _TRIGGERS
        command.downgrade(config, "20260926_0019")
        with engine.connect() as connection:
            database = inspect(connection)
            assert {c["name"] for c in database.get_columns("sweep_parents")} == before_columns
            assert {
                c["name"] for c in database.get_check_constraints("sweep_parents")
            } == before_checks
            assert "inference_extents" not in database.get_table_names()
            indexes = {i["name"] for i in database.get_indexes("idempotency_records")}
            assert "ix_idempotency_records_b16_sweep_parent" not in indexes
            assert _triggers(connection) == set()
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_proc WHERE proname = 'nexa_b16_guard_sweep_parent'"
                    )
                ).scalar_one()
                == 0
            )
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_b16_upgrade_refuses_a_pre_b16_sweep_row(clean_postgres_database: str) -> None:
    config = _config(clean_postgres_database)
    command.upgrade(config, "20260926_0019")
    engine = create_engine(clean_postgres_database)
    try:
        with engine.begin() as connection:
            graph = seed_tenant_graph(connection, label="b16-legacy-sweep")
            connection.execute(
                text(
                    "INSERT INTO sweep_parents (sweep_id, tenant_id, submitter_user_id, "
                    "base_spec_checksum, idempotency_context, child_count) "
                    "VALUES (:sweep, :tenant, :user, :hash, 'legacy', 1)"
                ),
                {
                    "sweep": new_uuid7(),
                    "tenant": graph["tenant_id"],
                    "user": graph["user_id"],
                    "hash": _HASH,
                },
            )
        with pytest.raises(OperationalError, match="without a stored expansion"):
            command.upgrade(config, "20260928_0020")
        with engine.connect() as connection:
            version = connection.execute(text("SELECT version_num FROM alembic_version"))
            assert version.scalar_one() == "20260926_0019"
    finally:
        engine.dispose()


def test_b16_downgrade_refuses_while_a_sweep_exists(clean_postgres_database: str) -> None:
    config = _config(clean_postgres_database)
    command.upgrade(config, "head")
    engine = create_engine(clean_postgres_database)
    try:
        with engine.begin() as connection:
            graph = seed_tenant_graph(connection, label="b16-downgrade-sweep")
            connection.execute(insert(s.sweep_parents).values(**_parent_values(graph)))
        with pytest.raises(OperationalError, match="cannot be kept"):
            command.downgrade(config, "20260926_0019")
    finally:
        engine.dispose()


def test_b16_sweep_rows_are_immutable_except_growing_counts(migrated_postgres_engine) -> None:
    engine = migrated_postgres_engine
    with engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="b16-sweep-guard")
        job = seed_job(connection, graph)
        values = _parent_values(graph)
        connection.execute(insert(s.sweep_parents).values(**values))
        connection.execute(
            insert(s.sweep_children).values(
                sweep_id=values["sweep_id"],
                tenant_id=graph["tenant_id"],
                child_index=0,
                parameter_hash=_HASH,
                job_id=job["job_id"],
            )
        )
        connection.execute(
            update(s.sweep_parents)
            .where(s.sweep_parents.c.sweep_id == values["sweep_id"])
            .values(accepted_count=1)
        )
    parent = s.sweep_parents.c.sweep_id == values["sweep_id"]
    child = s.sweep_children.c.sweep_id == values["sweep_id"]
    for statement in (
        update(s.sweep_parents).where(parent).values(accepted_count=0),
        update(s.sweep_parents).where(parent).values(request_hash="sha256:" + "2" * 64),
        update(s.sweep_parents).where(parent).values(expansion=[{"parameters": {}}]),
        update(s.sweep_parents).where(parent).values(child_count=2),
        s.sweep_parents.delete().where(parent),
        update(s.sweep_children).where(child).values(parameter_hash="sha256:" + "3" * 64),
        s.sweep_children.delete().where(child),
    ):
        with pytest.raises(IntegrityError, match="immutable"), engine.begin() as connection:
            connection.execute(statement)
    with (
        pytest.raises(IntegrityError, match="ck_sweep_parents_expansion_length"),
        engine.begin() as connection,
    ):
        connection.execute(insert(s.sweep_parents).values(**_parent_values(graph, child_count=2)))


def test_b16_inference_extent_is_fenced_bounded_and_immutable(migrated_postgres_engine) -> None:
    engine = migrated_postgres_engine
    with engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="b16-extent")
        job = seed_job(connection, graph, state="RUNNING")
        worker = seed_worker(connection, label="b16-extent")
        ids = seed_authority(connection, graph, job, worker)
        row = {
            "job_id": job["job_id"],
            "tenant_id": graph["tenant_id"],
            "item_count": 5000,
            "chunk_size": 1000,
            "source_attempt_id": ids["attempt_id"],
            "source_job_fence": 1,
        }
    for bad, match in (
        ({"source_job_fence": 2}, "fk_inference_extents_tenant_id_attempts"),
        ({"item_count": 0}, "ck_inference_extents_item_count"),
        ({"chunk_size": 100_001}, "ck_inference_extents_chunk_size"),
    ):
        with pytest.raises(IntegrityError, match=match), engine.begin() as connection:
            connection.execute(insert(s.inference_extents).values(**{**row, **bad}))
    with engine.begin() as connection:
        connection.execute(insert(s.inference_extents).values(**row))
    for statement in (
        update(s.inference_extents).values(item_count=4999),
        s.inference_extents.delete(),
    ):
        with pytest.raises(IntegrityError, match="immutable"), engine.begin() as connection:
            connection.execute(statement)


def test_b16_offline_sql_upgrade_and_downgrade(capsys: pytest.CaptureFixture[str]) -> None:
    placeholder = _config("postgresql+psycopg://unused/unused")
    command.upgrade(placeholder, "20260926_0019:20260928_0020", sql=True)
    upgrade = capsys.readouterr().out
    assert "ALTER TABLE sweep_parents ADD COLUMN expansion JSONB NOT NULL" in upgrade
    assert "CREATE TABLE inference_extents" in upgrade
    assert "CREATE TRIGGER trg_sweep_parents_guard" in upgrade
    assert "CREATE INDEX ix_idempotency_records_b16_sweep_parent" in upgrade
    assert "UPDATE alembic_version SET version_num='20260928_0020'" in upgrade
    command.downgrade(placeholder, "20260928_0020:20260926_0019", sql=True)
    downgrade = capsys.readouterr().out
    lock = downgrade.index("LOCK TABLE sweep_parents, sweep_children, inference_extents")
    precheck = downgrade.index("they cannot be kept")
    drop = downgrade.index("DROP TABLE inference_extents")
    assert lock < precheck < drop
    assert "ALTER TABLE sweep_parents DROP COLUMN expansion" in downgrade
    assert "UPDATE alembic_version SET version_num='20260926_0019'" in downgrade


def _definitions(connection, schema: str, table_name: str) -> dict[str, str]:
    indexes = {
        row.indexname: row.indexdef.replace(f" ON {schema}.", " ON ")
        for row in connection.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = :schema AND tablename = :table"
            ),
            {"schema": schema, "table": table_name},
        )
    }
    checks = {
        row.conname: row.definition
        for row in connection.execute(
            text(
                "SELECT c.conname, pg_get_constraintdef(c.oid) AS definition "
                "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = :schema AND t.relname = :table AND c.contype = 'c'"
            ),
            {"schema": schema, "table": table_name},
        )
    }
    return {**indexes, **checks}


def test_b16_metadata_matches_migrated_indexes_and_checks(migrated_postgres_engine) -> None:
    # Build each metadata index and check on an empty LIKE copy and compare
    # PostgreSQL's own definitions, so a drifted predicate or expression fails.
    tables = ("sweep_parents", "inference_extents", "idempotency_records")
    copies = MetaData()
    with migrated_postgres_engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA b16_parity"))
        try:
            for table_name in tables:
                connection.execute(
                    text(f"CREATE TABLE b16_parity.{table_name} (LIKE public.{table_name})")
                )
                copy = schema_v17.metadata.tables[table_name].to_metadata(
                    copies, schema="b16_parity"
                )
                for constraint in copy.constraints:
                    if isinstance(constraint, CheckConstraint):
                        connection.execute(AddConstraint(constraint))
                for index in copy.indexes:
                    connection.execute(CreateIndex(index))
                expected = _definitions(connection, "b16_parity", table_name)
                actual = _definitions(connection, "public", table_name)
                assert expected, table_name
                for name, definition in expected.items():
                    assert actual.get(name) == definition, name
        finally:
            connection.execute(text("DROP SCHEMA b16_parity CASCADE"))
