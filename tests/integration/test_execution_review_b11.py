"""Review regressions through production HTTP and PostgreSQL, not Docker evidence."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import select, text, update

from nexa.application.execution_cleanup import _lock_policy_counters
from nexa.application.worker_service import WorkerService
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.transactions import run_transaction
from tests.api.test_http_contract import _client
from tests.integration.test_worker_api_b10 import WORKER_ID, _bootstrap_worker, _create_incarnation
from tests.integration.test_worker_authority_b10 import _running_authority

pytestmark = pytest.mark.postgres


def test_callback_locks_policy_before_worker_foreign_key(migrated_postgres_engine, tmp_path):
    """A coordinator policy lock must not cycle with a callback's FK worker lock."""
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        _bootstrap_worker(client)
        factory = create_session_factory(engine)

        def callback():
            def operation(session):
                session.execute(text("SET LOCAL application_name = 'b11_callback_lock_probe'"))
                receipt, _ = WorkerService._begin_callback(
                    session,
                    worker_id=UUID(WORKER_ID),
                    operation_id="workerHeartbeat",
                    callback_id=new_uuid7(),
                    payload_hash="sha256:" + "a" * 64,
                )
                WorkerService._mode(session)
                WorkerService._complete_callback(session, receipt, {"accepted": True})

            run_transaction(factory, operation)

        with engine.connect() as leader:
            transaction = leader.begin()
            leader.execute(
                select(s.policy_versions.c.policy_version)
                .where(s.policy_versions.c.is_current.is_(True))
                .with_for_update()
            ).one()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(callback)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        with engine.connect() as observer:
                            waiting = observer.execute(
                                text(
                                    "SELECT count(*) FROM pg_stat_activity "
                                    "WHERE datname = current_database() "
                                    "AND wait_event_type = 'Lock' "
                                    "AND application_name = 'b11_callback_lock_probe'"
                                )
                            ).scalar_one()
                        if waiting:
                            break
                        time.sleep(0.02)
                    else:
                        pytest.fail("callback never reached the policy lock")
                    # Before the fix the pending receipt has already acquired
                    # a KEY SHARE lock on workers and this NOWAIT probe fails.
                    with engine.connect() as observer:
                        assert observer.execute(
                            select(s.workers.c.worker_id)
                            .where(s.workers.c.worker_id == UUID(WORKER_ID))
                            .with_for_update(nowait=True)
                        ).scalar_one() == UUID(WORKER_ID)
                finally:
                    transaction.rollback()
                    pending.result(timeout=5)


@pytest.mark.parametrize(
    "operations",
    [
        ("workerFailAttempt", "workerReportCleanup"),
        ("workerFailAttempt", "workerFailAttempt"),
        ("workerReportCleanup", "workerReportCleanup"),
    ],
)
def test_exclusive_policy_callbacks_do_not_deadlock_on_shared_to_update_upgrade(
    migrated_postgres_engine, tmp_path, operations
):
    """Overlap distinct callbacks after their first policy lock, before counter locks."""
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-policy-upgrade-incarnation"
        )
        authority = _running_authority(engine, incarnation)
        with engine.begin() as connection:
            job = connection.execute(select(s.jobs)).mappings().one()
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
        factory = create_session_factory(engine)
        first_ready, second_ready, proceed = Event(), Event(), Event()

        def callback(operation_id, index):
            def operation(session):
                session.execute(text(f"SET LOCAL application_name = 'b11_policy_upgrade_{index}'"))
                session.execute(text("SET LOCAL lock_timeout = '4s'"))
                receipt, replay = WorkerService._begin_callback(
                    session,
                    worker_id=UUID(WORKER_ID),
                    operation_id=operation_id,
                    callback_id=new_uuid7(),
                    payload_hash="sha256:" + "a" * 64,
                )
                assert replay is None
                (first_ready if index == 1 else second_ready).set()
                assert proceed.wait(timeout=10)
                _lock_policy_counters(session, UUID(authority["attempt_id"]))
                WorkerService._complete_callback(session, receipt, {"accepted": True})

            run_transaction(factory, operation, max_attempts=1)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(callback, operations[0], 1)
            assert first_ready.wait(timeout=5)
            second = pool.submit(callback, operations[1], 2)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if second_ready.is_set():
                        break  # Both shared locks were acquired: the old deadlock schedule.
                    with engine.connect() as observer:
                        waiting = observer.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname = current_database() "
                                "AND application_name = 'b11_policy_upgrade_2' "
                                "AND wait_event_type = 'Lock'"
                            )
                        ).scalar_one()
                    if waiting:
                        break  # The exclusive first lock safely serialized the callbacks.
                    time.sleep(0.02)
                else:
                    pytest.fail("second callback never acquired or waited on policy lock")
            finally:
                proceed.set()
            first.result(timeout=10)
            second.result(timeout=10)
        with engine.connect() as connection:
            assert len(connection.execute(select(s.callback_receipts)).all()) == 2
            assert all(
                row == (1, 1)
                for row in connection.execute(
                    select(
                        s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts
                    )
                )
            )


def running(client, engine):
    credential = _bootstrap_worker(client)
    incarnation = _create_incarnation(
        client, credential, nonce=str(new_uuid7()), key="b11-review-incarnation"
    )
    value = _running_authority(engine, incarnation)
    authority = {k: v for k, v in value.items() if k not in {"grant_id", "job_id"}}
    return credential, authority


