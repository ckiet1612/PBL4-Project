import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, text, update

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema import (
    allocations,
    policy_versions,
    worker_incarnations,
    workers,
)
from tests.api.test_http_contract import _client
from tests.integration._factories import seed_authority, seed_job, seed_tenant_graph

pytestmark = pytest.mark.postgres

WORKER_ID = "018f05c4-a922-7d0d-9f55-f9084a72d0f2"
INSTALLATION_ID = "018f05c4-a922-7d0d-9f55-f9084a72d0f1"
FINGERPRINT = "sha256:" + "a" * 64


def _bootstrap_worker(client) -> str:
    response = client.post(
        "/v1/internal/worker-bootstrap",
        headers={
            "X-Nexa-Bootstrap-Secret": "b" * 32,
            "Idempotency-Key": "b10-http-bootstrap-0001",
        },
        json={
            "installation_id": INSTALLATION_ID,
            "credential_public_fingerprint": FINGERPRINT,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["credential"]


def _inventory() -> dict:
    return {
        "architecture": "linux/amd64",
        "host_cpu_millis": 8_000,
        "host_memory_bytes": 16 * 1024**3,
        "allocatable": {
            "cpu_millis": 6_000,
            "memory_bytes": 12 * 1024**3,
            "gpu_count": 0,
        },
        "runtime": {
            "docker_version": "28.4.0",
            "oci_runtime": "runc",
            "oci_runtime_version": "1.3.0",
            "cgroups_version": 2,
            "kernel_release": "6.8.0",
            "seccomp_available": True,
        },
        "adapters": [{"adapter_id": "cpu.iterative", "adapter_version": "1.0.0"}],
        "images": [
            {
                "image_digest": "sha256:" + "c" * 64,
                "architecture": "linux/amd64",
                "verified": True,
            }
        ],
        "frameworks": [
            {
                "framework": "NEXA_CPU",
                "framework_version": "1.0.0",
                "device": "CPU",
                "cuda_runtime_version": None,
            }
        ],
        "gpu_devices": [],
        "discovered_at": "2026-09-21T00:00:00.000Z",
    }


def _create_incarnation(client, credential: str, *, nonce: str, key: str) -> dict:
    response = client.post(
        f"/v1/workers/{WORKER_ID}/incarnations",
        headers={"Authorization": f"Bearer {credential}", "Idempotency-Key": key},
        json={"process_start_nonce": nonce},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _wait_for_database_lock(engine) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
        if waiting:
            return
        time.sleep(0.02)
    raise AssertionError("worker operation did not reach the expected database lock")


def _assert_receipt_policy_worker_order(statements: list[str]) -> None:
    receipt = next(i for i, value in enumerate(statements) if "callback_receipts" in value)
    policy = next(i for i, value in enumerate(statements) if "policy_versions" in value)
    worker = next(i for i, value in enumerate(statements) if " workers" in value)
    assert receipt < policy < worker


def test_incarnation_replay_is_stable_and_does_not_reactivate_an_old_process(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        first_body = {"process_start_nonce": "018f05c4-a922-7d0d-9f55-f9084a72d101"}
        first_headers = {
            "Authorization": f"Bearer {credential}",
            "Idempotency-Key": "b10-incarnation-first-0001",
        }
        first = client.post(
            f"/v1/workers/{WORKER_ID}/incarnations",
            headers=first_headers,
            json=first_body,
        )
        same = client.post(
            f"/v1/workers/{WORKER_ID}/incarnations",
            headers=first_headers,
            json=first_body,
        )
        assert first.status_code == same.status_code == 201
        assert same.json() == first.json()

        second = _create_incarnation(
            client,
            credential,
            nonce="018f05c4-a922-7d0d-9f55-f9084a72d102",
            key="b10-incarnation-second-001",
        )
        old_replay = client.post(
            f"/v1/workers/{WORKER_ID}/incarnations",
            headers=first_headers,
            json=first_body,
        )
        assert old_replay.status_code == 201
        assert old_replay.json() == first.json()
        with migrated_postgres_engine.connect() as connection:
            current = connection.execute(
                select(workers.c.current_incarnation_id).where(workers.c.worker_id == WORKER_ID)
            ).scalar_one()
        assert str(current) == second["worker_incarnation_id"]
        assert second["sequence"] == first.json()["sequence"] + 1


def test_empty_reconciliation_heartbeat_ready_replay_and_poll_null(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client,
            credential,
            nonce="018f05c4-a922-7d0d-9f55-f9084a72d103",
            key="b10-incarnation-empty-0001",
        )
        worker_headers = {
            "Authorization": f"Bearer {credential}",
            "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"],
        }
        reconciliation = client.get(
            f"/v1/workers/{WORKER_ID}/reconciliation?page_size=100",
            headers=worker_headers,
        )
        assert reconciliation.status_code == 200, reconciliation.text
        assert reconciliation.json()["items"] == []
        assert reconciliation.json()["page"] == {"next_cursor": None, "page_size": 100}

        callback_id = "018f05c4-a922-7d0d-9f55-f9084a72d104"
        heartbeat_body = {
            "worker_incarnation_id": incarnation["worker_incarnation_id"],
            "observed_health": "READY",
            "reconcile_complete": True,
            "inventory": _inventory(),
            "observed_containers": [],
        }
        heartbeat = client.post(
            f"/v1/workers/{WORKER_ID}/heartbeat",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Callback-Id": callback_id,
            },
            json=heartbeat_body,
        )
        assert heartbeat.status_code == 200, heartbeat.text
        assert heartbeat.json()["accepted_incarnation_id"] == incarnation["worker_incarnation_id"]
        with migrated_postgres_engine.connect() as connection:
            committed = connection.execute(
                select(workers.c.health, workers.c.last_heartbeat_at).where(
                    workers.c.worker_id == WORKER_ID
                )
            ).one()
        assert committed.health == "READY"

        replay = client.post(
            f"/v1/workers/{WORKER_ID}/heartbeat",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Callback-Id": callback_id,
            },
            json=heartbeat_body,
        )
        assert replay.status_code == 200
        assert replay.json() == heartbeat.json()
        with migrated_postgres_engine.connect() as connection:
            replayed = connection.execute(
                select(workers.c.last_heartbeat_at).where(workers.c.worker_id == WORKER_ID)
            ).scalar_one()
        assert replayed == committed.last_heartbeat_at

        poll = client.post(
            f"/v1/workers/{WORKER_ID}/poll",
            headers={"Authorization": f"Bearer {credential}"},
            json={
                "worker_incarnation_id": incarnation["worker_incarnation_id"],
                "long_poll_seconds": 0,
            },
        )
        assert poll.status_code == 200, poll.text
        assert poll.json()["offer"] is None


def test_health_sweep_uses_database_age_without_a_new_worker_request(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client,
            credential,
            nonce="018f05c4-a922-7d0d-9f55-f9084a72d105",
            key="b10-incarnation-health-0001",
        )
        client.post(
            f"/v1/workers/{WORKER_ID}/heartbeat",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Callback-Id": "018f05c4-a922-7d0d-9f55-f9084a72d106",
            },
            json={
                "worker_incarnation_id": incarnation["worker_incarnation_id"],
                "observed_health": "STARTING",
                "reconcile_complete": False,
                "inventory": _inventory(),
                "observed_containers": [],
            },
        )
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                update(workers)
                .where(workers.c.worker_id == WORKER_ID)
                .values(last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=16))
            )
        client.app.state.services.worker.sweep_health()
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(workers.c.health).where(workers.c.worker_id == WORKER_ID)
                ).scalar_one()
                == "SUSPECT"
            )
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                update(workers)
                .where(workers.c.worker_id == WORKER_ID)
                .values(last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=31))
            )
        client.app.state.services.worker.sweep_health()
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(workers.c.health).where(workers.c.worker_id == WORKER_ID)
                ).scalar_one()
                == "UNAVAILABLE"
            )


