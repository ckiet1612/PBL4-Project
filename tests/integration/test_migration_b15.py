from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import MetaData, create_engine, func, insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.schema import CreateIndex

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence import schema_v16
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.integration._factories import seed_job, seed_tenant_graph
from tests.integration.test_migrations import _config

pytestmark = pytest.mark.postgres

_QUEUE_INDEXES = (
    "ix_jobs_b13_head",
    "ix_jobs_b13_submitter",
    "ix_jobs_b13_priority_age",
    "ix_jobs_b13_submitter_age",
)
_QUEUE_FUNCTIONS = (
    "nexa_b13_refresh_queue_head",
    "nexa_b13_job_head_change",
    "nexa_b13_refresh_queue_submitter",
    "nexa_b13_job_submitter_change",
    "nexa_b13_emit_eligibility",
    "nexa_b13_job_queue_size",
    "nexa_b13_spec_queue_size",
)


def _index_predicates(connection):
    return {
        row.indexname: row.indexdef
        for row in connection.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'jobs'")
        )
        if row.indexname in _QUEUE_INDEXES
    }


def _function_sources(connection):
    return {
        row.proname: row.prosrc
        for row in connection.execute(
            text("SELECT proname, prosrc FROM pg_proc WHERE proname = ANY(:names)"),
            {"names": list(_QUEUE_FUNCTIONS)},
        )
    }


def test_b15_upgrade_widens_queue_predicate_and_downgrade_restores_it(
    clean_postgres_database: str,
) -> None:
    config = _config(clean_postgres_database)
    command.upgrade(config, "20260926_0018")
    engine = create_engine(clean_postgres_database)
    try:
        with engine.connect() as connection:
            before_indexes = _index_predicates(connection)
            before_functions = _function_sources(connection)
            audit_length = next(
                column["type"].length
                for column in inspect(connection).get_columns("audit_records")
                if column["name"] == "reason"
            )
        assert audit_length == 128
        assert len(before_indexes) == 4 and len(before_functions) == 7
        assert all("recovery_intent IS NULL" in value for value in before_indexes.values())

        command.upgrade(config, "20260926_0019")
        with engine.connect() as connection:
            after_indexes = _index_predicates(connection)
            after_functions = _function_sources(connection)
            database = inspect(connection)
            checks = {check["name"] for check in database.get_check_constraints("jobs")}
            audit_length = next(
                column["type"].length
                for column in database.get_columns("audit_records")
                if column["name"] == "reason"
            )
            idempotency_indexes = {
                index["name"] for index in database.get_indexes("idempotency_records")
            }
            event_indexes = {index["name"] for index in database.get_indexes("events")}
            incarnation_columns = {
                column["name"] for column in database.get_columns("worker_incarnations")
            }
        assert "ck_jobs_queued_dispatchable" in checks
        assert audit_length == 256
        assert "ix_idempotency_records_resource" in idempotency_indexes
        assert "ix_idempotency_records_b15_sweep" in idempotency_indexes
        assert "ix_events_b15_recovery" in event_indexes
        assert "ready_checked_at" in incarnation_columns
        for name, definition in after_indexes.items():
            assert definition.endswith("WHERE ((state)::text = 'QUEUED'::text)"), name
        for name, source in after_functions.items():
            assert "desired_state = 'RUNNING'" not in source, name
            assert "recovery_intent IS NULL" not in source, name

        command.downgrade(config, "20260926_0018")
        with engine.connect() as connection:
            assert _index_predicates(connection) == before_indexes
            restored = _function_sources(connection)
            checks = {check["name"] for check in inspect(connection).get_check_constraints("jobs")}
            idempotency_indexes = {
                index["name"] for index in inspect(connection).get_indexes("idempotency_records")
            }
            event_indexes = {index["name"] for index in inspect(connection).get_indexes("events")}
            audit_length = next(
                column["type"].length
                for column in inspect(connection).get_columns("audit_records")
                if column["name"] == "reason"
            )
            incarnation_columns = {
                column["name"] for column in inspect(connection).get_columns("worker_incarnations")
            }
        assert "ck_jobs_queued_dispatchable" not in checks
        assert "ix_events_b15_recovery" not in event_indexes
        assert "ready_checked_at" not in incarnation_columns
        assert audit_length == 128
        assert not {"ix_idempotency_records_resource", "ix_idempotency_records_b15_sweep"} & (
            idempotency_indexes
        )
        for name in _QUEUE_FUNCTIONS:
            normalized = " ".join(restored[name].split())
            assert normalized == " ".join(before_functions[name].split()), name
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_b15_queued_rows_must_be_dispatchable(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="b15-check")
        job_id = seed_job(connection, graph)["job_id"]
    invalid = (("PAUSED", None), ("CANCELLED", None), ("RUNNING", "CHECKPOINT_FOR_PAUSE"))
    for desired, intent in invalid:
        with (
            pytest.raises(IntegrityError, match="ck_jobs_queued_dispatchable"),
            migrated_postgres_engine.begin() as connection,
        ):
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == job_id)
                .values(desired_state=desired, recovery_intent=intent)
            )
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.jobs)
            .where(s.jobs.c.job_id == job_id)
            .values(desired_state="PAUSED", recovery_intent="CHECKPOINT_FOR_PAUSE")
        )
        head = connection.execute(
            text("SELECT candidate_job_id FROM queue_heads WHERE candidate_job_id = :job"),
            {"job": job_id},
        ).scalar_one_or_none()
    # A checkpoint-for-pause job stays in the B13 queue structures.
    assert head == job_id


