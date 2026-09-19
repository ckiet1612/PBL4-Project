import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from nexa.infrastructure.persistence.database import create_database_engine, create_session_factory
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import (
    clock_timestamp,
    compare_and_swap,
    lock_rows,
    transaction_timestamp,
)
from nexa.infrastructure.persistence.schema import (
    admission_counters,
    audit_records,
    events,
    idempotency_records,
    jobs,
    tenants,
)
from nexa.infrastructure.persistence.transactions import run_transaction

from ._factories import CHECKSUM, seed_job, seed_tenant_graph

pytestmark = pytest.mark.postgres


def test_database_engine_returns_connection_to_pool(postgres_database_url: str) -> None:
    engine = create_database_engine(postgres_database_url, pool_size=1, max_overflow=0)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            assert engine.pool.checkedout() == 1
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


def test_two_independent_writers_with_same_version_have_one_cas_winner(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="cas")

    second_started = threading.Event()
    second_finished = threading.Event()
    second_result: list[bool] = []

    first_connection = migrated_postgres_engine.connect()
    first_transaction = first_connection.begin()
    assert compare_and_swap(
        first_connection,
        tenants,
        {"tenant_id": graph["tenant_id"]},
        expected_version=1,
        values={"display_name": "First writer"},
    )

    def second_writer() -> None:
        with migrated_postgres_engine.begin() as connection:
            second_started.set()
            second_result.append(
                compare_and_swap(
                    connection,
                    tenants,
                    {"tenant_id": graph["tenant_id"]},
                    expected_version=1,
                    values={"display_name": "Second writer"},
                )
            )
        second_finished.set()

    thread = threading.Thread(target=second_writer, daemon=True)
    thread.start()
    assert second_started.wait(timeout=5)
    first_transaction.commit()
    first_connection.close()
    assert second_finished.wait(timeout=5)
    thread.join(timeout=1)

    assert second_result == [False]
    with migrated_postgres_engine.connect() as connection:
        row = connection.execute(
            select(tenants.c.display_name, tenants.c.version).where(
                tenants.c.tenant_id == graph["tenant_id"]
            )
        ).one()
    assert row == ("First writer", 2)


def test_cas_loser_commits_no_event_or_counter_side_effect(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="cas-side-effects")
        job = seed_job(connection, graph)
        connection.execute(
            admission_counters.insert(),
            {
                "scope_type": "TENANT",
                "scope_id": str(graph["tenant_id"]),
                "outstanding": 0,
                "active_attempts": 0,
                "version": 1,
            },
        )

    def apply_winner_side_effects(connection, actor_id: str) -> bool:
        won = compare_and_swap(
            connection,
            jobs,
            {"job_id": job["job_id"]},
            expected_version=1,
            values={"state": "RUNNING", "event_sequence": 1},
        )
        if not won:
            return False
        connection.execute(
            events.insert(),
            {
                "event_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "sequence": 1,
                "event_type": "JOB_STARTED",
                "actor_type": "SYSTEM",
                "actor_id": actor_id,
                "safe_metadata": {},
            },
        )
        connection.execute(
            admission_counters.update()
            .where(
                admission_counters.c.scope_type == "TENANT",
                admission_counters.c.scope_id == str(graph["tenant_id"]),
            )
            .values(outstanding=1, active_attempts=1, version=2)
        )
        return True

    second_started = threading.Event()
    second_finished = threading.Event()
    second_result: list[bool] = []

    first_connection = migrated_postgres_engine.connect()
    first_transaction = first_connection.begin()
    assert apply_winner_side_effects(first_connection, "writer-one")

    def second_writer() -> None:
        with migrated_postgres_engine.begin() as connection:
            second_started.set()
            second_result.append(apply_winner_side_effects(connection, "writer-two"))
        second_finished.set()

    thread = threading.Thread(target=second_writer, daemon=True)
    thread.start()
    assert second_started.wait(timeout=5)
    first_transaction.commit()
    first_connection.close()
    assert second_finished.wait(timeout=5)
    thread.join(timeout=1)

    assert second_result == [False]
    with migrated_postgres_engine.connect() as connection:
        job_row = connection.execute(
            select(jobs.c.state, jobs.c.version, jobs.c.event_sequence).where(
                jobs.c.job_id == job["job_id"]
            )
        ).one()
        event_rows = connection.execute(
            select(events.c.actor_id).where(events.c.job_id == job["job_id"])
        ).all()
        counter_row = connection.execute(
            select(
                admission_counters.c.outstanding,
                admission_counters.c.active_attempts,
                admission_counters.c.version,
            ).where(
                admission_counters.c.scope_type == "TENANT",
                admission_counters.c.scope_id == str(graph["tenant_id"]),
            )
        ).one()

    assert job_row == ("RUNNING", 2, 1)
    assert event_rows == [("writer-one",)]
    assert counter_row == (1, 1, 2)