def test_create_incarnation_marks_the_previous_incarnation_ended(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        first = _create_incarnation(
            client,
            credential,
            nonce="018f05c4-a922-7d0d-9f55-f9084a72d107",
            key="b10-incarnation-ended-0001",
        )
        _create_incarnation(
            client,
            credential,
            nonce="018f05c4-a922-7d0d-9f55-f9084a72d108",
            key="b10-incarnation-ended-0002",
        )
        with migrated_postgres_engine.connect() as connection:
            ended_at = connection.execute(
                select(worker_incarnations.c.ended_at).where(
                    worker_incarnations.c.worker_incarnation_id == first["worker_incarnation_id"]
                )
            ).scalar_one()
    assert ended_at is not None


def test_second_incarnation_can_commit_a_new_inventory_version(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        for index in range(2):
            incarnation = _create_incarnation(
                client,
                credential,
                nonce=f"018f05c4-a922-7d0d-9f55-f9084a72d{120 + index:03d}",
                key=f"b10-inventory-restart-{index:04d}",
            )
            reconciliation = client.get(
                f"/v1/workers/{WORKER_ID}/reconciliation",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"],
                },
            )
            assert reconciliation.status_code == 200
            heartbeat = client.post(
                f"/v1/workers/{WORKER_ID}/heartbeat",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "X-Callback-Id": f"018f05c4-a922-7d0d-9f55-f9084a72d{122 + index:03d}",
                },
                json={
                    "worker_incarnation_id": incarnation["worker_incarnation_id"],
                    "observed_health": "READY",
                    "reconcile_complete": True,
                    "inventory": _inventory(),
                    "observed_containers": [],
                },
            )
            assert heartbeat.status_code == 200, heartbeat.text
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(workers.c.current_inventory_version).where(
                        workers.c.worker_id == WORKER_ID
                    )
                ).scalar_one()
                == 2
            )


