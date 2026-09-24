from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, update

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration.test_worker_api_b10 import _bootstrap_worker, _create_incarnation
from tests.integration.test_worker_authority_b10 import CONTAINER_ID, DIGEST, _running_authority

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "prior_retries,failure_class", [(0, "INTERNAL"), (0, "INFRASTRUCTURE"), (1, "INFRASTRUCTURE")]
)
def test_failure_then_cleanup_replays_without_double_release(
    migrated_postgres_engine, tmp_path, prior_retries, failure_class
):
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-cleanup-process"
        )
        prior = _running_authority(engine, incarnation)
        with engine.begin() as connection:
            connection.execute(update(s.jobs).values(retry_count=prior_retries))
            job = connection.execute(select(s.jobs)).mappings().one()
            nonce = connection.execute(select(s.attempts.c.startup_nonce)).scalar_one()
            for scope, identity in [
                ("GLOBAL", "global"),
                ("TENANT", str(job["tenant_id"])),
                ("USER", f"{job['tenant_id']}:{job['submitter_user_id']}"),
            ]:
                row = connection.execute(
                    select(s.admission_counters).where(
                        s.admission_counters.c.scope_type == scope,
                        s.admission_counters.c.scope_id == identity,
                    )
                ).first()
                if row is None:
                    connection.execute(
                        s.admission_counters.insert().values(
                            scope_type=scope, scope_id=identity, active_attempts=1, outstanding=1
                        )
                    )
                else:
                    connection.execute(
                        update(s.admission_counters)
                        .where(
                            s.admission_counters.c.scope_type == scope,
                            s.admission_counters.c.scope_id == identity,
                        )
                        .values(active_attempts=1, outstanding=1)
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
        failure = {
            "authority": authority,
            "failure_class": failure_class,
            "reason_code": "WORKLOAD_EXIT_NONZERO"
            if failure_class == "INTERNAL"
            else "EXECUTOR_UNAVAILABLE",
            "observation": {
                "observation_type": "CONTAINER",
                "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
                "observed_at": datetime.now(UTC).isoformat(),
                "exit_code": 1,
                "oom_killed": False,
                "runtime_limit_reached": False,
            },
        }
        callback = str(new_uuid7())
        headers = {"Authorization": f"Bearer {credential}", "X-Callback-Id": callback}
        path = f"/v1/attempts/{prior['attempt_id']}"
        failed = client.post(path + "/fail", headers=headers, json=failure)
        assert failed.status_code == 200, failed.text
        ack = failed.json()
        assert ack["job_state"] == "RECOVERING"
        assert client.post(path + "/fail", headers=headers, json=failure).json() == ack
        with engine.connect() as connection:
            assert connection.execute(select(s.allocations.c.state)).scalar_one() == "QUARANTINED"
            assert connection.execute(select(s.jobs.c.job_fence)).scalar_one() == 2
            assert all(
                row == 1
                for row in connection.execute(
                    select(s.admission_counters.c.active_attempts)
                ).scalars()
            )
        proof = {
            "proof_type": "CONTAINER_STOPPED",
            "startup_nonce": str(nonce),
            "executor_operation_sequence": 2,
            "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
            "stopped_at": datetime.now(UTC).isoformat(),
            "exit_code": 1,
            "inspection_checksum": "sha256:" + "c" * 64,
        }
        request = {**{k: v for k, v in authority.items() if k != "lease_id"}, "proof": proof}
        headers["X-Callback-Id"] = str(new_uuid7())
        response = client.post(path + "/cleanup", headers=headers, json=request)
        assert response.status_code == 200, response.text
        released = response.json()
        assert released["verified"] is True
        assert client.post(path + "/cleanup", headers=headers, json=request).json() == released
        headers["X-Callback-Id"] = str(new_uuid7())
        assert (
            client.post(path + "/cleanup", headers=headers, json=request).json()["verified"] is True
        )
        with engine.connect() as connection:
            expected = "FAILED" if failure_class == "INTERNAL" else "RETRY_WAIT"
            assert connection.execute(select(s.jobs.c.state)).scalar_one() == expected
            if failure_class == "INFRASTRUCTURE":
                schedule = connection.execute(select(s.retry_schedules)).mappings().one()
                released_at = connection.execute(select(s.allocations.c.released_at)).scalar_one()
                delay = (schedule["ready_at"] - released_at).total_seconds()
                assert 2**prior_retries <= delay <= 2**prior_retries + 1
                assert schedule["retry_number"] == prior_retries + 1
                assert schedule["ready_at"] - released_at == timedelta(
                    seconds=2**prior_retries, milliseconds=schedule["jitter_milliseconds"]
                )
            assert connection.execute(select(s.attempts.c.state)).scalar_one() == "FAILED"
            assert connection.execute(select(s.allocations.c.state)).scalar_one() == "RELEASED"
            assert all(
                row == (int(failure_class == "INFRASTRUCTURE"), 0)
                for row in connection.execute(
                    select(
                        s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts
                    )
                )
            )
            assert len(connection.execute(select(s.events)).all()) == 2
        admin = client.post(
            "/v1/internal/admin-bootstrap",
            headers={
                "X-Nexa-Bootstrap-Secret": "b" * 32,
                "Idempotency-Key": "b11-cleanup-events-admin",
            },
            json={
                "username": "b11-cleanup-events@example.test",
                "display_name": "Cleanup Events",
                "password": "correct-horse-battery-staple",
            },
        )
        assert admin.status_code == 201, admin.text
        with engine.begin() as connection:
            connection.execute(
                s.memberships.insert().values(
                    tenant_id=job["tenant_id"],
                    user_id=UUID(admin.json()["user_id"]),
                    role="MEMBER",
                )
            )
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b11-cleanup-events@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200, login.text
        events = client.get(
            f"/v1/jobs/{job['job_id']}/events",
            headers={"X-Nexa-Tenant-Id": str(job["tenant_id"])},
        )
        assert events.status_code == 200, events.text
        assert [item["type"] for item in events.json()["items"]] == [
            "ATTEMPT_FAILED",
            "ALLOCATION_RELEASED",
        ]


@pytest.mark.parametrize("alternate_callback", [False, True])
def test_pending_cleanup_receipt_replays_then_promotes_once_through_http(
    migrated_postgres_engine, tmp_path, alternate_callback
):
    """An exact 202 replay stays pending until failure linearization permits release."""
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-pending-cleanup-incarnation"
        )
        authority = _running_authority(engine, incarnation)
        with engine.begin() as connection:
            job = connection.execute(select(s.jobs)).mappings().one()
            startup_nonce = connection.execute(select(s.attempts.c.startup_nonce)).scalar_one()
            # Simulate an earlier revoke whose failure classification has not
            # committed yet. Cleanup is proven stopped but cannot release.
            now = datetime.now(UTC)
            connection.execute(update(s.jobs).values(state="RECOVERING"))
            connection.execute(update(s.attempts).values(state="STOPPING"))
            connection.execute(update(s.attempt_leases).values(revoked_at=now))
            connection.execute(update(s.attempt_authority_grants).values(ended_at=now))
            connection.execute(
                update(s.allocations).values(state="QUARANTINED", quarantined_at=now)
            )
            for scope, identity in (
                ("GLOBAL", "global"),
                ("TENANT", str(job["tenant_id"])),
                ("USER", f"{job['tenant_id']}:{job['submitter_user_id']}"),
            ):
                connection.execute(
                    s.admission_counters.insert().values(
                        scope_type=scope, scope_id=identity, active_attempts=1, outstanding=1
                    )
                )
        path = f"/v1/attempts/{authority['attempt_id']}/cleanup"
        headers = {
            "Authorization": f"Bearer {credential}",
            "X-Callback-Id": str(new_uuid7()),
        }
        payload = {
            **{k: v for k, v in authority.items() if k not in {"lease_id", "grant_id", "job_id"}},
            "proof": {
                "proof_type": "CONTAINER_STOPPED",
                "startup_nonce": str(startup_nonce),
                "executor_operation_sequence": 2,
                "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
                "stopped_at": datetime.now(UTC).isoformat(),
                "exit_code": 1,
                "inspection_checksum": "sha256:" + "c" * 64,
            },
        }
        first = client.post(path, headers=headers, json=payload)
        assert first.status_code == 202, first.text
        assert first.json()["verified"] is False
        again = client.post(path, headers=headers, json=payload)
        assert again.status_code == 202, again.text
        assert again.json() == first.json()
        with engine.connect() as connection:
            assert connection.execute(select(s.allocations.c.state)).scalar_one() == "QUARANTINED"
            assert all(
                row == (1, 1)
                for row in connection.execute(
                    select(
                        s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts
                    )
                )
            )
            assert (
                connection.execute(
                    select(s.callback_receipts.c.acknowledgment).where(
                        s.callback_receipts.c.callback_id == UUID(headers["X-Callback-Id"])
                    )
                ).scalar_one()
                == first.json()
            )
        # An independently committed classification makes the held allocation
        # releasable. Retry the original callback, as the worker does after 202.
        with engine.begin() as connection:
            connection.execute(
                update(s.attempts).values(
                    failure_class="INTERNAL", failure_reason="WORKLOAD_EXIT_NONZERO"
                )
            )
        if alternate_callback:
            # Another callback may release first; the original pending receipt
            # still needs promotion without running release/accounting twice.
            other_headers = {**headers, "X-Callback-Id": str(new_uuid7())}
            other_release = client.post(path, headers=other_headers, json=payload)
            assert other_release.status_code == 200, other_release.text
            with engine.connect() as connection:
                before_promotion = (
                    connection.execute(
                        select(
                            s.admission_counters.c.outstanding,
                            s.admission_counters.c.active_attempts,
                            s.admission_counters.c.version,
                        )
                    ).all(),
                    connection.execute(select(s.allocation_ledger_segments)).mappings().all(),
                    connection.execute(select(s.events)).mappings().all(),
                )
        released = client.post(path, headers=headers, json=payload)
        assert released.status_code == 200, released.text
        assert released.json()["verified"] is True
        assert released.json()["allocation_state"] == "RELEASED"
        with engine.connect() as connection:
            counters = connection.execute(
                select(s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts)
            ).all()
            segments = connection.execute(select(s.allocation_ledger_segments)).mappings().all()
            assert all(row == (0, 0) for row in counters)
            assert connection.execute(select(s.allocations.c.state)).scalar_one() == "RELEASED"
            assert connection.execute(select(s.jobs.c.state)).scalar_one() == "FAILED"
            assert len(connection.execute(select(s.events)).all()) == 1
            assert (
                connection.execute(
                    select(s.callback_receipts.c.acknowledgment).where(
                        s.callback_receipts.c.callback_id == UUID(headers["X-Callback-Id"])
                    )
                ).scalar_one()
                == released.json()
            )
            if alternate_callback:
                assert (
                    connection.execute(
                        select(
                            s.admission_counters.c.outstanding,
                            s.admission_counters.c.active_attempts,
                            s.admission_counters.c.version,
                        )
                    ).all(),
                    segments,
                    connection.execute(select(s.events)).mappings().all(),
                ) == before_promotion
        final_replay = client.post(path, headers=headers, json=payload)
        assert final_replay.status_code == 200 and final_replay.json() == released.json()
        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(
                        s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts
                    )
                ).all()
                == counters
            )
            assert connection.execute(select(s.allocation_ledger_segments)).mappings().all() == (
                segments
            )
            assert len(connection.execute(select(s.events)).all()) == 1
        altered = {**payload, "proof": {**payload["proof"], "exit_code": 2}}
        mismatch = client.post(path, headers=headers, json=altered)
        assert mismatch.status_code == 409, mismatch.text


