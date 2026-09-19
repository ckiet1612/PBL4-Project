import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema import (
    allocation_ledger_segments,
    artifact_references,
    events,
    policy_versions,
    upload_sessions,
)

from ._factories import CHECKSUM, seed_authority, seed_job, seed_tenant_graph, seed_worker

pytestmark = pytest.mark.postgres


def _insert_policy_version(connection) -> None:
    connection.execute(
        policy_versions.insert(),
        {
            "policy_version": 1,
            "global_outstanding_limit": 100_000,
            "operational_mode": "NORMAL",
            "is_current": True,
        },
    )


def _segment_values(*, allocation_id, tenant_id) -> dict[str, object]:
    return {
        "segment_id": new_uuid7(),
        "allocation_id": allocation_id,
        "tenant_id": tenant_id,
        "started_at": datetime.now(UTC),
        "dominant_share": Decimal("0.5"),
        "weight": Decimal("1"),
        "charged_amount": Decimal("0"),
        "policy_version": 1,
    }


def test_allocation_ledger_tenant_must_match_allocation_on_insert_and_update(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        tenant_a = seed_tenant_graph(connection, label="ledger-owner-a")
        tenant_b = seed_tenant_graph(connection, label="ledger-owner-b")
        job = seed_job(connection, tenant_a)
        worker = seed_worker(connection, label="ledger-owner")
        authority = seed_authority(connection, tenant_a, job, worker)
        _insert_policy_version(connection)

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(
            allocation_ledger_segments.insert(),
            _segment_values(
                allocation_id=authority["allocation_id"],
                tenant_id=tenant_b["tenant_id"],
            ),
        )

    with migrated_postgres_engine.begin() as connection:
        values = _segment_values(
            allocation_id=authority["allocation_id"], tenant_id=tenant_a["tenant_id"]
        )
        segment_id = values["segment_id"]
        connection.execute(allocation_ledger_segments.insert(), values)

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(
            allocation_ledger_segments.update()
            .where(allocation_ledger_segments.c.segment_id == segment_id)
            .values(tenant_id=tenant_b["tenant_id"])
        )


def test_job_event_requires_matching_tenant_while_global_event_remains_valid(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="event-owner")
        job = seed_job(connection, graph)
        connection.execute(
            events.insert(),
            {
                "event_id": new_uuid7(),
                "tenant_id": None,
                "job_id": None,
                "sequence": None,
                "event_type": "GLOBAL_MAINTENANCE",
                "actor_type": "SYSTEM",
                "actor_id": "coordinator",
                "safe_metadata": {},
            },
        )

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(
            events.insert(),
            {
                "event_id": new_uuid7(),
                "tenant_id": None,
                "job_id": job["job_id"],
                "sequence": 1,
                "event_type": "INVALID_OWNER",
                "actor_type": "SYSTEM",
                "actor_id": "coordinator",
                "safe_metadata": {},
            },
        )


def _insert_upload(connection, *, tenant_id):
    upload_id = new_uuid7()
    connection.execute(
        upload_sessions.insert(),
        {
            "upload_id": upload_id,
            "tenant_id": tenant_id,
            "expected_size_bytes": 4,
            "expected_checksum": CHECKSUM,
            "staged_key": f"staging/{upload_id}",
            "bytes_received": 0,
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
            "state": "ACTIVE",
        },
    )
    return upload_id


def _wait_for_lock_or_finish(engine, backend_pid: int, finished: threading.Event) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if finished.is_set():
            return False
        with engine.connect() as connection:
            waiting_on_lock = connection.execute(
                text(
                    "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :backend_pid"
                ),
                {"backend_pid": backend_pid},
            ).scalar_one_or_none()
        if waiting_on_lock:
            return True
        time.sleep(0.01)
    raise AssertionError("competing owner mutation neither blocked nor finished")


def test_referenced_upload_owner_cannot_move_tenant_or_be_deleted(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        tenant_a = seed_tenant_graph(connection, label="upload-owner-a")
        tenant_b = seed_tenant_graph(connection, label="upload-owner-b")
        upload_id = _insert_upload(connection, tenant_id=tenant_a["tenant_id"])
        connection.execute(
            artifact_references.insert(),
            {
                "tenant_id": tenant_a["tenant_id"],
                "artifact_id": tenant_a["artifact_id"],
                "owner_type": "UPLOAD_SESSION",
                "owner_id": upload_id,
                "purpose": "STAGED_SOURCE",
                "logical_name": "input.data",
            },
        )

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(
            upload_sessions.update()
            .where(upload_sessions.c.upload_id == upload_id)
            .values(tenant_id=tenant_b["tenant_id"])
        )

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(upload_sessions.delete().where(upload_sessions.c.upload_id == upload_id))


def test_unreferenced_upload_owner_can_be_deleted(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="upload-cleanup")
        upload_id = _insert_upload(connection, tenant_id=graph["tenant_id"])
        connection.execute(upload_sessions.delete().where(upload_sessions.c.upload_id == upload_id))


@pytest.mark.parametrize("mutation", ["delete", "move-tenant"])
def test_concurrent_upload_reference_serializes_with_owner_identity_mutation(
    migrated_postgres_engine,
    mutation: str,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        tenant_a = seed_tenant_graph(connection, label=f"upload-race-{mutation}-a")
        tenant_b = seed_tenant_graph(connection, label=f"upload-race-{mutation}-b")
        upload_id = _insert_upload(connection, tenant_id=tenant_a["tenant_id"])

    reference_connection = migrated_postgres_engine.connect()
    reference_transaction = reference_connection.begin()
    reference_connection.execute(
        artifact_references.insert(),
        {
            "tenant_id": tenant_a["tenant_id"],
            "artifact_id": tenant_a["artifact_id"],
            "owner_type": "UPLOAD_SESSION",
            "owner_id": upload_id,
            "purpose": "STAGED_SOURCE",
            "logical_name": "input.data",
        },
    )

    mutation_started = threading.Event()
    mutation_finished = threading.Event()
    mutation_error: list[BaseException] = []
    mutation_backend_pid: list[int] = []

    def mutate_owner() -> None:
        try:
            with migrated_postgres_engine.begin() as connection:
                connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                mutation_backend_pid.append(
                    connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                )
                mutation_started.set()
                if mutation == "delete":
                    connection.execute(
                        upload_sessions.delete().where(upload_sessions.c.upload_id == upload_id)
                    )
                else:
                    connection.execute(
                        upload_sessions.update()
                        .where(upload_sessions.c.upload_id == upload_id)
                        .values(tenant_id=tenant_b["tenant_id"])
                    )
        except BaseException as exc:
            mutation_error.append(exc)
        finally:
            mutation_finished.set()

    thread = threading.Thread(target=mutate_owner, daemon=True)
    thread.start()
    assert mutation_started.wait(timeout=5)
    mutation_blocked_on_reference = _wait_for_lock_or_finish(
        migrated_postgres_engine, mutation_backend_pid[0], mutation_finished
    )
    reference_transaction.commit()
    reference_connection.close()
    assert mutation_finished.wait(timeout=5)
    thread.join(timeout=1)

    assert mutation_blocked_on_reference
    assert len(mutation_error) == 1
    assert isinstance(mutation_error[0], IntegrityError)
    with migrated_postgres_engine.connect() as connection:
        assert (
            connection.execute(
                select(upload_sessions.c.tenant_id).where(upload_sessions.c.upload_id == upload_id)
            ).scalar_one()
            == tenant_a["tenant_id"]
        )
        assert (
            connection.execute(
                select(artifact_references.c.owner_id).where(
                    artifact_references.c.owner_type == "UPLOAD_SESSION",
                    artifact_references.c.owner_id == upload_id,
                )
            ).scalar_one()
            == upload_id
        )