def headers(credential, authority):
    return {
        "Authorization": f"Bearer {credential}",
        "X-Worker-Id": authority["worker_id"],
        "X-Worker-Incarnation-Id": authority["worker_incarnation_id"],
        "X-Allocation-Id": authority["allocation_id"],
        "X-Lease-Id": authority["lease_id"],
        "X-Job-Fence": str(authority["job_fence"]),
    }


def test_poll_committed_offer_closed_schema_replay(migrated_postgres_engine, tmp_path):
    from nexa.coordinator.service import CoordinatorService
    from nexa.infrastructure.persistence.database import create_session_factory
    from tests.integration.test_coordinator_b11 import seed_dispatchable
    from tests.integration.test_jobs_b08 import _submit_body
    from tests.integration.test_worker_api_b10 import _inventory

    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-poll-incarnation"
        )
        graph, worker, _ = seed_dispatchable(
            engine,
            count=0,
            template_id="cpu-iterative",
            existing_worker=(UUID(WORKER_ID), UUID(incarnation["worker_incarnation_id"])),
        )
        with engine.begin() as connection:
            spec = _submit_body(graph["artifact_id"])["spec"]
            spec["resources"]["memory_bytes"] = 1_073_741_824
            spec["runtime_limit_seconds"] = 300
            # Immutable fixture spec is inserted before dispatch, using the actual closed wire spec.
            from tests.integration._factories import seed_job

            seed_job(connection, graph, canonical_spec=spec)
            connection.execute(update(s.admission_counters).values(outstanding=1))
        service = CoordinatorService(create_session_factory(engine))
        service.tick(service.acquire())
        # Periodic reconciliation can land after dispatch and before the first
        # poll. A live, unclaimed allocation without any container must not
        # make the worker ineligible to fetch its committed offer.
        worker_headers = {
            "Authorization": f"Bearer {credential}",
            "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"],
        }
        page = client.get(
            f"/v1/workers/{WORKER_ID}/reconciliation?page_size=100",
            headers=worker_headers,
        )
        assert page.status_code == 200, page.text
        assert len(page.json()["items"]) == 1
        assert page.json()["items"][0]["claim_state"] == "UNCLAIMED"
        assert page.json()["items"][0]["expected_container"] is None
        inventory = _inventory()
        inventory["architecture"] = "linux/arm64"
        inventory["images"][0]["architecture"] = "linux/arm64"
        heartbeat = client.post(
            f"/v1/workers/{WORKER_ID}/heartbeat",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Callback-Id": str(new_uuid7()),
            },
            json={
                "worker_incarnation_id": incarnation["worker_incarnation_id"],
                "observed_health": "READY",
                "reconcile_complete": True,
                "inventory": inventory,
                "observed_containers": [],
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text
        with engine.connect() as connection:
            assert connection.execute(select(s.workers.c.health)).scalar_one() == "READY"
        payload = {"worker_incarnation_id": str(worker["incarnation_id"]), "long_poll_seconds": 0}
        url = f"/v1/workers/{worker['worker_id']}/poll"
        # An allocation with an expired, non-revoked lease must not be offered.
        # Use PostgreSQL clock_timestamp() so the assertion exercises DB-time
        # expiry semantics rather than the test process clock.
        with engine.begin() as connection:
            connection.execute(
                update(s.attempt_leases).values(
                    expires_at=text("clock_timestamp() - interval '1 second'")
                )
            )
        expired = client.post(url, headers={"Authorization": f"Bearer {credential}"}, json=payload)
        assert expired.status_code == 200, expired.text
        assert expired.json()["offer"] is None
        with engine.begin() as connection:
            connection.execute(
                update(s.attempt_leases).values(
                    expires_at=text("clock_timestamp() + interval '45 seconds'")
                )
            )
        first = client.post(url, headers={"Authorization": f"Bearer {credential}"}, json=payload)
        assert first.status_code == 200, first.text
        offer = first.json()["offer"]
        assert set(offer) == {
            "authority",
            "dispatch_coordinator_epoch",
            "spec",
            "input_artifact",
            "checkpoint",
            "startup_limit_seconds",
            "lease_duration_seconds",
        }
        assert offer["spec"] == spec
        second = client.post(url, headers={"Authorization": f"Bearer {credential}"}, json=payload)
        assert second.status_code == 200 and second.json()["offer"] == offer
        with engine.connect() as connection:
            assert len(connection.execute(select(s.attempts)).all()) == 1
            job = connection.execute(select(s.jobs)).mappings().one()
        admin = client.post(
            "/v1/internal/admin-bootstrap",
            headers={"X-Nexa-Bootstrap-Secret": "b" * 32, "Idempotency-Key": "b11-event-admin-key"},
            json={
                "username": "b11-events@example.test",
                "display_name": "Events Admin",
                "password": "correct-horse-battery-staple",
            },
        )
        assert admin.status_code == 201, admin.text
        with engine.begin() as connection:
            connection.execute(
                s.memberships.insert().values(
                    tenant_id=job["tenant_id"], user_id=UUID(admin.json()["user_id"]), role="MEMBER"
                )
            )
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b11-events@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200, login.text
        events = client.get(
            f"/v1/jobs/{job['job_id']}/events",
            headers={"X-Nexa-Tenant-Id": str(job["tenant_id"])},
        )
        assert events.status_code == 200, events.text
        assert [item["type"] for item in events.json()["items"]] == ["JOB_DISPATCHING"]
