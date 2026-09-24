import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import event, select, text, update

from nexa.api.schemas import AdoptRequest
from nexa.application.errors import ApplicationError
from nexa.application.json_codec import jcs_request_hash
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema import (
    attempt_authority_grants,
    attempt_leases,
    attempts,
    checkpoint_reservations,
    container_identities,
    jobs,
    result_reservations,
)
from tests.api.test_http_contract import _client
from tests.integration._factories import seed_authority, seed_job, seed_tenant_graph
from tests.integration.test_worker_api_b10 import WORKER_ID, _bootstrap_worker, _create_incarnation

pytestmark = pytest.mark.postgres
CONTAINER_ID = "a" * 64
DIGEST = "sha256:" + "b" * 64


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


def _assert_policy_receipt_worker_order(statements: list[str]) -> None:
    receipt = next(i for i, value in enumerate(statements) if "callback_receipts" in value)
    policy = next(i for i, value in enumerate(statements) if "policy_versions" in value)
    worker = next(i for i, value in enumerate(statements) if " workers" in value)
    assert policy < receipt < worker


def _running_authority(engine, incarnation: dict, *, reservations: bool = False) -> dict:
    with engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="b10-authority")
        job = seed_job(connection, graph, state="RUNNING")
        worker = {
            "worker_id": UUID(WORKER_ID),
            "incarnation_id": UUID(incarnation["worker_incarnation_id"]),
        }
        authority = seed_authority(connection, graph, job, worker)
        connection.execute(
            update(attempts)
            .where(attempts.c.attempt_id == authority["attempt_id"])
            .values(state="RUNNING", started_at=datetime.now(UTC))
        )
        startup_nonce = connection.execute(
            select(attempts.c.startup_nonce).where(attempts.c.attempt_id == authority["attempt_id"])
        ).scalar_one()
        connection.execute(
            container_identities.insert().values(
                tenant_id=graph["tenant_id"],
                job_id=job["job_id"],
                attempt_id=authority["attempt_id"],
                allocation_id=authority["allocation_id"],
                startup_nonce=startup_nonce,
                executor_create_sequence=1,
                container_id=CONTAINER_ID,
                runtime_identity_digest=DIGEST,
                created_at=datetime.now(UTC),
            )
        )
        if reservations:
            connection.execute(
                checkpoint_reservations.insert().values(
                    checkpoint_id=new_uuid7(),
                    tenant_id=graph["tenant_id"],
                    job_id=job["job_id"],
                    attempt_id=authority["attempt_id"],
                    authority_grant_id=authority["grant_id"],
                    sequence=1,
                    callback_id=new_uuid7(),
                    state="RESERVED",
                )
            )
            connection.execute(
                result_reservations.insert().values(
                    result_id=new_uuid7(),
                    tenant_id=graph["tenant_id"],
                    job_id=job["job_id"],
                    attempt_id=authority["attempt_id"],
                    authority_grant_id=authority["grant_id"],
                    callback_id=new_uuid7(),
                    state="ACTIVE",
                )
            )
    return {
        "worker_id": WORKER_ID,
        "worker_incarnation_id": incarnation["worker_incarnation_id"],
        "attempt_id": str(authority["attempt_id"]),
        "allocation_id": str(authority["allocation_id"]),
        "lease_id": str(authority["lease_id"]),
        "job_fence": 1,
        "grant_id": authority["grant_id"],
        "job_id": job["job_id"],
    }


def test_adopt_transfers_authority_reservations_and_replays_without_extending_lease(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        first = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b10-old-process-0001"
        )
        prior = _running_authority(migrated_postgres_engine, first, reservations=True)
        current = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b10-new-process-0001"
        )
        headers = {"Authorization": f"Bearer {credential}", "X-Callback-Id": str(new_uuid7())}
        body = {
            "prior_authority": {
                k: prior[k]
                for k in (
                    "worker_id",
                    "worker_incarnation_id",
                    "attempt_id",
                    "allocation_id",
                    "lease_id",
                    "job_fence",
                )
            },
            "current_worker_incarnation_id": current["worker_incarnation_id"],
            "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
        }
        path = f"/v1/attempts/{prior['attempt_id']}/adopt"
        adopted = client.post(path, headers=headers, json=body)
        assert adopted.status_code == 200, adopted.text
        assert (
            adopted.json()["authority"]["worker_incarnation_id"] == current["worker_incarnation_id"]
        )
        assert adopted.json()["transferred_checkpoint_reservation"] is not None
        assert adopted.json()["transferred_result_reservation"] is not None
        replay = client.post(path, headers=headers, json=body)
        assert replay.status_code == 200 and replay.json() == adopted.json()
        _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b10-newer-process-0001"
        )
        stale_replay = client.post(path, headers=headers, json=body)
        assert stale_replay.status_code == 409
        with migrated_postgres_engine.connect() as connection:
            grants = (
                connection.execute(
                    select(attempt_authority_grants).where(
                        attempt_authority_grants.c.attempt_id == UUID(prior["attempt_id"])
                    )
                )
                .mappings()
                .all()
            )
            assert len(grants) == 2 and sum(g["ended_at"] is None for g in grants) == 1
            new_grant = next(g for g in grants if g["ended_at"] is None)
            assert (
                connection.execute(
                    select(checkpoint_reservations.c.authority_grant_id)
                ).scalar_one()
                == new_grant["grant_id"]
            )
            assert (
                connection.execute(select(result_reservations.c.authority_grant_id)).scalar_one()
                == new_grant["grant_id"]
            )


