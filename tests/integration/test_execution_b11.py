import pytest

from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration.test_worker_api_b10 import _bootstrap_worker, _create_incarnation
from tests.integration.test_worker_authority_b10 import _running_authority

pytestmark = pytest.mark.postgres


def test_result_reservation_is_server_generated_and_callback_replayed(
    migrated_postgres_engine, tmp_path
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-incarnation-reserve"
        )
        value = _running_authority(migrated_postgres_engine, incarnation)
        authority = {k: v for k, v in value.items() if k not in {"grant_id", "job_id"}}
        callback = str(new_uuid7())
        headers = {"Authorization": f"Bearer {credential}", "X-Callback-Id": callback}
        url = f"/v1/attempts/{value['attempt_id']}/result-reservations"
        first = client.post(url, headers=headers, json={"authority": authority})
        assert first.status_code == 201, first.text
        second = client.post(url, headers=headers, json={"authority": authority})
        assert second.status_code == 201, second.text
        assert first.json() == second.json()
        assert first.json()["job_id"] == str(value["job_id"])
        distinct = client.post(
            url,
            headers={**headers, "X-Callback-Id": str(new_uuid7())},
            json={"authority": authority},
        )
        assert distinct.status_code == 409, distinct.text


def test_start_requires_claim_and_binds_exact_container(migrated_postgres_engine, tmp_path):
    from uuid import UUID

    from sqlalchemy import delete, select, update

    from nexa.infrastructure.persistence.schema import attempts, container_identities, jobs

    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-incarnation-start00"
        )
        value = _running_authority(migrated_postgres_engine, incarnation)
        authority = {k: v for k, v in value.items() if k not in {"grant_id", "job_id"}}
        with migrated_postgres_engine.begin() as connection:
            connection.execute(delete(container_identities))
            connection.execute(update(attempts).values(state="CREATED", started_at=None))
            connection.execute(update(jobs).values(state="DISPATCHING"))
            nonce = connection.execute(select(attempts.c.startup_nonce)).scalar_one()
        headers = {"Authorization": f"Bearer {credential}", "X-Callback-Id": str(new_uuid7())}
        start = {
            "authority": authority,
            "startup_nonce": str(nonce),
            "executor_operation_sequence": 1,
            "container": {
                "container_id": "a" * 64,
                "runtime_identity_digest": "sha256:" + "b" * 64,
            },
        }
        url = f"/v1/attempts/{value['attempt_id']}"
        early = client.post(url + "/start", headers=headers, json=start)
        assert early.status_code == 409, early.text
        claim = client.post(url + "/claim", headers=headers, json={"authority": authority})
        assert claim.status_code == 200, claim.text
        assert claim.json()["job_version"] == 1
        with migrated_postgres_engine.connect() as connection:
            assert connection.execute(select(jobs.c.version)).scalar_one() == 1
            assert connection.execute(select(jobs.c.event_sequence)).scalar_one() == 0
        repeated = client.post(
            url + "/claim",
            headers={**headers, "X-Callback-Id": str(new_uuid7())},
            json={"authority": authority},
        )
        assert repeated.json()["execution_context"] == claim.json()["execution_context"]
        started = client.post(url + "/start", headers=headers, json=start)
        assert started.status_code == 200, started.text
        replay = client.post(url + "/start", headers=headers, json=start)
        assert replay.json() == started.json()
        changed = client.post(
            url + "/start",
            headers={**headers, "X-Callback-Id": str(new_uuid7())},
            json={
                **start,
                "container": {
                    "container_id": "c" * 64,
                    "runtime_identity_digest": "sha256:" + "b" * 64,
                },
            },
        )
        assert changed.status_code == 409, changed.text
        with migrated_postgres_engine.connect() as connection:
            tenant_id = connection.execute(select(jobs.c.tenant_id)).scalar_one()
            job_id = connection.execute(select(jobs.c.job_id)).scalar_one()
        admin = client.post(
            "/v1/internal/admin-bootstrap",
            headers={
                "X-Nexa-Bootstrap-Secret": "b" * 32,
                "Idempotency-Key": "b11-start-events-admin",
            },
            json={
                "username": "b11-start-events@example.test",
                "display_name": "Start Events",
                "password": "correct-horse-battery-staple",
            },
        )
        assert admin.status_code == 201, admin.text
        with migrated_postgres_engine.begin() as connection:
            from nexa.infrastructure.persistence import schema as s

            connection.execute(
                s.memberships.insert().values(
                    tenant_id=tenant_id,
                    user_id=UUID(admin.json()["user_id"]),
                    role="MEMBER",
                )
            )
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b11-start-events@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200, login.text
        events = client.get(
            f"/v1/jobs/{job_id}/events", headers={"X-Nexa-Tenant-Id": str(tenant_id)}
        )
        assert events.status_code == 200, events.text
        assert [item["type"] for item in events.json()["items"]] == ["ATTEMPT_STARTED"]


def test_attempt_upload_replays_and_rechecks_authority(migrated_postgres_engine, tmp_path):
    import hashlib
    from datetime import UTC, datetime, timedelta
    from uuid import UUID

    from sqlalchemy import select, update

    from nexa.infrastructure.persistence.schema import attempt_leases

    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-incarnation-upload0"
        )
        value = _running_authority(migrated_postgres_engine, incarnation)
        data = b'{"answer":42}'
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/octet-stream",
            "Idempotency-Key": "b11-upload-result-000001",
            "X-Artifact-Kind": "RESULT_FILE",
            "X-Artifact-Media-Type": "application/json",
            "X-Artifact-Checksum": "sha256:" + hashlib.sha256(data).hexdigest(),
            "X-Artifact-Size": str(len(data)),
            "X-Worker-Id": value["worker_id"],
            "X-Worker-Incarnation-Id": value["worker_incarnation_id"],
            "X-Allocation-Id": value["allocation_id"],
            "X-Lease-Id": value["lease_id"],
            "X-Job-Fence": str(value["job_fence"]),
        }
        url = f"/v1/attempts/{value['attempt_id']}/artifacts"
        first = client.post(url, headers=headers, content=data)
        assert first.status_code == 201, first.text
        replay = client.post(url, headers=headers, content=data)
        assert replay.status_code == 201 and first.json() == replay.json()
        from nexa.infrastructure.persistence import schema as s
        from tests.integration._factories import seed_authority, seed_job

        with migrated_postgres_engine.begin() as connection:
            original = connection.execute(select(s.jobs)).mappings().one()
            spec = connection.execute(select(s.job_specs)).mappings().one()
            graph = {
                "tenant_id": original["tenant_id"],
                "user_id": original["submitter_user_id"],
                "artifact_id": spec["input_artifact_id"],
                "template_id": spec["template_id"],
            }
            other_job = seed_job(connection, graph, state="RUNNING")
            other = seed_authority(
                connection,
                graph,
                other_job,
                {
                    "worker_id": UUID(value["worker_id"]),
                    "incarnation_id": UUID(value["worker_incarnation_id"]),
                },
            )
            connection.execute(
                update(s.attempts)
                .where(s.attempts.c.attempt_id == other["attempt_id"])
                .values(state="RUNNING", started_at=datetime.now(UTC))
            )
        another_attempt = client.post(
            f"/v1/attempts/{other['attempt_id']}/artifacts",
            headers={
                **headers,
                "X-Allocation-Id": str(other["allocation_id"]),
                "X-Lease-Id": str(other["lease_id"]),
            },
            content=data,
        )
        assert another_attempt.status_code == 409, another_attempt.text
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                update(attempt_leases).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        stale = client.post(url, headers=headers, content=data)
        assert stale.status_code == 409, stale.text