def test_heartbeat_cannot_self_attest_reconciliation_without_a_server_page(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client,
            credential,
            nonce="018f05c4-a922-7d0d-9f55-f9084a72d109",
            key="b10-incarnation-no-page-0001",
        )
        response = client.post(
            f"/v1/workers/{WORKER_ID}/heartbeat",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Callback-Id": "018f05c4-a922-7d0d-9f55-f9084a72d110",
            },
            json={
                "worker_incarnation_id": incarnation["worker_incarnation_id"],
                "observed_health": "READY",
                "reconcile_complete": True,
                "inventory": _inventory(),
                "observed_containers": [],
            },
        )
        assert response.status_code == 200, response.text
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(workers.c.health).where(workers.c.worker_id == WORKER_ID)
                ).scalar_one()
                == "STARTING"
            )


def test_reconciliation_drains_101_rows_and_rejects_a_stale_cursor(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client,
            credential,
            nonce="018f05c4-a922-7d0d-9f55-f9084a72d111",
            key="b10-incarnation-many-0001",
        )
        with migrated_postgres_engine.begin() as connection:
            graph = seed_tenant_graph(connection, label="many-b10")
            worker = {
                "worker_id": WORKER_ID,
                "incarnation_id": incarnation["worker_incarnation_id"],
            }
            for _ in range(101):
                job = seed_job(connection, graph, state="RUNNING")
                seed_authority(connection, graph, job, worker)
        headers = {
            "Authorization": f"Bearer {credential}",
            "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"],
        }
        first = client.get(f"/v1/workers/{WORKER_ID}/reconciliation?page_size=100", headers=headers)
        assert first.status_code == 200, first.text
        assert len(first.json()["items"]) == 100
        cursor = first.json()["page"]["next_cursor"]
        assert cursor is not None
        second = client.get(
            f"/v1/workers/{WORKER_ID}/reconciliation?page_size=100&cursor={cursor}",
            headers=headers,
        )
        assert second.status_code == 200, second.text
        assert len(second.json()["items"]) == 1
        assert second.json()["page"]["next_cursor"] is None
        replay_old_page = client.get(
            f"/v1/workers/{WORKER_ID}/reconciliation?page_size=100&cursor={cursor}",
            headers=headers,
        )
        assert replay_old_page.status_code == 409


def test_poll_requires_ready_enabled_worker(migrated_postgres_engine, tmp_path) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client,
            credential,
            nonce="018f05c4-a922-7d0d-9f55-f9084a72d130",
            key="b10-poll-state-0001",
        )
        poll_body = {
            "worker_incarnation_id": incarnation["worker_incarnation_id"],
            "long_poll_seconds": 0,
        }
        headers = {"Authorization": f"Bearer {credential}"}
        assert (
            client.post(
                f"/v1/workers/{WORKER_ID}/poll", headers=headers, json=poll_body
            ).status_code
            == 409
        )

        reconcile_headers = {
            **headers,
            "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"],
        }
        assert (
            client.get(
                f"/v1/workers/{WORKER_ID}/reconciliation", headers=reconcile_headers
            ).status_code
            == 200
        )
        heartbeat = client.post(
            f"/v1/workers/{WORKER_ID}/heartbeat",
            headers={**headers, "X-Callback-Id": "018f05c4-a922-7d0d-9f55-f9084a72d131"},
            json={
                "worker_incarnation_id": incarnation["worker_incarnation_id"],
                "observed_health": "READY",
                "reconcile_complete": True,
                "inventory": _inventory(),
                "observed_containers": [],
            },
        )
        assert heartbeat.status_code == 200
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                update(workers)
                .where(workers.c.worker_id == WORKER_ID)
                .values(admin_state="DISABLED")
            )
        assert (
            client.post(
                f"/v1/workers/{WORKER_ID}/poll", headers=headers, json=poll_body
            ).status_code
            == 409
        )