def test_renew_in_starting_preserves_progress_and_rejects_conflicting_sequence(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        first = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b10-renew-process-0001"
        )
        authority = _running_authority(migrated_postgres_engine, first)
        headers = {"Authorization": f"Bearer {credential}", "X-Callback-Id": str(new_uuid7())}
        path = f"/v1/attempts/{authority['attempt_id']}/renew"
        body = {
            "authority": {
                k: authority[k]
                for k in (
                    "worker_id",
                    "worker_incarnation_id",
                    "attempt_id",
                    "allocation_id",
                    "lease_id",
                    "job_fence",
                )
            },
            "progress_sequence": 0,
            "progress": None,
        }
        first_response = client.post(path, headers=headers, json=body)
        assert first_response.status_code == 200, first_response.text
        assert client.post(path, headers=headers, json=body).json() == first_response.json()
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(attempts.c.progress_sequence).where(
                        attempts.c.attempt_id == UUID(authority["attempt_id"])
                    )
                ).scalar_one()
                == 0
            )
        snapshot = {"fraction": 0.25, "step": 2, "epoch": None, "item_cursor": None}
        body.update(progress_sequence=1, progress=snapshot)
        headers["X-Callback-Id"] = str(new_uuid7())
        assert client.post(path, headers=headers, json=body).status_code == 200
        headers["X-Callback-Id"] = str(new_uuid7())
        body["progress"] = {**snapshot, "step": 3}
        assert client.post(path, headers=headers, json=body).status_code == 409
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                update(attempt_leases)
                .where(attempt_leases.c.lease_id == UUID(authority["lease_id"]))
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        headers["X-Callback-Id"] = str(new_uuid7())
        assert client.post(path, headers=headers, json=body).status_code == 409


def test_adopt_and_renew_reject_a_job_fence_that_advanced_without_mutation(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        first = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b10-stale-fence-old-0001"
        )
        prior = _running_authority(migrated_postgres_engine, first, reservations=True)
        current = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b10-stale-fence-new-0001"
        )
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                update(jobs).where(jobs.c.job_id == prior["job_id"]).values(job_fence=2)
            )
            original_expiry = connection.execute(
                select(attempt_leases.c.expires_at).where(
                    attempt_leases.c.lease_id == UUID(prior["lease_id"])
                )
            ).scalar_one()

        authority = {
            key: prior[key]
            for key in (
                "worker_id",
                "worker_incarnation_id",
                "attempt_id",
                "allocation_id",
                "lease_id",
                "job_fence",
            )
        }
        adopt = client.post(
            f"/v1/attempts/{prior['attempt_id']}/adopt",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Callback-Id": str(new_uuid7()),
            },
            json={
                "prior_authority": authority,
                "current_worker_incarnation_id": current["worker_incarnation_id"],
                "container": {
                    "container_id": CONTAINER_ID,
                    "runtime_identity_digest": DIGEST,
                },
            },
        )
        assert adopt.status_code == 409

        renew = client.post(
            f"/v1/attempts/{prior['attempt_id']}/renew",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Callback-Id": str(new_uuid7()),
            },
            json={
                "authority": authority,
                "progress_sequence": 0,
                "progress": None,
            },
        )
        assert renew.status_code == 409

        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(attempt_leases.c.expires_at).where(
                        attempt_leases.c.lease_id == UUID(prior["lease_id"])
                    )
                ).scalar_one()
                == original_expiry
            )
            assert connection.execute(
                select(attempt_authority_grants.c.grant_id).where(
                    attempt_authority_grants.c.attempt_id == UUID(prior["attempt_id"])
                )
            ).scalars().all() == [prior["grant_id"]]


