import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, Table, create_engine, func, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from nexa.coordinator.eligibility import process_eligibility_batch
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.schema import metadata
from tests.integration._factories import seed_authority, seed_job
from tests.integration.test_coordinator_b11 import seed_dispatchable

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
            assert "auth_control" in inspect(connection).get_table_names()
            assert "ix_jobs_b13_submitter_age" in {
                index["name"] for index in inspect(connection).get_indexes("jobs")
            }
        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert "jobs" not in inspect(connection).get_table_names()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert "jobs" in inspect(connection).get_table_names()
            assert "auth_control" in inspect(connection).get_table_names()
            assert "ix_jobs_b13_submitter_age" in {
                index["name"] for index in inspect(connection).get_indexes("jobs")
            }
    finally:
        engine.dispose()


def test_b13_upgrade_replays_legacy_null_age_without_resetting_valid_age(
    clean_postgres_database: str,
) -> None:
    config = _config(clean_postgres_database)
    command.upgrade(config, "20260925_0012")
    engine = create_engine(clean_postgres_database)
    try:
        graph, _, ids = seed_dispatchable(engine, count=2)
        with engine.begin() as connection:
            blocked_id = seed_job(connection, graph, cpu_millis=7000)["job_id"]
            old_age = connection.execute(select(func.clock_timestamp())).scalar_one() - timedelta(
                seconds=180
            )
            connection.execute(
                update(s.jobs).where(s.jobs.c.job_id == ids[0]).values(eligible_since=None)
            )
            connection.execute(
                update(s.jobs).where(s.jobs.c.job_id == ids[1]).values(eligible_since=old_age)
            )
            connection.execute(
                update(s.jobs).where(s.jobs.c.job_id == blocked_id).values(eligible_since=old_age)
            )
        upgraded_at = datetime.now(UTC)
        command.upgrade(config, "head")
        with Session(engine) as session:
            now = session.execute(select(func.clock_timestamp())).scalar_one()
            pending = process_eligibility_batch(session, now)
            session.commit()
        assert graph["tenant_id"] not in pending
        with engine.connect() as connection:
            ages = dict(
                connection.execute(
                    select(s.jobs.c.job_id, s.jobs.c.eligible_since).where(
                        s.jobs.c.job_id.in_([*ids, blocked_id])
                    )
                ).all()
            )
        assert ages[ids[0]] >= upgraded_at
        assert ages[ids[1]] == old_age
        assert ages[blocked_id] is None
        command.downgrade(config, "20260925_0013")
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_b13_quota_headroom_upgrade_restores_held_age_and_downgrade_folds_it_back(
    clean_postgres_database: str,
) -> None:
    config = _config(clean_postgres_database)
    command.upgrade(config, "20260925_0016")
    engine = create_engine(clean_postgres_database)

    def ages(connection, job_ids):
        return dict(
            connection.execute(
                select(s.jobs.c.job_id, s.jobs.c.eligible_since).where(s.jobs.c.job_id.in_(job_ids))
            ).all()
        )

    def drain():
        with Session(engine) as session:
            while process_eligibility_batch(
                session, session.execute(select(func.clock_timestamp())).scalar_one()
            ):
                pass
            session.commit()

    try:
        graph, worker, ids = seed_dispatchable(engine, count=2)
        with engine.begin() as connection:
            old_age = connection.execute(select(func.clock_timestamp())).scalar_one() - timedelta(
                seconds=90
            )
            small_id = seed_job(connection, graph, cpu_millis=400)["job_id"]
            too_large_id = seed_job(connection, graph, cpu_millis=7000)["job_id"]
            connection.execute(
                update(s.jobs).where(s.jobs.c.job_id == small_id).values(eligible_since=old_age)
            )
            connection.execute(
                update(s.jobs).where(s.jobs.c.job_id == too_large_id).values(eligible_since=None)
            )
            connection.execute(update(s.tenant_policies).values(cpu_limit_millis=1500))
            connection.execute(
                update(s.jobs).where(s.jobs.c.job_id == ids[0]).values(state="DISPATCHING")
            )
            seed_authority(connection, graph, {"job_id": ids[0]}, worker)
        drain()
        with engine.connect() as connection:
            assert ages(connection, [ids[1]])[ids[1]] is None

        upgraded_at = datetime.now(UTC)
        command.upgrade(config, "head")
        with engine.connect() as connection:
            steps = connection.execute(
                select(s.quota_headroom_steps.c.upper_bound, s.quota_headroom_steps.c.resumed_at)
                .where(s.quota_headroom_steps.c.resource == "cpu")
                .order_by(s.quota_headroom_steps.c.upper_bound)
            ).all()
            sizes = dict(
                connection.execute(
                    select(s.queue_request_sizes.c.cpu_millis, s.queue_request_sizes.c.queued_jobs)
                ).all()
            )
        assert steps == [(500, None)]
        assert sizes == {400: 1, 1000: 1, 7000: 1}
        drain()
        with engine.connect() as connection:
            upgraded = ages(connection, [ids[1], small_id, too_large_id])
        assert upgraded[ids[1]] >= upgraded_at
        assert upgraded[small_id] == old_age
        assert upgraded[too_large_id] is None

        command.downgrade(config, "20260925_0016")
        drain()
        with engine.connect() as connection:
            downgraded = ages(connection, [ids[1], small_id, too_large_id])
        assert downgraded == {ids[1]: None, small_id: old_age, too_large_id: None}

        command.upgrade(config, "head")
        drain()
        with engine.begin() as connection:
            connection.execute(
                update(s.allocations).values(state="RELEASED", released_at=func.clock_timestamp())
            )
            released_at = connection.execute(select(func.clock_timestamp())).scalar_one()
        command.downgrade(config, "20260925_0016")
        drain()
        with engine.connect() as connection:
            released = ages(connection, [ids[1], small_id])
        assert released[ids[1]] <= released_at
        assert released[ids[1]] >= upgraded_at
        assert released[small_id] == old_age
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_b14_checkpoint_corruption_upgrade_is_additive_and_downgrades(
    clean_postgres_database: str,
) -> None:
    config = _config(clean_postgres_database)
    command.upgrade(config, "20260925_0017")
    engine = create_engine(clean_postgres_database)
    try:
        with engine.connect() as connection:
            assert "checkpoint_corruptions" not in inspect(connection).get_table_names()
        command.upgrade(config, "20260926_0018")
        with engine.connect() as connection:
            database = inspect(connection)
            assert "checkpoint_corruptions" in database.get_table_names()
            columns = database.get_columns("checkpoint_corruptions")
            assert {column["name"] for column in columns} == {
                "tenant_id",
                "checkpoint_id",
                "reason_code",
                "detected_at",
            }
            triggers = set(
                connection.execute(
                    text(
                        "SELECT tgname FROM pg_trigger "
                        "WHERE tgrelid = 'checkpoint_corruptions'::regclass AND NOT tgisinternal"
                    )
                ).scalars()
            )
        assert triggers == {"trg_checkpoint_corruptions_immutable"}
        command.downgrade(config, "20260925_0017")
        with engine.connect() as connection:
            assert "checkpoint_corruptions" not in inspect(connection).get_table_names()
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_b14_offline_sql_creates_insert_only_corruption_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Offline SQL uses an explicit placeholder target; NEXA_DATABASE_URL stays unset.
    command.upgrade(
        _config("postgresql+psycopg://unused/unused"), "20260925_0017:20260926_0018", sql=True
    )
    output = capsys.readouterr().out

    assert "CREATE TABLE checkpoint_corruptions" in output
    assert "CREATE TRIGGER trg_checkpoint_corruptions_immutable" in output
    assert "UPDATE alembic_version SET version_num='20260926_0018'" in output


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