def test_health_sweep_does_not_mutate_worker_while_writes_are_frozen(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        _create_incarnation(
            client,
            credential,
            nonce="018f05c4-a922-7d0d-9f55-f9084a72d132",
            key="b10-frozen-sweep-0001",
        )
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                update(workers)
                .where(workers.c.worker_id == WORKER_ID)
                .values(
                    health="READY",
                    last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=31),
                )
            )
            connection.execute(
                update(policy_versions)
                .where(policy_versions.c.is_current.is_(True))
                .values(operational_mode="WRITE_FROZEN")
            )
        assert client.app.state.services.worker.sweep_health() == 0
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(workers.c.health).where(workers.c.worker_id == WORKER_ID)
                ).scalar_one()
                == "READY"
            )


def test_allocation_change_after_ready_can_start_a_new_bounded_reconciliation(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client,
            credential,
            nonce="018f05c4-a922-7d0d-9f55-f9084a72d133",
            key="b10-rereconcile-0001",
        )
        auth_headers = {"Authorization": f"Bearer {credential}"}
        reconcile_headers = {
            **auth_headers,
            "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"],
        }
        assert (
            client.get(
                f"/v1/workers/{WORKER_ID}/reconciliation", headers=reconcile_headers
            ).status_code
            == 200
        )
        first_ready = client.post(
            f"/v1/workers/{WORKER_ID}/heartbeat",
            headers={
                **auth_headers,
                "X-Callback-Id": "018f05c4-a922-7d0d-9f55-f9084a72d134",
            },
            json={
                "worker_incarnation_id": incarnation["worker_incarnation_id"],
                "observed_health": "READY",
                "reconcile_complete": True,
                "inventory": _inventory(),
                "observed_containers": [],
            },
        )
        assert first_ready.status_code == 200
        with migrated_postgres_engine.begin() as connection:
            graph = seed_tenant_graph(connection, label="rereconcile-b10")
            job = seed_job(connection, graph, state="RUNNING")
            authority = seed_authority(
                connection,
                graph,
                job,
                {
                    "worker_id": WORKER_ID,
                    "incarnation_id": incarnation["worker_incarnation_id"],
                },
            )
        changed = client.post(
            f"/v1/workers/{WORKER_ID}/heartbeat",
            headers={
                **auth_headers,
                "X-Callback-Id": "018f05c4-a922-7d0d-9f55-f9084a72d135",
            },
            json={
                "worker_incarnation_id": incarnation["worker_incarnation_id"],
                "observed_health": "READY",
                "reconcile_complete": True,
                "inventory": _inventory(),
                "observed_containers": [],
            },
        )
        assert changed.status_code == 200
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(workers.c.health).where(workers.c.worker_id == WORKER_ID)
                ).scalar_one()
                == "STARTING"
            )

        restarted = client.get(f"/v1/workers/{WORKER_ID}/reconciliation", headers=reconcile_headers)
        assert restarted.status_code == 200, restarted.text
        assert len(restarted.json()["items"]) == 1

        with migrated_postgres_engine.begin() as connection:
            now = datetime.now(UTC)
            connection.execute(
                update(allocations)
                .where(allocations.c.allocation_id == authority["allocation_id"])
                .values(state="RELEASED", released_at=now, release_reason="TEST")
            )
        released = client.get(f"/v1/workers/{WORKER_ID}/reconciliation", headers=reconcile_headers)
        assert released.status_code == 200, released.text
        assert released.json()["items"] == []