def test_terminal_cleanup_after_incarnation_change_uses_original_grant(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        original = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-terminal-original-incarnation"
        )
        authority = _running_authority(engine, original)
        with engine.begin() as connection:
            job = connection.execute(select(s.jobs)).mappings().one()
            nonce = connection.execute(select(s.attempts.c.startup_nonce)).scalar_one()
            connection.execute(update(s.jobs).values(state="SUCCEEDED"))
            connection.execute(update(s.attempts).values(state="SUCCEEDED"))
            for scope, identity in (
                ("GLOBAL", "global"),
                ("TENANT", str(job["tenant_id"])),
                ("USER", f"{job['tenant_id']}:{job['submitter_user_id']}"),
            ):
                connection.execute(
                    s.admission_counters.insert().values(
                        scope_type=scope, scope_id=identity, active_attempts=1, outstanding=0
                    )
                )
        successor = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-terminal-new-incarnation"
        )
        page = client.get(
            f"/v1/workers/{authority['worker_id']}/reconciliation?page_size=100",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Worker-Incarnation-Id": successor["worker_incarnation_id"],
            },
        )
        assert page.status_code == 200, page.text
        assert page.json()["items"][0]["authority_state"] == "REVOKED"
        assert page.json()["items"][0]["allocation"]["state"] == "HELD"
        request = {
            key: authority[key]
            for key in (
                "worker_id",
                "worker_incarnation_id",
                "attempt_id",
                "allocation_id",
                "job_fence",
            )
        }
        request["proof"] = {
            "proof_type": "CONTAINER_STOPPED",
            "startup_nonce": str(nonce),
            "executor_operation_sequence": 2,
            "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
            "stopped_at": datetime.now(UTC).isoformat(),
            "exit_code": 0,
            "inspection_checksum": "sha256:" + "c" * 64,
        }
        response = client.post(
            f"/v1/attempts/{authority['attempt_id']}/cleanup",
            headers={"Authorization": f"Bearer {credential}", "X-Callback-Id": str(new_uuid7())},
            json=request,
        )
        assert response.status_code == 200, response.text
        assert response.json()["allocation_state"] == "RELEASED"
        with engine.connect() as connection:
            assert connection.execute(select(s.allocations.c.state)).scalar_one() == "RELEASED"
            assert set(connection.execute(select(s.admission_counters.c.active_attempts))) == {(0,)}