def _run_job_transition(session, graph, job, *, suffix: str, abort: bool) -> str:
    assert compare_and_swap(
        session,
        jobs,
        {"job_id": job["job_id"]},
        expected_version=1,
        values={"state": "RUNNING", "event_sequence": 1},
    )
    session.execute(
        events.insert(),
        {
            "event_id": new_uuid7(),
            "tenant_id": graph["tenant_id"],
            "job_id": job["job_id"],
            "sequence": 1,
            "event_type": "JOB_STARTED",
            "actor_type": "SYSTEM",
            "actor_id": "coordinator",
            "safe_metadata": {},
        },
    )
    session.execute(
        admission_counters.update()
        .where(
            admission_counters.c.scope_type == "TENANT",
            admission_counters.c.scope_id == str(graph["tenant_id"]),
        )
        .values(outstanding=1, active_attempts=1, version=2)
    )
    session.execute(
        idempotency_records.insert(),
        {
            "idempotency_id": new_uuid7(),
            "context": str(graph["tenant_id"]),
            "principal_id": str(graph["user_id"]),
            "operation_id": "startJob",
            "idempotency_key": f"0123456789abcdef-{suffix}",
            "request_hash": CHECKSUM,
            "state": "COMPLETED",
            "response_status": 200,
            "response_body": {"job_id": str(job["job_id"])},
            "response_headers": {},
            "one_time_secret": False,
            "expires_at": datetime.now(UTC) + timedelta(days=30),
        },
    )
    if abort:
        raise RuntimeError("abort helper transaction")
    return "committed"


def _seed_transaction_job(connection, *, label: str):
    graph = seed_tenant_graph(connection, label=label)
    job = seed_job(connection, graph)
    connection.execute(
        admission_counters.insert(),
        {
            "scope_type": "TENANT",
            "scope_id": str(graph["tenant_id"]),
            "outstanding": 0,
            "active_attempts": 0,
            "version": 1,
        },
    )
    return graph, job