def test_b15_downgrade_refuses_while_checkpoint_for_pause_is_queued(
    clean_postgres_database: str,
) -> None:
    config = _config(clean_postgres_database)
    command.upgrade(config, "head")
    engine = create_engine(clean_postgres_database)
    try:
        with engine.begin() as connection:
            graph = seed_tenant_graph(connection, label="b15-downgrade")
            job_id = seed_job(connection, graph)["job_id"]
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == job_id)
                .values(desired_state="PAUSED", recovery_intent="CHECKPOINT_FOR_PAUSE")
            )
        with pytest.raises(OperationalError, match="checkpoint-for-pause job is queued"):
            command.downgrade(config, "20260926_0018")
    finally:
        engine.dispose()


def test_b15_downgrade_refuses_to_narrow_a_long_audit_reason(
    clean_postgres_database: str,
) -> None:
    config = _config(clean_postgres_database)
    command.upgrade(config, "head")
    engine = create_engine(clean_postgres_database)
    try:
        with engine.begin() as connection:
            graph = seed_tenant_graph(connection, label="b15-audit")
            connection.execute(
                insert(s.audit_records).values(
                    audit_id=new_uuid7(),
                    actor_type="USER",
                    actor_id=str(graph["user_id"]),
                    tenant_id=graph["tenant_id"],
                    action="job.cancel",
                    target_type="JOB",
                    target_id=str(new_uuid7()),
                    reason="r" * 129,
                )
            )
        with pytest.raises(OperationalError, match="audit reason exceeds 128"):
            command.downgrade(config, "20260926_0018")
        with engine.connect() as connection:
            version = connection.execute(text("SELECT version_num FROM alembic_version"))
            assert version.scalar_one() == "20260926_0019"
    finally:
        engine.dispose()


def test_b15_offline_downgrade_sql_locks_prechecks_and_restores(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.downgrade(
        _config("postgresql+psycopg://unused/unused"), "20260926_0019:20260926_0018", sql=True
    )
    output = capsys.readouterr().out

    lock = output.index("LOCK TABLE jobs, audit_records IN ACCESS EXCLUSIVE MODE")
    precheck = output.index("an audit reason exceeds 128 characters")
    narrow = output.index("ALTER TABLE audit_records ALTER COLUMN reason TYPE VARCHAR(128)")
    assert lock < precheck < narrow
    assert "DROP INDEX ix_events_b15_recovery" in output
    assert "ALTER TABLE jobs DROP CONSTRAINT ck_jobs_queued_dispatchable" in output
    assert "UPDATE alembic_version SET version_num='20260926_0018'" in output


def test_b15_offline_sql_contains_queue_predicate_change(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Offline SQL uses an explicit placeholder target; NEXA_DATABASE_URL stays unset.
    command.upgrade(
        _config("postgresql+psycopg://unused/unused"), "20260926_0018:20260926_0019", sql=True
    )
    output = capsys.readouterr().out

    assert "ck_jobs_queued_dispatchable" in output
    assert "CREATE OR REPLACE FUNCTION nexa_b13_job_queue_size()" in output
    assert "ALTER TABLE audit_records ALTER COLUMN reason TYPE VARCHAR(256)" in output
    assert "UPDATE jobs SET terminal_at = updated_at" in output
    assert "UPDATE alembic_version SET version_num='20260926_0019'" in output


def _index_definitions(connection, schema: str, table_name: str) -> dict[str, str]:
    return {
        row.indexname: row.indexdef.replace(f" ON {schema}.", " ON ")
        for row in connection.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = :schema AND tablename = :table"
            ),
            {"schema": schema, "table": table_name},
        )
    }