def test_rejected_late_start_binds_observed_container_then_releases_exact_proof(
    migrated_postgres_engine, tmp_path
):
    from sqlalchemy import delete

    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-late-start-incarnation"
        )
        value = _running_authority(engine, incarnation)
        authority = {k: v for k, v in value.items() if k not in {"grant_id", "job_id"}}
        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(delete(s.container_identities))
            connection.execute(
                update(s.attempts).values(
                    state="CLAIMED",
                    started_at=None,
                    claimed_at=now - timedelta(seconds=25),
                    created_at=now - timedelta(seconds=31),
                )
            )
            connection.execute(update(s.jobs).values(state="DISPATCHING"))
            connection.execute(
                update(s.attempt_leases).values(expires_at=now + timedelta(seconds=14))
            )
            nonce = connection.execute(select(s.attempts.c.startup_nonce)).scalar_one()
            job = connection.execute(select(s.jobs)).mappings().one()
            for scope, identity in [
                ("GLOBAL", "global"),
                ("TENANT", str(job["tenant_id"])),
                ("USER", f"{job['tenant_id']}:{job['submitter_user_id']}"),
            ]:
                row = connection.execute(
                    select(s.admission_counters).where(
                        s.admission_counters.c.scope_type == scope,
                        s.admission_counters.c.scope_id == identity,
                    )
                ).first()
                if row is None:
                    connection.execute(
                        s.admission_counters.insert().values(
                            scope_type=scope, scope_id=identity, active_attempts=1, outstanding=1
                        )
                    )
                else:
                    connection.execute(
                        update(s.admission_counters)
                        .where(
                            s.admission_counters.c.scope_type == scope,
                            s.admission_counters.c.scope_id == identity,
                        )
                        .values(active_attempts=1, outstanding=1)
                    )
        path = f"/v1/attempts/{value['attempt_id']}"
        headers = {"Authorization": f"Bearer {credential}", "X-Callback-Id": str(new_uuid7())}
        container = {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST}
        late = client.post(
            path + "/start",
            headers=headers,
            json={
                "authority": authority,
                "startup_nonce": str(nonce),
                "executor_operation_sequence": 3,
                "container": container,
            },
        )
        assert late.status_code == 409, late.text
        observed_at = datetime.now(UTC)
        headers["X-Callback-Id"] = str(new_uuid7())
        failure = client.post(
            path + "/fail",
            headers=headers,
            json={
                "authority": authority,
                "failure_class": "TIMEOUT",
                "reason_code": "STARTUP_TIMEOUT",
                "observation": {
                    "observation_type": "CONTAINER",
                    "container": container,
                    "observed_at": observed_at.isoformat(),
                    "exit_code": None,
                    "oom_killed": False,
                    "runtime_limit_reached": False,
                },
            },
        )
        assert failure.status_code == 200, failure.text
        assert failure.json()["job_state"] == "RECOVERING"
        with engine.connect() as connection:
            bound = connection.execute(select(s.container_identities)).mappings().one()
            assert bound["container_id"] == CONTAINER_ID
            assert bound["runtime_identity_digest"] == DIGEST
            assert connection.execute(select(s.allocations.c.state)).scalar_one() == "QUARANTINED"
        headers["X-Callback-Id"] = str(new_uuid7())
        cleanup = {
            **{k: v for k, v in authority.items() if k != "lease_id"},
            "proof": {
                "proof_type": "CONTAINER_STOPPED",
                "startup_nonce": str(nonce),
                "executor_operation_sequence": 5,
                "container": container,
                "stopped_at": datetime.now(UTC).isoformat(),
                "exit_code": 0,
                "inspection_checksum": "sha256:" + "c" * 64,
            },
        }
        wrong = client.post(
            path + "/cleanup",
            headers=headers,
            json={
                **cleanup,
                "proof": {
                    **cleanup["proof"],
                    "container": {**container, "container_id": "d" * 64},
                },
            },
        )
        assert wrong.status_code == 409, wrong.text
        released = client.post(path + "/cleanup", headers=headers, json=cleanup)
        assert released.status_code == 200, released.text
        assert released.json()["verified"] is True
        assert client.post(path + "/cleanup", headers=headers, json=cleanup).json() == (
            released.json()
        )
        with engine.connect() as connection:
            assert connection.execute(select(s.allocations.c.state)).scalar_one() == "RELEASED"
            assert all(
                row == (0, 0)
                for row in connection.execute(
                    select(
                        s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts
                    )
                )
            )