def test_run_transaction_commits_job_event_counter_and_idempotency_atomically(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph, job = _seed_transaction_job(connection, label="helper-commit")

    result = run_transaction(
        create_session_factory(migrated_postgres_engine),
        lambda session: _run_job_transition(
            session, graph, job, suffix="helper-commit", abort=False
        ),
    )

    assert result == "committed"
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(
            select(jobs.c.state, jobs.c.version, jobs.c.event_sequence).where(
                jobs.c.job_id == job["job_id"]
            )
        ).one() == ("RUNNING", 2, 1)
        assert connection.execute(
            select(events.c.event_type).where(events.c.job_id == job["job_id"])
        ).all() == [("JOB_STARTED",)]
        assert connection.execute(
            select(
                admission_counters.c.outstanding,
                admission_counters.c.active_attempts,
                admission_counters.c.version,
            ).where(
                admission_counters.c.scope_type == "TENANT",
                admission_counters.c.scope_id == str(graph["tenant_id"]),
            )
        ).one() == (1, 1, 2)
        assert connection.execute(
            select(idempotency_records.c.state).where(
                idempotency_records.c.idempotency_key == "0123456789abcdef-helper-commit"
            )
        ).all() == [("COMPLETED",)]


def test_run_transaction_rolls_back_job_event_counter_and_idempotency_atomically(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph, job = _seed_transaction_job(connection, label="helper-rollback")

    with pytest.raises(RuntimeError, match="abort helper transaction"):
        run_transaction(
            create_session_factory(migrated_postgres_engine),
            lambda session: _run_job_transition(
                session, graph, job, suffix="helper-rollback", abort=True
            ),
        )

    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(
            select(jobs.c.state, jobs.c.version, jobs.c.event_sequence).where(
                jobs.c.job_id == job["job_id"]
            )
        ).one() == ("QUEUED", 1, 0)
        assert (
            connection.execute(
                select(events.c.event_id).where(events.c.job_id == job["job_id"])
            ).all()
            == []
        )
        assert connection.execute(
            select(
                admission_counters.c.outstanding,
                admission_counters.c.active_attempts,
                admission_counters.c.version,
            ).where(
                admission_counters.c.scope_type == "TENANT",
                admission_counters.c.scope_id == str(graph["tenant_id"]),
            )
        ).one() == (0, 0, 1)
        assert (
            connection.execute(
                select(idempotency_records.c.idempotency_id).where(
                    idempotency_records.c.idempotency_key == "0123456789abcdef-helper-rollback"
                )
            ).all()
            == []
        )


def test_unit_of_work_rolls_back_state_audit_and_idempotency_together(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="rollback")

    with (
        pytest.raises(RuntimeError, match="abort whole transaction"),
        migrated_postgres_engine.begin() as connection,
    ):
        assert compare_and_swap(
            connection,
            tenants,
            {"tenant_id": graph["tenant_id"]},
            expected_version=1,
            values={"display_name": "Must roll back"},
        )
        connection.execute(
            audit_records.insert(),
            {
                "audit_id": new_uuid7(),
                "actor_type": "USER",
                "actor_id": str(graph["user_id"]),
                "tenant_id": graph["tenant_id"],
                "action": "TENANT_UPDATE",
                "target_type": "TENANT",
                "target_id": str(graph["tenant_id"]),
                "before_version": 1,
                "after_version": 2,
                "safe_metadata": {},
            },
        )
        connection.execute(
            idempotency_records.insert(),
            {
                "idempotency_id": new_uuid7(),
                "context": str(graph["tenant_id"]),
                "principal_id": str(graph["user_id"]),
                "operation_id": "updateTenant",
                "idempotency_key": "0123456789abcdef-rollback",
                "request_hash": CHECKSUM,
                "state": "COMPLETED",
                "response_status": 200,
                "response_body": {},
                "response_headers": {},
                "one_time_secret": False,
                "expires_at": datetime.now(UTC) + timedelta(days=30),
            },
        )
        raise RuntimeError("abort whole transaction")

    with migrated_postgres_engine.connect() as connection:
        tenant = connection.execute(
            select(tenants.c.display_name, tenants.c.version).where(
                tenants.c.tenant_id == graph["tenant_id"]
            )
        ).one()
        assert tenant == ("Tenant rollback", 1)
        assert connection.execute(select(audit_records)).all() == []
        assert connection.execute(select(idempotency_records)).all() == []


def test_lock_rows_uses_stable_id_order(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        first = seed_tenant_graph(connection, label="lock-a")
        second = seed_tenant_graph(connection, label="lock-b")

    requested = [second["tenant_id"], first["tenant_id"]]
    with migrated_postgres_engine.begin() as connection:
        rows = lock_rows(connection, tenants, tenants.c.tenant_id, requested)

    assert [row.tenant_id for row in rows] == sorted(requested, key=lambda value: value.bytes)


def test_transaction_timestamp_is_stable_while_clock_timestamp_advances(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        transaction_before = transaction_timestamp(connection)
        clock_before = clock_timestamp(connection)
        connection.execute(text("SELECT pg_sleep(0.01)"))
        transaction_after = transaction_timestamp(connection)
        clock_after = clock_timestamp(connection)

    assert transaction_before == transaction_after
    assert clock_after > clock_before