def test_b15_metadata_matches_migrated_indexes(migrated_postgres_engine) -> None:
    # Names alone would miss a drifted predicate: build each metadata index on an
    # empty copy of the table and compare PostgreSQL's own definitions (L4).
    tables = ("jobs", "events", "idempotency_records")
    copies = MetaData()
    with migrated_postgres_engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA b15_parity"))
        try:
            for table_name in tables:
                connection.execute(
                    text(f"CREATE TABLE b15_parity.{table_name} (LIKE public.{table_name})")
                )
                copy = schema_v16.metadata.tables[table_name].to_metadata(
                    copies, schema="b15_parity"
                )
                for index in copy.indexes:
                    connection.execute(CreateIndex(index))
                expected = _index_definitions(connection, "b15_parity", table_name)
                actual = _index_definitions(connection, "public", table_name)
                assert expected, table_name
                for name, definition in expected.items():
                    assert actual.get(name) == definition, name
        finally:
            connection.execute(text("DROP SCHEMA b15_parity CASCADE"))


def _submit_record(connection, graph, job_id, *, operation="submitJob", age_days=10):
    record_id = new_uuid7()
    submitted = func.now() - timedelta(days=age_days)
    connection.execute(
        insert(s.idempotency_records).values(
            idempotency_id=record_id,
            context=str(graph["tenant_id"]),
            principal_id=str(graph["user_id"]),
            operation_id=operation,
            idempotency_key=f"b15-backfill-{record_id}",
            request_hash="sha256:" + "0" * 64,
            state="COMPLETED",
            resource_id=job_id,
            # Pre-B15 submit records expired a fixed retention after submission.
            expires_at=submitted + timedelta(days=7),
            created_at=submitted,
            updated_at=submitted,
        )
    )
    return record_id


def test_b15_upgrade_backfills_retention_of_jobs_terminal_before_it(
    clean_postgres_database: str,
) -> None:
    # B15-R25: a job terminal before B15 has no terminal_at, and its submit record
    # expired a fixed time after submission; the sweep would delete it early.
    config = _config(clean_postgres_database)
    command.upgrade(config, "20260926_0018")
    engine = create_engine(clean_postgres_database)
    try:
        with engine.begin() as connection:
            graph = seed_tenant_graph(connection, label="b15-backfill")
            finished = seed_job(connection, graph, state="SUCCEEDED")["job_id"]
            running = seed_job(connection, graph, state="RUNNING")["job_id"]
            for job_id in (finished, running):
                connection.execute(
                    update(s.jobs)
                    .where(s.jobs.c.job_id == job_id)
                    .values(
                        created_at=func.now() - timedelta(days=10),
                        updated_at=func.now() - timedelta(days=1),
                    )
                )
            swept = _submit_record(connection, graph, finished)
            other = _submit_record(connection, graph, finished, operation="uploadJobInput")
            live = _submit_record(connection, graph, running)
            before = dict(
                connection.execute(
                    select(
                        s.idempotency_records.c.idempotency_id, s.idempotency_records.c.expires_at
                    )
                ).all()
            )

        command.upgrade(config, "20260926_0019")
        with engine.connect() as connection:
            jobs = {
                row.job_id: row
                for row in connection.execute(
                    select(s.jobs.c.job_id, s.jobs.c.terminal_at, s.jobs.c.updated_at)
                )
            }
            after = dict(
                connection.execute(
                    select(
                        s.idempotency_records.c.idempotency_id, s.idempotency_records.c.expires_at
                    )
                ).all()
            )
        # The last update of an immutable terminal row bounds its terminal time from above.
        assert jobs[finished].terminal_at == jobs[finished].updated_at
        assert jobs[running].terminal_at is None
        assert after[swept] == jobs[finished].terminal_at + timedelta(days=7)
        assert after[other] == before[other]
        assert after[live] == before[live]
    finally:
        engine.dispose()