def test_b05_to_b06_upgrade_preserves_existing_identity_rows(
    clean_postgres_database: str,
) -> None:
    config = _config(clean_postgres_database)
    command.upgrade(config, "20260919_0001")
    engine = create_engine(clean_postgres_database)
    tenant_id = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f1")
    existing_policy_tenant_id = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f8")
    user_id = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f2")
    worker_id = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f3")
    old_credential_id = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f4")
    current_credential_id = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f5")
    created_at = datetime(2026, 9, 20, tzinfo=UTC)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name) "
                    "VALUES (:tenant_id, 'existing', 'Existing')"
                ),
                {"tenant_id": tenant_id},
            )
            connection.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name) "
                    "VALUES (:tenant_id, 'existing-policy', 'Existing Policy')"
                ),
                {"tenant_id": existing_policy_tenant_id},
            )
            connection.execute(
                text("INSERT INTO membership_sets (tenant_id) VALUES (:tenant_id)"),
                {"tenant_id": existing_policy_tenant_id},
            )
            connection.execute(
                text(
                    "INSERT INTO tenant_policies ("
                    "tenant_id, version, weight, cpu_limit_millis, memory_limit_bytes, gpu_limit, "
                    "outstanding_limit, user_outstanding_limit, tenant_active_limit, "
                    "user_active_limit, tenant_rate_per_second, tenant_rate_burst, "
                    "user_rate_per_second, user_rate_burst, "
                    "is_current"
                    ") VALUES ("
                    ":tenant_id, 2, '0:7:0', 123, 456, 1, 99, 11, 3, 2, "
                    "'0:3:0', '0:8:0', '0:2:0', '0:4:0', true"
                    ")"
                ),
                {"tenant_id": existing_policy_tenant_id},
            )
            connection.execute(
                text("INSERT INTO membership_sets (tenant_id) VALUES (:tenant_id)"),
                {"tenant_id": tenant_id},
            )
            connection.execute(
                text(
                    "INSERT INTO users (user_id, username, display_name, password_hash) "
                    "VALUES (:user_id, 'existing@example.test', 'Existing', :password_hash)"
                ),
                {"user_id": user_id, "password_hash": "x" * 32},
            )
            connection.execute(
                text(
                    "INSERT INTO workers (worker_id, admin_state, health) "
                    "VALUES (:worker_id, 'ENABLED', 'STARTING')"
                ),
                {"worker_id": worker_id},
            )
            connection.execute(
                text(
                    "INSERT INTO worker_credentials "
                    "(credential_id, worker_id, credential_hash, scopes, "
                    "expires_at, created_at, updated_at) "
                    "VALUES "
                    "(:old_id, :worker_id, :old_hash, ARRAY['worker:local'], "
                    ":expires_at, :old_at, :old_at), "
                    "(:current_id, :worker_id, :current_hash, ARRAY['worker:local'], "
                    ":expires_at, :current_at, :current_at)"
                ),
                {
                    "old_id": old_credential_id,
                    "current_id": current_credential_id,
                    "worker_id": worker_id,
                    "old_hash": b"o" * 32,
                    "current_hash": b"c" * 32,
                    "expires_at": created_at + timedelta(days=1),
                    "old_at": created_at,
                    "current_at": created_at + timedelta(seconds=1),
                },
            )

        command.upgrade(config, "head")

        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT slug FROM tenants WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                ).scalar_one()
                == "existing"
            )
            assert (
                connection.execute(text("SELECT username FROM users")).scalar_one()
                == "existing@example.test"
            )
            assert (
                connection.execute(
                    text(
                        "SELECT schema_generation FROM nexa_schema_metadata "
                        "WHERE singleton_key = 'nexa'"
                    )
                ).scalar_one()
                == 2
            )
            assert connection.execute(text("SELECT count(*) FROM auth_control")).scalar_one() == 1
            policy = connection.execute(
                text(
                    "SELECT version, weight, cpu_limit_millis, memory_limit_bytes, gpu_limit, "
                    "outstanding_limit, user_outstanding_limit, tenant_active_limit, "
                    "user_active_limit, tenant_rate_per_second, tenant_rate_burst, "
                    "user_rate_per_second, user_rate_burst "
                    "FROM tenant_policies WHERE tenant_id = :tenant_id AND is_current"
                ),
                {"tenant_id": tenant_id},
            ).one()
            assert policy == (
                1,
                "0:1:0",
                0,
                0,
                0,
                2000,
                2000,
                2,
                1,
                "0:5:0",
                "0:20:0",
                "0:2:0",
                "0:10:0",
            )
            preserved_policy = connection.execute(
                text(
                    "SELECT version, weight, cpu_limit_millis, memory_limit_bytes, gpu_limit, "
                    "outstanding_limit, user_outstanding_limit, tenant_active_limit, "
                    "user_active_limit, tenant_rate_per_second, tenant_rate_burst, "
                    "user_rate_per_second, user_rate_burst "
                    "FROM tenant_policies WHERE tenant_id = :tenant_id AND is_current"
                ),
                {"tenant_id": existing_policy_tenant_id},
            ).one()
            assert preserved_policy == (
                2,
                "0:7:0",
                123,
                456,
                1,
                99,
                11,
                3,
                2,
                "0:3:0",
                "0:8:0",
                "0:2:0",
                "0:4:0",
            )
            assert (
                connection.execute(
                    text("SELECT global_outstanding_limit FROM policy_versions WHERE is_current")
                ).scalar_one()
                == 100_000
            )
            credentials = connection.execute(
                text(
                    "SELECT credential_id, revoked_at FROM worker_credentials "
                    "WHERE worker_id = :worker_id ORDER BY created_at"
                ),
                {"worker_id": worker_id},
            ).all()
            assert [row.credential_id for row in credentials] == [
                old_credential_id,
                current_credential_id,
            ]
            assert credentials[0].revoked_at is not None
            assert credentials[1].revoked_at is None
    finally:
        engine.dispose()


def test_b05_to_b06_upgrade_fails_closed_on_casefold_collision(
    clean_postgres_database: str,
) -> None:
    config = _config(clean_postgres_database)
    command.upgrade(config, "20260919_0001")
    engine = create_engine(clean_postgres_database)
    try:
        with engine.begin() as connection:
            for suffix, username in (("1", "straße@example.test"), ("2", "strasse@example.test")):
                connection.execute(
                    text(
                        "INSERT INTO users (user_id, username, display_name, password_hash) "
                        "VALUES (:user_id, :username, :display_name, :password_hash)"
                    ),
                    {
                        "user_id": UUID(
                            "018f05c4-a922-7d0d-9f55-f9084a72d0f6"
                            if suffix == "1"
                            else "018f05c4-a922-7d0d-9f55-f9084a72d0f7"
                        ),
                        "username": username,
                        "display_name": suffix,
                        "password_hash": "x" * 32,
                    },
                )
        with pytest.raises(RuntimeError, match="casefold collision"):
            command.upgrade(config, "head")
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
