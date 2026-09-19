import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, Table, create_engine, inspect, text
from sqlalchemy.engine import make_url

from nexa.infrastructure.persistence.schema import metadata

pytestmark = pytest.mark.postgres

_MIGRATION_LOCK_KEY = 7_149_208_505_001


def _config(url: str, *, script_location: str = "migrations") -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", script_location)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@contextmanager
def _temporary_database(base_url: str):
    database_name = f"nexa_b05_test_migration_target_{uuid4().hex}"
    admin_url = make_url(base_url).set(database="postgres")
    database_url = make_url(base_url).set(database=database_name)
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        yield database_url.render_as_string(hide_password=False)
    finally:
        with engine.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        engine.dispose()


def test_migration_has_exactly_one_head() -> None:
    script = ScriptDirectory.from_config(_config("postgresql+psycopg://unused/unused"))

    assert len(script.get_heads()) == 1


def test_clean_repeat_downgrade_and_reupgrade(clean_postgres_database: str) -> None:
    config = _config(clean_postgres_database)

    command.upgrade(config, "head")
    command.upgrade(config, "head")
    engine = create_engine(clean_postgres_database)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 1
            )
            assert "jobs" in inspect(connection).get_table_names()
        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert "jobs" not in inspect(connection).get_table_names()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert "jobs" in inspect(connection).get_table_names()
    finally:
        engine.dispose()


def test_explicit_migration_target_is_not_overridden_by_runtime_database_url(
    clean_postgres_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _temporary_database(clean_postgres_database) as runtime_database_url:
        monkeypatch.setenv("NEXA_DATABASE_URL", runtime_database_url)

        command.upgrade(_config(clean_postgres_database), "head")

        target_engine = create_engine(clean_postgres_database)
        runtime_engine = create_engine(runtime_database_url)
        try:
            assert "jobs" in inspect(target_engine).get_table_names()
            assert inspect(runtime_engine).get_table_names() == []
        finally:
            target_engine.dispose()
            runtime_engine.dispose()


def test_migration_matches_sqlalchemy_table_and_column_map(clean_postgres_database: str) -> None:
    command.upgrade(_config(clean_postgres_database), "head")
    engine = create_engine(clean_postgres_database)
    try:
        database = inspect(engine)
        assert set(database.get_table_names()) - {"alembic_version"} == set(metadata.tables)
        for table_name, table in metadata.tables.items():
            reflected_columns = {column["name"] for column in database.get_columns(table_name)}
            assert reflected_columns == set(table.columns.keys())
    finally:
        engine.dispose()


def test_initial_revision_is_frozen_from_future_metadata_changes(
    clean_postgres_database: str,
) -> None:
    future_table = Table("future_schema_table", metadata, Column("id", Integer, primary_key=True))
    try:
        command.upgrade(_config(clean_postgres_database), "head")
        engine = create_engine(clean_postgres_database)
        try:
            assert "future_schema_table" not in inspect(engine).get_table_names()
        finally:
            engine.dispose()
    finally:
        metadata.remove(future_table)


def test_two_migration_runners_serialize_on_the_advisory_lock(
    clean_postgres_database: str,
) -> None:
    application_names = {"nexa-b05-migration-1", "nexa-b05-migration-2"}
    engine = create_engine(clean_postgres_database)
    observer_engine = create_engine(clean_postgres_database, isolation_level="AUTOCOMMIT")
    processes: list[subprocess.Popen[str]] = []
    try:
        with engine.connect() as lock_connection, observer_engine.connect() as observer:
            lock_connection.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY}
            )
            for application_name in sorted(application_names):
                process_env = os.environ.copy()
                process_env["NEXA_DATABASE_URL"] = clean_postgres_database
                process_env["PGAPPNAME"] = application_name
                processes.append(
                    subprocess.Popen(
                        [sys.executable, "-m", "alembic", "upgrade", "head"],
                        cwd=Path.cwd(),
                        env=process_env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                waiting = {
                    row.application_name
                    for row in observer.execute(
                        text(
                            "SELECT application_name "
                            "FROM pg_stat_activity "
                            "WHERE application_name IN (:first, :second) "
                            "AND wait_event_type = 'Lock' "
                            "AND wait_event = 'advisory'"
                        ),
                        {
                            "first": min(application_names),
                            "second": max(application_names),
                        },
                    )
                }
                if waiting == application_names:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("both migration runners did not wait on the advisory lock")

            lock_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK_KEY}
            )
            lock_connection.commit()

        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, f"{stdout}\n{stderr}"

        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 1
            )
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        observer_engine.dispose()
        engine.dispose()


def test_failed_revision_rolls_back_transactional_ddl(
    clean_postgres_database: str, tmp_path: Path
) -> None:
    scripts = tmp_path / "migrations"
    versions = scripts / "versions"
    versions.mkdir(parents=True)
    source_env = Path("migrations/env.py").read_text(encoding="utf-8")
    (scripts / "env.py").write_text(source_env, encoding="utf-8")
    (scripts / "script.py.mako").write_text("", encoding="utf-8")
    (versions / "broken.py").write_text(
        "from alembic import op\n"
        "revision = 'broken'\n"
        "down_revision = None\n"
        "branch_labels = None\n"
        "depends_on = None\n\n"
        "def upgrade():\n"
        "    op.execute('CREATE TABLE migration_should_rollback (id integer)')\n"
        "    op.execute('SELECT definitely_missing_function()')\n\n"
        "def downgrade():\n"
        "    op.drop_table('migration_should_rollback')\n",
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="definitely_missing_function"):
        command.upgrade(_config(clean_postgres_database, script_location=str(scripts)), "head")

    engine = create_engine(clean_postgres_database)
    try:
        assert "migration_should_rollback" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