def test_checkpoint_for_pause_authority_can_be_adopted_and_renewed(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        first = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b10-pause-old-0001"
        )
        prior = _running_authority(migrated_postgres_engine, first)
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.job_id == prior["job_id"])
                .values(state="PAUSING", desired_state="PAUSED")
            )
            connection.execute(
                update(attempts)
                .where(attempts.c.attempt_id == UUID(prior["attempt_id"]))
                .values(execution_intent="CHECKPOINT_FOR_PAUSE")
            )
        current = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b10-pause-new-0001"
        )
        authority = {
            key: prior[key]
            for key in (
                "worker_id",
                "worker_incarnation_id",
                "attempt_id",
                "allocation_id",
                "lease_id",
                "job_fence",
            )
        }
        adopted = client.post(
            f"/v1/attempts/{prior['attempt_id']}/adopt",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Callback-Id": str(new_uuid7()),
            },
            json={
                "prior_authority": authority,
                "current_worker_incarnation_id": current["worker_incarnation_id"],
                "container": {
                    "container_id": CONTAINER_ID,
                    "runtime_identity_digest": DIGEST,
                },
            },
        )
        assert adopted.status_code == 200, adopted.text
        renewed_authority = adopted.json()["authority"]
        renewed = client.post(
            f"/v1/attempts/{prior['attempt_id']}/renew",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Callback-Id": str(new_uuid7()),
            },
            json={
                "authority": renewed_authority,
                "progress_sequence": 0,
                "progress": None,
            },
        )
        assert renewed.status_code == 200, renewed.text
        assert renewed.json()["desired_state"] == "PAUSED"


def test_adoption_rechecks_database_time_after_waiting_for_reservation_lock(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        first = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b10-expiry-old-0001"
        )
        prior = _running_authority(migrated_postgres_engine, first, reservations=True)
        current = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b10-expiry-new-0001"
        )
        body = {
            "prior_authority": {
                key: prior[key]
                for key in (
                    "worker_id",
                    "worker_incarnation_id",
                    "attempt_id",
                    "allocation_id",
                    "lease_id",
                    "job_fence",
                )
            },
            "current_worker_incarnation_id": current["worker_incarnation_id"],
            "container": {
                "container_id": CONTAINER_ID,
                "runtime_identity_digest": DIGEST,
            },
        }
        request = AdoptRequest.model_validate(body)
        callback_id = new_uuid7()
        with migrated_postgres_engine.begin() as connection:
            checkpoint_id = connection.execute(
                select(checkpoint_reservations.c.checkpoint_id).where(
                    checkpoint_reservations.c.attempt_id == UUID(prior["attempt_id"])
                )
            ).scalar_one()
            connection.execute(
                update(attempt_leases)
                .where(attempt_leases.c.lease_id == UUID(prior["lease_id"]))
                .values(expires_at=text("clock_timestamp() + interval '1 second'"))
            )

        blocker = migrated_postgres_engine.connect()
        transaction = blocker.begin()
        blocker.execute(
            select(checkpoint_reservations.c.checkpoint_id)
            .where(checkpoint_reservations.c.checkpoint_id == checkpoint_id)
            .with_for_update()
        ).scalar_one()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    client.app.state.services.worker.adopt_attempt,
                    worker_id=UUID(WORKER_ID),
                    credential=credential,
                    attempt_id=UUID(prior["attempt_id"]),
                    callback_id=callback_id,
                    payload_hash=jcs_request_hash(body),
                    request=request,
                )
                _wait_for_database_lock(migrated_postgres_engine)
                time.sleep(1.2)
                transaction.commit()
                with pytest.raises(ApplicationError) as failure:
                    future.result(timeout=5)
        finally:
            if transaction.is_active:
                transaction.rollback()
            blocker.close()
        assert failure.value.code == "state_conflict"
        with migrated_postgres_engine.connect() as connection:
            grants = (
                connection.execute(
                    select(attempt_authority_grants.c.grant_id).where(
                        attempt_authority_grants.c.attempt_id == UUID(prior["attempt_id"])
                    )
                )
                .scalars()
                .all()
            )
        assert grants == [prior["grant_id"]]


def test_policy_lock_precedes_receipt_and_worker_for_adopt_and_renew(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        first = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b10-lock-order-old-0001"
        )
        prior = _running_authority(migrated_postgres_engine, first)
        current = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b10-lock-order-new-0001"
        )
        statements: list[str] = []

        def record(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
            statements.append(statement.lower())

        event.listen(migrated_postgres_engine, "before_cursor_execute", record)
        try:
            adoption = client.post(
                f"/v1/attempts/{prior['attempt_id']}/adopt",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "X-Callback-Id": str(new_uuid7()),
                },
                json={
                    "prior_authority": {
                        key: prior[key]
                        for key in (
                            "worker_id",
                            "worker_incarnation_id",
                            "attempt_id",
                            "allocation_id",
                            "lease_id",
                            "job_fence",
                        )
                    },
                    "current_worker_incarnation_id": current["worker_incarnation_id"],
                    "container": {
                        "container_id": CONTAINER_ID,
                        "runtime_identity_digest": DIGEST,
                    },
                },
            )
            assert adoption.status_code == 200, adoption.text
            _assert_policy_receipt_worker_order(statements)
            statements.clear()
            renewal = client.post(
                f"/v1/attempts/{prior['attempt_id']}/renew",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "X-Callback-Id": str(new_uuid7()),
                },
                json={
                    "authority": adoption.json()["authority"],
                    "progress_sequence": 0,
                    "progress": None,
                },
            )
            assert renewal.status_code == 200, renewal.text
            _assert_policy_receipt_worker_order(statements)
        finally:
            event.remove(migrated_postgres_engine, "before_cursor_execute", record)
