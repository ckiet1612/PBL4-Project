"""Remediation B15-OBS-02: ADMISSION_OFF stops new dispatch, not committed offers.

state-machines.md:110 rejects new admission and new dispatch while running, control,
checkpoint and cleanup continue. An offer the coordinator committed before the mode
change is existing work (the DRAINING precedent, B15-R22): the worker must still poll,
claim and run it without the reaper losing it and consuming a retry. The coordinator
must not create a new offer until the mode is NORMAL again, and the freeze still waits
for every allocation to be released. WRITE_FROZEN keeps rejecting poll.
"""

from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.integration.test_control_b15 import PASSWORD, _events
from tests.integration.test_retry_b14 import _job, _leader
from tests.integration.test_worker_api_b10 import WORKER_ID

pytestmark = pytest.mark.postgres


class _AllowProofs:
    """Isolates the unreleased-allocation guard from the external freeze/restore proofs."""

    def freeze_ready(self, session):
        return True

    def restore_verified(self, session):
        return True

    def readiness_verified(self, session):
        return True


def _admin_browser(browser, label):
    bootstrap = browser.post(
        "/v1/internal/admin-bootstrap",
        headers={"X-Nexa-Bootstrap-Secret": "b" * 32, "Idempotency-Key": f"{label}-admin-key"},
        json={"username": f"{label}@example.test", "display_name": "REM", "password": PASSWORD},
    )
    assert bootstrap.status_code == 201, bootstrap.text
    login = browser.post(
        "/v1/auth/login",
        headers={"Origin": "https://nexa.test"},
        json={"username": f"{label}@example.test", "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    browser.app.state.services.policy.proofs = _AllowProofs()
    csrf = login.json()["csrf_token"]

    def set_mode(mode):
        current = browser.get("/v1/admin/policy", headers={"Origin": "https://nexa.test"})
        assert current.status_code == 200, current.text
        return browser.patch(
            "/v1/admin/policy",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": csrf,
                "If-Match": current.headers["ETag"],
                "Idempotency-Key": f"{label}-mode-{new_uuid7()}",
            },
            json={"operational_mode": mode},
        )

    return set_mode


def _attempts(engine, job_id):
    with engine.connect() as connection:
        return (
            connection.execute(
                select(s.attempts)
                .where(s.attempts.c.job_id == job_id)
                .order_by(s.attempts.c.attempt_id)
            )
            .mappings()
            .all()
        )


def _allocation_states(engine):
    with engine.connect() as connection:
        return sorted(connection.execute(select(s.allocations.c.state)).scalars())


def test_admission_off_keeps_a_committed_offer_claimable_until_it_succeeds(
    migrated_postgres_engine, tmp_path
):
    from tests.api.test_http_contract import _client
    from tests.integration._factories import seed_job
    from tests.integration.test_completion_race_b11 import (
        _authority,
        _cleanup_request,
        _prepare_completion,
    )
    from tests.integration.test_control_b15 import _reconcile_and_heartbeat
    from tests.integration.test_coordinator_b11 import seed_dispatchable
    from tests.integration.test_jobs_b08 import _submit_body
    from tests.integration.test_worker_api_b10 import _bootstrap_worker, _create_incarnation
    from tests.integration.test_worker_authority_b10 import DIGEST

    engine = migrated_postgres_engine
    users = tmp_path / "users"
    users.mkdir()
    with _client(engine, tmp_path) as client, _client(engine, users) as browser:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="rem-obs02-offer-incarnation"
        )
        incarnation_id = incarnation["worker_incarnation_id"]
        graph, _, _ = seed_dispatchable(
            engine,
            count=0,
            template_id="cpu-iterative",
            existing_worker=(UUID(WORKER_ID), UUID(incarnation_id)),
        )
        with engine.begin() as connection:
            spec = _submit_body(graph["artifact_id"])["spec"]
            spec["resources"]["memory_bytes"] = 1_073_741_824
            job_id = seed_job(connection, graph, canonical_spec=spec)["job_id"]
            connection.execute(update(s.jobs).values(eligible_since=func.clock_timestamp()))
            connection.execute(update(s.admission_counters).values(outstanding=1))
        service, epoch = _leader(engine)
        service.tick(epoch)
        (attempt,) = _attempts(engine, job_id)
        assert attempt["state"] == "CREATED"
        assert _reconcile_and_heartbeat(client, engine, credential, incarnation, []) == "READY"

        set_mode = _admin_browser(browser, "rem-obs02-offer")
        off = set_mode("ADMISSION_OFF")
        assert off.status_code == 200, off.text
        assert off.json()["operational_mode"] == "ADMISSION_OFF"
        # The committed offer still holds its allocation, so the freeze must wait.
        held = set_mode("WRITE_FROZEN")
        assert held.status_code == 409 and held.json()["code"] == "state_conflict", held.text
        assert _allocation_states(engine) == ["HELD"]

        def poll():
            return client.post(
                f"/v1/workers/{WORKER_ID}/poll",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "X-Worker-Incarnation-Id": incarnation_id,
                },
                json={"worker_incarnation_id": incarnation_id},
            )

        polled = poll()
        assert polled.status_code == 200, polled.text
        offer = polled.json()["offer"]
        assert offer is not None and offer["authority"]["attempt_id"] == str(attempt["attempt_id"])
        authority = _authority(offer["authority"])

        def post(path, body):
            return client.post(
                f"/v1/attempts/{authority['attempt_id']}{path}",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "X-Callback-Id": str(new_uuid7()),
                },
                json=body,
            )

        claim = post("/claim", {"authority": authority})
        assert claim.status_code == 200, claim.text
        container_id = "f" * 64
        started = post(
            "/start",
            {
                "authority": authority,
                "startup_nonce": str(attempt["startup_nonce"]),
                "executor_operation_sequence": 1,
                "container": {"container_id": container_id, "runtime_identity_digest": DIGEST},
            },
        )
        assert started.status_code == 200, started.text
        worker_service = client.app.state.services.worker
        complete, payload_hash, nonce = _prepare_completion(
            client, engine, credential, authority, "rem-obs02"
        )
        assert worker_service.complete_attempt(
            credential=credential,
            attempt_id=UUID(authority["attempt_id"]),
            callback_id=new_uuid7(),
            payload_hash=payload_hash,
            request=complete,
        )["accepted"]
        cleanup, cleanup_hash = _cleanup_request(authority, nonce, container_id)
        assert worker_service.report_cleanup(
            worker_id=UUID(WORKER_ID),
            credential=credential,
            attempt_id=UUID(authority["attempt_id"]),
            callback_id=new_uuid7(),
            payload_hash=cleanup_hash,
            request=cleanup,
        )["verified"]

        job = _job(engine, job_id)
        assert (job["state"], job["retry_count"]) == ("SUCCEEDED", 0)
        assert _events(engine, job_id, "ATTEMPT_LOST") == []
        assert [row["state"] for row in _attempts(engine, job_id)] == ["SUCCEEDED"]
        assert _allocation_states(engine) == ["RELEASED"]

        # Nothing is left to offer; once every allocation is released the freeze is legal
        # and WRITE_FROZEN keeps rejecting poll as before.
        empty = poll()
        assert empty.status_code == 200 and empty.json()["offer"] is None, empty.text
        frozen = set_mode("WRITE_FROZEN")
        assert frozen.status_code == 200, frozen.text
        rejected = poll()
        assert rejected.status_code == 409 and rejected.json()["code"] == "state_conflict"


def test_admission_off_creates_no_new_offer_until_normal(migrated_postgres_engine, tmp_path):
    from tests.api.test_http_contract import _client
    from tests.integration.test_coordinator_b11 import seed_dispatchable

    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as browser:
        _, _, (job_id,) = seed_dispatchable(engine, count=1)
        before = _job(engine, job_id)
        set_mode = _admin_browser(browser, "rem-obs02-dispatch")
        off = set_mode("ADMISSION_OFF")
        assert off.status_code == 200, off.text
        service, epoch = _leader(engine)
        for _ in range(3):
            service.tick(epoch)
        assert _attempts(engine, job_id) == []
        assert _allocation_states(engine) == []
        queued = _job(engine, job_id)
        assert (queued["state"], queued["job_fence"], queued["retry_count"]) == (
            "QUEUED",
            before["job_fence"],
            before["retry_count"],
        )

        normal = set_mode("NORMAL")
        assert normal.status_code == 200, normal.text
        service.tick(epoch)
        (attempt,) = _attempts(engine, job_id)
        assert attempt["state"] == "CREATED"
        assert _job(engine, job_id)["state"] == "DISPATCHING"
        assert _allocation_states(engine) == ["HELD"]