@pytest.mark.parametrize(
    "mutate_inventory",
    [
        lambda inventory: inventory["runtime"].update(seccomp_available=False),
        lambda inventory: inventory["allocatable"].update(
            cpu_millis=inventory["host_cpu_millis"],
            memory_bytes=inventory["host_memory_bytes"],
        ),
    ],
    ids=["seccomp-missing", "host-reserve-missing"],
)
def test_invalid_runtime_or_reserve_cannot_make_worker_ready(
    migrated_postgres_engine, tmp_path, mutate_inventory
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client,
            credential,
            nonce=str(new_uuid7()),
            key=f"b10-invalid-ready-{new_uuid7()}",
        )
        headers = {"Authorization": f"Bearer {credential}"}
        assert (
            client.get(
                f"/v1/workers/{WORKER_ID}/reconciliation",
                headers={
                    **headers,
                    "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"],
                },
            ).status_code
            == 200
        )
        inventory = _inventory()
        mutate_inventory(inventory)
        heartbeat = client.post(
            f"/v1/workers/{WORKER_ID}/heartbeat",
            headers={**headers, "X-Callback-Id": "018f05c4-a922-7d0d-9f55-f9084a72d136"},
            json={
                "worker_incarnation_id": incarnation["worker_incarnation_id"],
                "observed_health": "READY",
                "reconcile_complete": True,
                "inventory": inventory,
                "observed_containers": [],
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(workers.c.health).where(workers.c.worker_id == WORKER_ID)
                ).scalar_one()
                == "STARTING"
            )


def test_failed_artifact_storage_probe_blocks_ready(migrated_postgres_engine, tmp_path) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client,
            credential,
            nonce="018f05c4-a922-7d0d-9f55-f9084a72d137",
            key="b10-storage-ready-0001",
        )
        headers = {"Authorization": f"Bearer {credential}"}
        assert (
            client.get(
                f"/v1/workers/{WORKER_ID}/reconciliation",
                headers={
                    **headers,
                    "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"],
                },
            ).status_code
            == 200
        )
        storage_root = client.app.state.services.settings.artifact_root
        shutil.rmtree(storage_root)
        storage_root.write_text("storage unavailable", encoding="ascii")
        heartbeat = client.post(
            f"/v1/workers/{WORKER_ID}/heartbeat",
            headers={**headers, "X-Callback-Id": "018f05c4-a922-7d0d-9f55-f9084a72d138"},
            json={
                "worker_incarnation_id": incarnation["worker_incarnation_id"],
                "observed_health": "READY",
                "reconcile_complete": True,
                "inventory": _inventory(),
                "observed_containers": [],
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(workers.c.health).where(workers.c.worker_id == WORKER_ID)
                ).scalar_one()
                == "STARTING"
            )


def test_health_sweep_reads_database_time_after_waiting_for_worker_lock(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        _create_incarnation(
            client,
            credential,
            nonce="018f05c4-a922-7d0d-9f55-f9084a72d139",
            key="b10-health-lock-time-0001",
        )
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                update(workers)
                .where(workers.c.worker_id == WORKER_ID)
                .values(
                    health="READY",
                    last_heartbeat_at=text("clock_timestamp() - interval '14 seconds'"),
                )
            )
        blocker = migrated_postgres_engine.connect()
        transaction = blocker.begin()
        blocker.execute(
            select(workers.c.worker_id).where(workers.c.worker_id == WORKER_ID).with_for_update()
        ).scalar_one()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(client.app.state.services.worker.sweep_health)
                _wait_for_database_lock(migrated_postgres_engine)
                time.sleep(1.2)
                transaction.commit()
                assert future.result(timeout=5) == 1
        finally:
            if transaction.is_active:
                transaction.rollback()
            blocker.close()
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(workers.c.health).where(workers.c.worker_id == WORKER_ID)
                ).scalar_one()
                == "SUSPECT"
            )


def test_heartbeat_callback_receipt_lock_precedes_policy_and_worker(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client,
            credential,
            nonce="018f05c4-a922-7d0d-9f55-f9084a72d140",
            key="b10-heartbeat-lock-order-0001",
        )
        assert (
            client.get(
                f"/v1/workers/{WORKER_ID}/reconciliation",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"],
                },
            ).status_code
            == 200
        )
        statements: list[str] = []

        def record(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
            statements.append(statement.lower())

        event.listen(migrated_postgres_engine, "before_cursor_execute", record)
        try:
            heartbeat = client.post(
                f"/v1/workers/{WORKER_ID}/heartbeat",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "X-Callback-Id": "018f05c4-a922-7d0d-9f55-f9084a72d141",
                },
                json={
                    "worker_incarnation_id": incarnation["worker_incarnation_id"],
                    "observed_health": "STARTING",
                    "reconcile_complete": False,
                    "inventory": _inventory(),
                    "observed_containers": [],
                },
            )
            assert heartbeat.status_code == 200, heartbeat.text
            _assert_receipt_policy_worker_order(statements)
        finally:
            event.remove(migrated_postgres_engine, "before_cursor_execute", record)
