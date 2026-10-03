"""B18 migration 0023: index-only support for the cross-tenant admin reads.

adminListJobs without a tenant filter otherwise sorts every job, and adminQueryFairness
otherwise scans the whole ledger history (docs/evidence/raw/B18-explain-*.out).
"""

from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from nexa.application.admin_queries import (
    _FAIRNESS_ALL,
    FAIRNESS_SETTINGS,
    admin_job_statement,
    fairness_parameters,
)
from nexa.infrastructure.persistence import schema as s
from tests.integration.test_migrations import _config

pytestmark = pytest.mark.postgres

JOBS_INDEX = "ix_jobs_created_keyset"
PERIOD_INDEX = "ix_allocation_ledger_segments_period"


def _definitions(connection) -> dict[str, str]:
    rows = connection.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND indexname IN (:jobs, :period)"
        ),
        {"jobs": JOBS_INDEX, "period": PERIOD_INDEX},
    )
    return dict(rows.tuples().all())


def test_b18_upgrade_adds_only_two_indexes_and_downgrade_drops_them(
    clean_postgres_database: str,
) -> None:
    config = _config(clean_postgres_database)
    command.upgrade(config, "20260929_0022")
    engine = create_engine(clean_postgres_database)
    try:
        with engine.connect() as connection:
            assert _definitions(connection) == {}
            database = inspect(connection)
            before = {
                name: {c["name"] for c in database.get_columns(name)}
                for name in database.get_table_names()
            }
        command.upgrade(config, "20261001_0023")
        with engine.connect() as connection:
            definitions = _definitions(connection)
            database = inspect(connection)
            after = {
                name: {c["name"] for c in database.get_columns(name)}
                for name in database.get_table_names()
            }
        assert after == before
        assert "(created_at DESC, job_id DESC)" in definitions[JOBS_INDEX]
        assert "USING gist (tstzrange(started_at, ended_at, '[)'" in definitions[PERIOD_INDEX]
        command.downgrade(config, "20260929_0022")
        with engine.connect() as connection:
            assert _definitions(connection) == {}
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_b18_offline_sql_is_index_only(capsys: pytest.CaptureFixture[str]) -> None:
    command.upgrade(
        _config("postgresql+psycopg://unused/unused"), "20260929_0022:20261001_0023", sql=True
    )
    output = capsys.readouterr().out
    statements = [
        line for line in output.splitlines() if line and not line.startswith(("--", "BEGIN"))
    ]
    ddl = [line for line in statements if not line.startswith(("UPDATE alembic_version", "COMMIT"))]
    assert all(line.startswith("CREATE INDEX") for line in ddl), ddl
    assert len(ddl) == 2
    assert "UPDATE alembic_version SET version_num='20261001_0023'" in output


def test_b18_metadata_declares_the_indexes() -> None:
    jobs_index = next(index for index in s.jobs.indexes if index.name == JOBS_INDEX)
    period_index = next(
        index for index in s.allocation_ledger_segments.indexes if index.name == PERIOD_INDEX
    )
    assert period_index.dialect_options["postgresql"]["using"] == "gist"
    assert len(jobs_index.expressions) == 2


def test_b18_admin_reads_can_use_the_new_indexes(migrated_postgres_engine) -> None:
    engine = migrated_postgres_engine
    to_at = datetime(2026, 9, 20, tzinfo=UTC)
    from_at = to_at - timedelta(hours=1)
    statement = admin_job_statement(
        tenant_id=None,
        user_id=None,
        state=None,
        waiting_reason=None,
        created_after=None,
        after=None,
        limit=51,
    )
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        connection.execute(text("SET LOCAL enable_sort = off"))
        compiled = statement.compile(dialect=engine.dialect)
        jobs_plan = "\n".join(
            connection.exec_driver_sql("EXPLAIN " + str(compiled), compiled.params).scalars()
        )
        connection.execute(FAIRNESS_SETTINGS)
        fairness_plan = "\n".join(
            connection.execute(
                text("EXPLAIN " + _FAIRNESS_ALL.text),
                fairness_parameters(from_at=from_at, to_at=to_at, bucket_seconds=300),
            ).scalars()
        )
    assert JOBS_INDEX in jobs_plan
    assert PERIOD_INDEX in fairness_plan
