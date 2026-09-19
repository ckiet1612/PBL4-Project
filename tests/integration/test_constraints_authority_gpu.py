import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema import (
    allocation_gpu_claims,
    allocations,
    attempt_authority_grants,
    attempt_leases,
    attempts,
    jobs,
    worker_incarnations,
    workers,
)

from ._factories import seed_authority, seed_job, seed_tenant_graph, seed_worker

pytestmark = pytest.mark.postgres


def _insert_attempt(connection, graph, job, worker, *, attempt_number: int):
    attempt_id = new_uuid7()
    connection.execute(jobs.update().where(jobs.c.job_id == job["job_id"]).values(job_fence=1))
    connection.execute(
        attempts.insert(),
        {
            "attempt_id": attempt_id,
            "tenant_id": graph["tenant_id"],
            "job_id": job["job_id"],
            "attempt_number": attempt_number,
            "state": "CREATED",
            "execution_intent": "RUN",
            "worker_id": worker["worker_id"],
            "worker_incarnation_id": worker["incarnation_id"],
            "job_fence": 1,
            "startup_nonce": new_uuid7(),
            "progress_sequence": 0,
        },
    )
    return attempt_id


def test_created_attempt_grant_already_consumes_current_job_authority(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="authority")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label="authority")
        authority = seed_authority(connection, graph, job, worker)
        connection.execute(
            attempt_authority_grants.update()
            .where(attempt_authority_grants.c.grant_id == authority["grant_id"])
            .values(ended_at=datetime.now(UTC))
        )
        incarnation_two = new_uuid7()
        incarnation_three = new_uuid7()
        for sequence, incarnation_id in ((2, incarnation_two), (3, incarnation_three)):
            connection.execute(
                worker_incarnations.insert(),
                {
                    "worker_incarnation_id": incarnation_id,
                    "worker_id": worker["worker_id"],
                    "sequence": sequence,
                    "process_start_nonce": new_uuid7(),
                    "process_started_at": datetime.now(UTC),
                    "ended_at": datetime.now(UTC),
                },
            )
        connection.execute(
            attempt_authority_grants.insert(),
            {
                "grant_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "allocation_id": authority["allocation_id"],
                "lease_id": authority["lease_id"],
                "worker_id": worker["worker_id"],
                "worker_incarnation_id": incarnation_two,
                "job_fence": 1,
                "predecessor_grant_id": authority["grant_id"],
                "callback_id": new_uuid7(),
            },
        )

    with (
        pytest.raises(IntegrityError, match="uq_attempt_authority_grants_current_job"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            attempt_authority_grants.insert(),
            {
                "grant_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "allocation_id": authority["allocation_id"],
                "lease_id": authority["lease_id"],
                "worker_id": worker["worker_id"],
                "worker_incarnation_id": incarnation_three,
                "job_fence": 1,
                "predecessor_grant_id": authority["grant_id"],
                "callback_id": new_uuid7(),
            },
        )


def test_attempt_lease_insert_cannot_change_worker_from_allocation(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="lease-worker-insert")
        job = seed_job(connection, graph)
        worker_a = seed_worker(connection, label="lease-worker-insert-a")
        worker_b = seed_worker(connection, label="lease-worker-insert-b")
        authority = seed_authority(connection, graph, job, worker_a)
        connection.execute(
            attempt_leases.update()
            .where(attempt_leases.c.lease_id == authority["lease_id"])
            .values(revoked_at=datetime.now(UTC), revoke_reason="ADOPTED")
        )

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(
            attempt_leases.insert(),
            {
                "lease_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "allocation_id": authority["allocation_id"],
                "worker_id": worker_b["worker_id"],
                "current_worker_incarnation_id": worker_b["incarnation_id"],
                "job_fence": 1,
                "expires_at": datetime.now(UTC) + timedelta(seconds=45),
            },
        )


def test_attempt_lease_update_cannot_change_worker_from_allocation(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="lease-worker-update")
        job = seed_job(connection, graph)
        worker_a = seed_worker(connection, label="lease-worker-update-a")
        worker_b = seed_worker(connection, label="lease-worker-update-b")
        authority = seed_authority(connection, graph, job, worker_a)

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(
            attempt_leases.update()
            .where(attempt_leases.c.lease_id == authority["lease_id"])
            .values(
                worker_id=worker_b["worker_id"],
                current_worker_incarnation_id=worker_b["incarnation_id"],
            )
        )


def test_authority_adoption_cannot_reuse_an_attempt_incarnation(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="authority-incarnation")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label="authority-incarnation")
        authority = seed_authority(connection, graph, job, worker)
        now = datetime.now(UTC)
        connection.execute(
            attempt_authority_grants.update()
            .where(attempt_authority_grants.c.grant_id == authority["grant_id"])
            .values(ended_at=now)
        )
        connection.execute(
            worker_incarnations.update()
            .where(worker_incarnations.c.worker_incarnation_id == worker["incarnation_id"])
            .values(ended_at=now)
        )
        incarnation_b = new_uuid7()
        connection.execute(
            worker_incarnations.insert(),
            {
                "worker_incarnation_id": incarnation_b,
                "worker_id": worker["worker_id"],
                "sequence": 2,
                "process_start_nonce": new_uuid7(),
                "process_started_at": now,
                "reconcile_completed_at": now,
                "ready_at": now,
            },
        )
        connection.execute(
            workers.update()
            .where(workers.c.worker_id == worker["worker_id"])
            .values(current_incarnation_id=incarnation_b)
        )
        grant_b = new_uuid7()
        connection.execute(
            attempt_authority_grants.insert(),
            {
                "grant_id": grant_b,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "allocation_id": authority["allocation_id"],
                "lease_id": authority["lease_id"],
                "worker_id": worker["worker_id"],
                "worker_incarnation_id": incarnation_b,
                "job_fence": 1,
                "predecessor_grant_id": authority["grant_id"],
                "callback_id": new_uuid7(),
            },
        )
        connection.execute(
            attempt_authority_grants.update()
            .where(attempt_authority_grants.c.grant_id == grant_b)
            .values(ended_at=datetime.now(UTC))
        )

    with (
        pytest.raises(IntegrityError, match="uq_authority_grants_attempt_incarnation"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            attempt_authority_grants.insert(),
            {
                "grant_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "allocation_id": authority["allocation_id"],
                "lease_id": authority["lease_id"],
                "worker_id": worker["worker_id"],
                "worker_incarnation_id": worker["incarnation_id"],
                "job_fence": 1,
                "predecessor_grant_id": grant_b,
                "callback_id": new_uuid7(),
            },
        )


def test_two_transactions_cannot_claim_same_physical_gpu(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="gpu-race")
        worker = seed_worker(connection, label="gpu-race")
        job_one = seed_job(connection, graph)
        job_two = seed_job(connection, graph)
        attempt_one = _insert_attempt(connection, graph, job_one, worker, attempt_number=1)
        attempt_two = _insert_attempt(connection, graph, job_two, worker, attempt_number=1)

    allocation_one = new_uuid7()
    allocation_two = new_uuid7()
    second_started = threading.Event()
    second_finished = threading.Event()
    second_error: list[BaseException] = []

    connection_one = migrated_postgres_engine.connect()
    transaction_one = connection_one.begin()
    connection_one.execute(
        allocations.insert(),
        {
            "allocation_id": allocation_one,
            "tenant_id": graph["tenant_id"],
            "job_id": job_one["job_id"],
            "attempt_id": attempt_one,
            "worker_id": worker["worker_id"],
            "cpu_millis": 1000,
            "memory_bytes": 1024,
            "gpu_count": 1,
            "state": "HELD",
        },
    )
    connection_one.execute(
        allocation_gpu_claims.insert(),
        {
            "allocation_id": allocation_one,
            "worker_id": worker["worker_id"],
            "inventory_version": 1,
            "gpu_uuid": worker["gpu_uuid"],
        },
    )

    def competing_claim() -> None:
        try:
            with migrated_postgres_engine.begin() as connection:
                connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                connection.execute(
                    allocations.insert(),
                    {
                        "allocation_id": allocation_two,
                        "tenant_id": graph["tenant_id"],
                        "job_id": job_two["job_id"],
                        "attempt_id": attempt_two,
                        "worker_id": worker["worker_id"],
                        "cpu_millis": 1000,
                        "memory_bytes": 1024,
                        "gpu_count": 1,
                        "state": "HELD",
                    },
                )
                second_started.set()
                connection.execute(
                    allocation_gpu_claims.insert(),
                    {
                        "allocation_id": allocation_two,
                        "worker_id": worker["worker_id"],
                        "inventory_version": 1,
                        "gpu_uuid": worker["gpu_uuid"],
                    },
                )
        except BaseException as exc:
            second_error.append(exc)
        finally:
            second_finished.set()

    thread = threading.Thread(target=competing_claim, daemon=True)
    thread.start()
    assert second_started.wait(timeout=5)
    transaction_one.commit()
    connection_one.close()
    assert second_finished.wait(timeout=5)
    thread.join(timeout=1)

    assert len(second_error) == 1
    assert isinstance(second_error[0], IntegrityError)
    with migrated_postgres_engine.connect() as connection:
        active = connection.execute(
            allocation_gpu_claims.select().where(allocation_gpu_claims.c.released_at.is_(None))
        ).all()
    assert [(row.allocation_id, row.gpu_uuid) for row in active] == [
        (allocation_one, worker["gpu_uuid"])
    ]


def test_quarantine_and_inventory_version_do_not_release_gpu_claim(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="gpu-hold")
        worker = seed_worker(connection, label="gpu-hold", gpu_versions=2)
        job_one = seed_job(connection, graph)
        held = seed_authority(connection, graph, job_one, worker, gpu_count=1, inventory_version=1)
        connection.execute(
            allocations.update()
            .where(allocations.c.allocation_id == held["allocation_id"])
            .values(state="QUARANTINED", quarantined_at=datetime.now(UTC))
        )
        job_two = seed_job(connection, graph)
        attempt_two = _insert_attempt(connection, graph, job_two, worker, attempt_number=1)

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        allocation_two = new_uuid7()
        connection.execute(
            allocations.insert(),
            {
                "allocation_id": allocation_two,
                "tenant_id": graph["tenant_id"],
                "job_id": job_two["job_id"],
                "attempt_id": attempt_two,
                "worker_id": worker["worker_id"],
                "cpu_millis": 1000,
                "memory_bytes": 1024,
                "gpu_count": 1,
                "state": "HELD",
            },
        )
        connection.execute(
            allocation_gpu_claims.insert(),
            {
                "allocation_id": allocation_two,
                "worker_id": worker["worker_id"],
                "inventory_version": 2,
                "gpu_uuid": worker["gpu_uuid"],
            },
        )


def test_allocation_and_gpu_claim_release_must_commit_together(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="gpu-release")
        worker = seed_worker(connection, label="gpu-release")
        job = seed_job(connection, graph)
        authority = seed_authority(connection, graph, job, worker, gpu_count=1)

    with (
        pytest.raises(IntegrityError, match="cannot retain active GPU claims"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            allocations.update()
            .where(allocations.c.allocation_id == authority["allocation_id"])
            .values(
                state="RELEASED",
                released_at=datetime.now(UTC),
                release_reason="CLEANUP_VERIFIED",
            )
        )

    released_at = datetime.now(UTC)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            allocations.update()
            .where(allocations.c.allocation_id == authority["allocation_id"])
            .values(state="RELEASED", released_at=released_at, release_reason="CLEANUP_VERIFIED")
        )
        connection.execute(
            allocation_gpu_claims.update()
            .where(allocation_gpu_claims.c.allocation_id == authority["allocation_id"])
            .values(released_at=released_at)
        )


def test_active_gpu_claim_cannot_be_deleted_from_unreleased_allocation(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="gpu-delete")
        worker = seed_worker(connection, label="gpu-delete")
        job = seed_job(connection, graph)
        authority = seed_authority(connection, graph, job, worker, gpu_count=1)

    with (
        pytest.raises(IntegrityError, match="GPU claim count"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            allocation_gpu_claims.delete().where(
                allocation_gpu_claims.c.allocation_id == authority["allocation_id"]
            )
        )
