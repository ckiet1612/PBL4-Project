"""B15 admin worker control and recovery reads over PostgreSQL.

drain/disable/enable follow the control pipeline (idempotency replay before If-Match,
428/412, WRITE_FROZEN 409); disable fences live attempts and leaves allocations
QUARANTINED until verified cleanup. Docker runs live in tests/docker.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.integration.test_checkpoint_restore_b14 import _next_attempt
from tests.integration.test_completion_race_b11 import _prepare_completion
from tests.integration.test_control_b15 import (
    MS_TIMESTAMP,
    REASON,
    Control,
    _events,
    _expire_lease,
    _renew,
    _set_mode,
)
from tests.integration.test_retry_b14 import _job, _leader
from tests.integration.test_worker_api_b10 import WORKER_ID, _inventory
from tests.integration.test_worker_authority_b10 import DIGEST

pytestmark = pytest.mark.postgres


def _worker_row(engine):
    with engine.connect() as connection:
        return (
            connection.execute(select(s.workers).where(s.workers.c.worker_id == UUID(WORKER_ID)))
            .mappings()
            .one()
        )


def _admin(control, method, path, *, if_match="current", key=None, body=None, **params):
    headers = dict(control.headers)
    if method == "post":
        if if_match == "current":
            if_match = f'"v{_worker_row(control.engine)["version"]}"'
        if if_match is not None:
            headers["If-Match"] = if_match
        headers["Idempotency-Key"] = key or f"b15-admin-worker-{new_uuid7()}"
        return control.client.post(
            f"/v1/admin{path}",
            headers=headers,
            json=body if body is not None else {"reason": REASON},
        )
    return control.client.get(f"/v1/admin{path}", headers=headers, params=params)


def _heartbeat(control, inventory=None):
    return control.worker_client.post(
        f"/v1/workers/{WORKER_ID}/heartbeat",
        headers={
            "Authorization": f"Bearer {control.job.credential}",
            "X-Callback-Id": str(new_uuid7()),
        },
        json={
            "worker_incarnation_id": control.job.authority["worker_incarnation_id"],
            "observed_health": "READY",
            "reconcile_complete": True,
            "inventory": inventory or _inventory(),
            "observed_containers": [],
        },
    )


def _reconcile(control):
    return control.worker_client.get(
        f"/v1/workers/{WORKER_ID}/reconciliation?page_size=100",
        headers={
            "Authorization": f"Bearer {control.job.credential}",
            "X-Worker-Incarnation-Id": control.job.authority["worker_incarnation_id"],
        },
    )


def _admin_audits(engine, action):
    with engine.connect() as connection:
        return (
            connection.execute(select(s.audit_records).where(s.audit_records.c.action == action))
            .mappings()
            .all()
        )


def _window():
    now = datetime.now(UTC)
    return (
        (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    )


def test_get_worker_has_etag_inventory_and_millisecond_timestamps(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-admin-get") as control:
        control.login("admin")
        response = _admin(control, "get", f"/workers/{WORKER_ID}")
        assert response.status_code == 200, response.text
        body = response.json()
        row = _worker_row(engine)
        assert response.headers["ETag"] == f'"v{row["version"]}"' == f'"v{body["version"]}"'
        assert (body["worker_id"], body["health"], body["admin_state"]) == (
            WORKER_ID,
            "READY",
            "ENABLED",
        )
        assert body["current_incarnation_id"] == control.job.authority["worker_incarnation_id"]
        expected = _inventory()
        inventory = body["inventory"]
        assert inventory["allocatable"] == expected["allocatable"]
        assert inventory["runtime"] == expected["runtime"]
        assert inventory["images"] == expected["images"]
        assert inventory["gpu_devices"] == expected["gpu_devices"]
        for value in (body["last_heartbeat_at"], body["ready_at"], inventory["discovered_at"]):
            assert MS_TIMESTAMP.match(value), value

        listed = _admin(control, "get", "/workers")
        assert listed.status_code == 200, listed.text
        assert listed.json()["items"] == [body]
        assert listed.json()["page"] == {"next_cursor": None, "page_size": 50}
        assert _admin_audits(engine, "admin.worker.get")
        assert _admin_audits(engine, "admin.worker.list")

        missing = _admin(control, "get", f"/workers/{new_uuid7()}")
        assert missing.status_code == 404
        control.login("member")
        assert _admin(control, "get", f"/workers/{WORKER_ID}").status_code == 403
        assert _admin(control, "get", "/workers").status_code == 403


def test_drain_keeps_running_attempts_and_enable_restores_dispatch(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-admin-drain") as control:
        control.login("admin")
        before = _worker_row(engine)
        key = f"b15-drain-{new_uuid7()}"
        drained = _admin(control, "post", f"/workers/{WORKER_ID}/drain", key=key)
        assert drained.status_code == 202, drained.text
        body = drained.json()
        assert body["admin_state"] == "DRAINING"
        assert body["version"] == before["version"] + 1
        assert drained.headers["ETag"] == f'"v{body["version"]}"'
        # Replay is answered before If-Match, even with a stale precondition.
        replay = _admin(control, "post", f"/workers/{WORKER_ID}/drain", key=key, if_match='"v999"')
        assert (replay.status_code, replay.json()) == (202, body)
        again = _admin(control, "post", f"/workers/{WORKER_ID}/drain")
        assert again.status_code == 409 and again.json()["code"] == "state_conflict"
        # Existing attempts continue: authority is untouched and renew still works.
        rows = control.authority_rows()
        assert rows["lease"]["revoked_at"] is None and rows["allocation"]["state"] == "HELD"
        assert _renew(control).status_code == 200
        assert _job(engine, control.job.job_id)["state"] == "RUNNING"
        audits = _admin_audits(engine, "admin.worker.drain")
        assert [(a["target_id"], a["reason"]) for a in audits] == [(WORKER_ID, REASON)]

        # The agent's periodic reconciliation covers the attempt's allocation.
        assert _reconcile(control).status_code == 200
        enabled = _admin(control, "post", f"/workers/{WORKER_ID}/enable")
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["admin_state"] == "ENABLED"
        assert enabled.headers["ETag"] == f'"v{enabled.json()["version"]}"'
        repeat = _admin(control, "post", f"/workers/{WORKER_ID}/enable")
        assert repeat.status_code == 409


def test_worker_mutation_preconditions_authorization_and_frozen_mode(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-admin-pre") as control:
        path = f"/workers/{WORKER_ID}/drain"
        control.login("member")
        assert _admin(control, "post", path).status_code == 403
        control.login("admin")
        missing = _admin(control, "post", path, if_match=None)
        assert missing.status_code == 428
        stale = _admin(control, "post", path, if_match='"v999"')
        assert stale.status_code == 412 and stale.json()["code"] == "version_conflict"
        assert _admin(control, "post", path, body={"reason": ""}).status_code == 422
        unknown = _admin(control, "post", f"/workers/{new_uuid7()}/drain", if_match='"v1"')
        assert unknown.status_code == 404
        _set_mode(engine, "WRITE_FROZEN")
        for operation in ("drain", "disable", "enable"):
            frozen = _admin(control, "post", f"/workers/{WORKER_ID}/{operation}")
            assert frozen.status_code == 409, operation
        _set_mode(engine, "ADMISSION_OFF")
        assert _admin(control, "post", path).status_code == 202
        assert _worker_row(engine)["admin_state"] == "DRAINING"


def test_disable_fences_the_running_attempt_and_cleanup_releases_it(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-admin-disable") as control:
        control.counters(outstanding=1, active=1)
        control.login("admin")
        disabled = _admin(control, "post", f"/workers/{WORKER_ID}/disable")
        assert disabled.status_code == 202, disabled.text
        assert disabled.json()["admin_state"] == "DISABLED"
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["job_fence"], job["desired_state"]) == (
            "RECOVERING",
            2,
            "RUNNING",
        )
        rows = control.authority_rows()
        assert (
            rows["attempt"]["state"],
            rows["attempt"]["failure_class"],
            rows["attempt"]["failure_reason"],
        ) == ("STOPPING", "INFRASTRUCTURE", "WORKER_DISABLED")
        assert rows["lease"]["revoked_at"] is not None
        assert rows["lease"]["revoke_reason"] == "WORKER_DISABLED"
        assert rows["grant"]["ended_at"] is not None
        # Quarantined capacity stays charged until verified cleanup.
        assert rows["allocation"]["state"] == "QUARANTINED"
        assert control.counter_values()["GLOBAL"] == (1, 1)
        (fenced,) = _events(engine, control.job.job_id, "ATTEMPT_FENCED")
        assert (fenced["reason"], fenced["actor_type"]) == ("WORKER_DISABLED", "ADMIN")
        # The free-text reason stays in the audit record only.
        assert REASON not in {e["reason"] for e in _events(engine, control.job.job_id, None)}
        assert [a["reason"] for a in _admin_audits(engine, "admin.worker.disable")] == [REASON]

        # SM:59 (B15-R30): the fenced authority is stale.
        renew = _renew(control)
        assert renew.status_code == 409 and renew.json()["code"] == "stale_authority"
        # A disabled worker never advertises READY and cannot be enabled before cleanup.
        assert _heartbeat(control).status_code == 200
        assert _worker_row(engine)["health"] != "READY"
        blocked = _admin(control, "post", f"/workers/{WORKER_ID}/enable")
        assert blocked.status_code == 409
        assert _admin(control, "post", f"/workers/{WORKER_ID}/disable").status_code == 409

        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        assert control.authority_rows()["allocation"]["state"] == "RELEASED"
        job = _job(engine, control.job.job_id)
        assert job["state"] == "RETRY_WAIT"
        assert control.authority_rows()["attempt"]["state"] == "FAILED"

        assert _heartbeat(control).status_code == 200
        enabled = _admin(control, "post", f"/workers/{WORKER_ID}/enable")
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["admin_state"] == "ENABLED"
        # Enable never revives the fenced attempt; the next heartbeat restores READY.
        assert control.authority_rows()["attempt"]["state"] == "FAILED"
        assert _heartbeat(control).status_code == 200
        assert _worker_row(engine)["health"] == "READY"


def test_steady_heartbeats_keep_the_worker_etag(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-admin-etag") as control:
        control.login("admin")
        assert _heartbeat(control).status_code == 200
        before = _worker_row(engine)
        etag = f'"v{before["version"]}"'
        for _ in range(3):
            assert _heartbeat(control).status_code == 200
        after = _worker_row(engine)
        # B15-R08: a heartbeat timestamp alone is not a visible change (contracts.md:100).
        assert after["version"] == before["version"]
        assert after["last_heartbeat_at"] > before["last_heartbeat_at"]
        drained = _admin(control, "post", f"/workers/{WORKER_ID}/drain", if_match=etag)
        assert drained.status_code == 202, drained.text
        # A visible health change still moves the ETag.
        with engine.begin() as connection:
            connection.execute(
                update(s.workers).values(last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=1))
            )
        version = _worker_row(engine)["version"]
        assert control.worker_client.app.state.services.worker.sweep_health() == 1
        assert _worker_row(engine)["version"] == version + 1


def test_rediscovered_inventory_keeps_the_worker_etag(migrated_postgres_engine, tmp_path):
    # B18-R22: the agent re-discovers its inventory on every heartbeat, so `discovered_at`
    # always moves. That timestamp alone is not a visible change (contracts.md:100): no new
    # inventory version and no new worker ETag, or every admin action from a page older
    # than one heartbeat meets 412.
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b18-inventory-etag") as control:
        control.login("admin")

        def inventory(second: int, cpu_millis: int = 6_000) -> dict:
            value = _inventory()
            value["discovered_at"] = f"2026-10-01T00:00:{second:02d}.000Z"
            value["allocatable"]["cpu_millis"] = cpu_millis
            return value

        assert _heartbeat(control, inventory(0)).status_code == 200
        before = _worker_row(engine)

        def inventory_rows() -> int:
            with engine.connect() as connection:
                return connection.execute(
                    select(func.count()).select_from(s.worker_inventories)
                ).scalar_one()

        rows = inventory_rows()
        for second in (5, 10, 15):
            assert _heartbeat(control, inventory(second)).status_code == 200
        after = _worker_row(engine)
        assert after["version"] == before["version"]
        assert after["current_inventory_version"] == before["current_inventory_version"]
        assert inventory_rows() == rows
        drained = _admin(
            control, "post", f"/workers/{WORKER_ID}/drain", if_match=f'"v{before["version"]}"'
        )
        assert drained.status_code == 202, drained.text

        # A changed inventory is still a new version and a visible change.
        version = _worker_row(engine)["version"]
        assert _heartbeat(control, inventory(20, cpu_millis=5_000)).status_code == 200
        changed = _worker_row(engine)
        assert changed["current_inventory_version"] == after["current_inventory_version"] + 1
        assert changed["version"] == version + 1
        assert inventory_rows() == rows + 1


def test_enable_requires_a_fresh_reconciled_heartbeat(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-admin-enable") as control:
        control.login("admin")
        assert _admin(control, "post", f"/workers/{WORKER_ID}/drain").status_code == 202
        with engine.begin() as connection:
            connection.execute(
                update(s.workers).values(last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=5))
            )
        stale = _admin(control, "post", f"/workers/{WORKER_ID}/enable")
        assert stale.status_code == 409 and stale.json()["code"] == "state_conflict"
        with engine.begin() as connection:
            connection.execute(update(s.worker_incarnations).values(reconciliation_drained=False))
        assert _heartbeat(control).status_code == 200
        unreconciled = _admin(control, "post", f"/workers/{WORKER_ID}/enable")
        assert unreconciled.status_code == 409
        assert _worker_row(engine)["admin_state"] == "DRAINING"


def test_enable_of_a_disabled_worker_requires_a_heartbeat_that_passed_readiness(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-admin-enable-disabled") as control:
        control.counters(outstanding=1, active=1)
        control.login("admin")
        assert _admin(control, "post", f"/workers/{WORKER_ID}/disable").status_code == 202
        assert control.cleanup().status_code == 200
        worker = control.worker_client.app.state.services.worker

        def broken_storage():
            raise OSError("storage is unavailable")

        # B15-R07: storage fails while DISABLED; the heartbeat still answers but the
        # worker has not passed the READY checks, so enable is refused.
        worker._storage_readiness = broken_storage
        assert _heartbeat(control).status_code == 200
        refused = _admin(control, "post", f"/workers/{WORKER_ID}/enable")
        assert refused.status_code == 409, refused.text
        assert _worker_row(engine)["admin_state"] == "DISABLED"

        worker._storage_readiness = lambda: None
        assert _heartbeat(control).status_code == 200
        assert _worker_row(engine)["health"] == "STARTING"
        # Reconciliation is rechecked at enable even after a passing heartbeat.
        with engine.begin() as connection:
            connection.execute(update(s.worker_incarnations).values(reconciliation_drained=False))
        unreconciled = _admin(control, "post", f"/workers/{WORKER_ID}/enable")
        assert unreconciled.status_code == 409, unreconciled.text
        assert "reconciled" in unreconciled.json()["message"]
        with engine.begin() as connection:
            connection.execute(update(s.worker_incarnations).values(reconciliation_drained=True))
        enabled = _admin(control, "post", f"/workers/{WORKER_ID}/enable")
        assert enabled.status_code == 200, enabled.text


def test_disable_revokes_a_terminal_jobs_leftover_lease_without_a_fence(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-admin-terminal") as control:
        _prepare_completion(
            control.worker_client, engine, control.job.credential, control.job.authority, "b15-at"
        )
        with engine.begin() as connection:
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == control.job.job_id)
                .values(state="SUCCEEDED", terminal_at=datetime.now(UTC))
            )
        before = _job(engine, control.job.job_id)
        control.login("admin")
        assert _admin(control, "post", f"/workers/{WORKER_ID}/disable").status_code == 202
        job = _job(engine, control.job.job_id)
        # L1: the appended event does not bump the terminal job's version (ETag).
        assert (job["state"], job["job_fence"], job["version"]) == (
            "SUCCEEDED",
            1,
            before["version"],
        )
        assert job["event_sequence"] == before["event_sequence"] + 1
        rows = control.authority_rows()
        assert rows["attempt"]["state"] == "RUNNING"
        assert rows["lease"]["revoke_reason"] == "WORKER_DISABLED"
        assert rows["allocation"]["state"] == "QUARANTINED"
        (revoked,) = _events(engine, control.job.job_id, "LEASE_REVOKED")
        assert revoked["reason"] == "WORKER_DISABLED"
        # L2: the leftover authority can no longer publish through its reservation.
        with engine.connect() as connection:
            states = (
                connection.execute(
                    select(s.result_reservations.c.state).where(
                        s.result_reservations.c.attempt_id
                        == UUID(control.job.authority["attempt_id"])
                    )
                )
                .scalars()
                .all()
            )
        assert states == ["ABANDONED"]


def test_disable_and_reaper_race_fence_the_attempt_once(migrated_postgres_engine, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-admin-race") as control:
        control.login("admin")
        service, epoch = _leader(engine)
        _expire_lease(engine, control.job.authority["lease_id"])
        if_match = f'"v{_worker_row(engine)["version"]}"'
        with ThreadPoolExecutor(max_workers=2) as pool:
            reaped = pool.submit(service.reap_leases, epoch)
            disabled = pool.submit(
                _admin, control, "post", f"/workers/{WORKER_ID}/disable", if_match=if_match
            )
            reaped, disabled = reaped.result(), disabled.result()
        assert disabled.status_code == 202, disabled.text
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["job_fence"]) == ("RECOVERING", 2)
        lost = _events(engine, control.job.job_id, "ATTEMPT_LOST")
        fenced = _events(engine, control.job.job_id, "ATTEMPT_FENCED")
        assert len(lost) + len(fenced) == 1
        assert reaped == len(lost)


def test_admin_allocations_and_recovery_events_are_bounded_signed_pages(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-admin-reads") as control:
        first = control.job.authority["allocation_id"]
        _next_attempt(control.job)
        second = control.job.authority["allocation_id"]
        control.login("admin")

        held = _admin(control, "get", "/allocations", state="HELD")
        assert held.status_code == 200, held.text
        (item,) = held.json()["items"]
        assert (item["allocation_id"], item["state"], item["gpu_uuids"]) == (second, "HELD", [])
        assert item["job_id"] == str(control.job.job_id)
        assert set(item["resources"]) == {"cpu_millis", "memory_bytes", "gpu_count"}
        assert MS_TIMESTAMP.match(item["held_at"])
        page = _admin(control, "get", "/allocations", page_size=1)
        assert [i["allocation_id"] for i in page.json()["items"]] == [second]
        cursor = page.json()["page"]["next_cursor"]
        rest = _admin(control, "get", "/allocations", page_size=1, cursor=cursor)
        assert [i["allocation_id"] for i in rest.json()["items"]] == [first]
        assert rest.json()["items"][0]["state"] == "RELEASED"
        assert rest.json()["page"]["next_cursor"] is None
        # The cursor is bound to its filter.
        crossed = _admin(control, "get", "/allocations", state="HELD", cursor=cursor)
        assert crossed.status_code == 400 and crossed.json()["code"] == "invalid_cursor"
        assert _admin(control, "get", "/allocations", state="GONE").status_code == 400
        assert _admin(control, "get", "/allocations", page_size=101).status_code == 400

        assert _admin(control, "post", f"/workers/{WORKER_ID}/disable").status_code == 202
        start, end = _window()
        events = _admin(control, "get", "/recovery-events", **{"from": start, "to": end})
        assert events.status_code == 200, events.text
        (fenced,) = events.json()["items"]
        assert (fenced["type"], fenced["reason"], fenced["actor_type"]) == (
            "ATTEMPT_FENCED",
            "WORKER_DISABLED",
            "ADMIN",
        )
        assert fenced["job_id"] == str(control.job.job_id)
        assert MS_TIMESTAMP.match(fenced["created_at"])
        # Non-recovery events (e.g. the disable's own audit trail) are not listed.
        assert set(fenced) == {
            "event_id",
            "tenant_id",
            "job_id",
            "sequence",
            "type",
            "reason",
            "actor_type",
            "created_at",
        }
        empty = _admin(control, "get", "/recovery-events", **{"from": start, "to": start})
        assert empty.json()["items"] == []
        inverted = _admin(control, "get", "/recovery-events", **{"from": end, "to": start})
        assert inverted.status_code == 400
        assert _admin(control, "get", "/recovery-events").status_code == 400
        forged = _admin(
            control, "get", "/recovery-events", cursor="x" * 32, **{"from": start, "to": end}
        )
        assert forged.status_code == 400 and forged.json()["code"] == "invalid_cursor"
        assert _admin_audits(engine, "admin.allocation.list")
        assert _admin_audits(engine, "admin.recovery_event.list")
        control.login("member")
        assert _admin(control, "get", "/allocations").status_code == 403
        assert (
            _admin(control, "get", "/recovery-events", **{"from": start, "to": end}).status_code
            == 403
        )


def test_drain_keeps_a_committed_offer_pollable_until_it_succeeds(
    migrated_postgres_engine, tmp_path
):
    """An offer committed before drain is existing work: poll must return it (B15-R22)."""
    from tests.api.test_http_contract import _client
    from tests.integration._factories import seed_job
    from tests.integration.test_completion_race_b11 import (
        _authority,
        _cleanup_request,
        _prepare_completion,
    )
    from tests.integration.test_control_b15 import PASSWORD, _reconcile_and_heartbeat
    from tests.integration.test_coordinator_b11 import seed_dispatchable
    from tests.integration.test_jobs_b08 import _submit_body
    from tests.integration.test_worker_api_b10 import _bootstrap_worker, _create_incarnation

    engine = migrated_postgres_engine
    users = tmp_path / "users"
    users.mkdir()
    with _client(engine, tmp_path) as client, _client(engine, users) as browser:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b15-drain-offer-incarnation"
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
        with engine.connect() as connection:
            attempt = connection.execute(select(s.attempts)).mappings().one()
        assert attempt["state"] == "CREATED"
        assert _reconcile_and_heartbeat(client, engine, credential, incarnation, []) == "READY"

        bootstrap = browser.post(
            "/v1/internal/admin-bootstrap",
            headers={
                "X-Nexa-Bootstrap-Secret": "b" * 32,
                "Idempotency-Key": "b15-drain-offer-admin-key",
            },
            json={
                "username": "b15-drain-offer@example.test",
                "display_name": "B15",
                "password": PASSWORD,
            },
        )
        assert bootstrap.status_code == 201, bootstrap.text
        login = browser.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={"username": "b15-drain-offer@example.test", "password": PASSWORD},
        )
        assert login.status_code == 200, login.text
        drained = browser.post(
            f"/v1/admin/workers/{WORKER_ID}/drain",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": login.json()["csrf_token"],
                "If-Match": f'"v{_worker_row(engine)["version"]}"',
                "Idempotency-Key": "b15-drain-offer-drain",
            },
            json={"reason": REASON},
        )
        assert drained.status_code == 202, drained.text

        poll = client.post(
            f"/v1/workers/{WORKER_ID}/poll",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Worker-Incarnation-Id": incarnation_id,
            },
            json={"worker_incarnation_id": incarnation_id},
        )
        assert poll.status_code == 200, poll.text
        offer = poll.json()["offer"]
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
        container_id = "e" * 64
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
            client, engine, credential, authority, "drain"
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
        assert _worker_row(engine)["admin_state"] == "DRAINING"
        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(s.allocations.c.state).where(
                        s.allocations.c.attempt_id == attempt["attempt_id"]
                    )
                ).scalar_one()
                == "RELEASED"
            )


def test_disable_fences_several_offers_in_job_order(migrated_postgres_engine, tmp_path):
    """Disable locks each job in job_id order: two CREATED offers are fenced and a
    terminal job's leftover lease is revoked, each once (lock-order deadlock guard)."""
    from concurrent.futures import ThreadPoolExecutor

    from tests.api.test_http_contract import _client
    from tests.integration._factories import seed_job
    from tests.integration.test_control_b15 import PASSWORD, _await_lock_waiters
    from tests.integration.test_coordinator_b11 import seed_dispatchable
    from tests.integration.test_jobs_b08 import _submit_body
    from tests.integration.test_worker_api_b10 import _bootstrap_worker, _create_incarnation

    engine = migrated_postgres_engine
    users = tmp_path / "users"
    users.mkdir()
    with _client(engine, tmp_path) as client, _client(engine, users) as browser:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b15-multi-disable-incarnation"
        )
        graph, _, _ = seed_dispatchable(
            engine,
            count=0,
            template_id="cpu-iterative",
            existing_worker=(UUID(WORKER_ID), UUID(incarnation["worker_incarnation_id"])),
        )
        with engine.begin() as connection:
            spec = _submit_body(graph["artifact_id"])["spec"]
            spec["resources"]["memory_bytes"] = 1_073_741_824
            for _ in range(3):
                seed_job(connection, graph, canonical_spec=spec)
            connection.execute(update(s.jobs).values(eligible_since=func.clock_timestamp()))
            connection.execute(update(s.admission_counters).values(outstanding=3))
            connection.execute(
                update(s.tenant_policies).values(tenant_active_limit=3, user_active_limit=3)
            )
        service, epoch = _leader(engine)
        for _ in range(3):
            service.tick(epoch)
        with engine.connect() as connection:
            offers = connection.execute(
                select(s.attempts.c.job_id, s.attempts.c.state).order_by(s.attempts.c.job_id)
            ).all()
        assert [state for _, state in offers] == ["CREATED"] * 3
        first, second, terminal = (job_id for job_id, _ in offers)
        with engine.begin() as connection:
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == terminal)
                .values(state="SUCCEEDED", terminal_at=func.clock_timestamp())
            )
        before = {job_id: _job(engine, job_id) for job_id in (first, second, terminal)}

        bootstrap = browser.post(
            "/v1/internal/admin-bootstrap",
            headers={
                "X-Nexa-Bootstrap-Secret": "b" * 32,
                "Idempotency-Key": "b15-multi-disable-admin-key",
            },
            json={
                "username": "b15-multi-disable@example.test",
                "display_name": "B15",
                "password": PASSWORD,
            },
        )
        assert bootstrap.status_code == 201, bootstrap.text
        login = browser.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={"username": "b15-multi-disable@example.test", "password": PASSWORD},
        )
        assert login.status_code == 200, login.text
        headers = {
            "Origin": "https://nexa.test",
            "X-CSRF-Token": login.json()["csrf_token"],
            "If-Match": f'"v{_worker_row(engine)["version"]}"',
            "Idempotency-Key": "b15-multi-disable-disable",
        }

        # Hold the last job's row: disable must already hold the earlier jobs' rows.
        # The holder closes (and rolls back) before the pool joins, even on failure.
        with ThreadPoolExecutor(max_workers=1) as pool, engine.connect() as holder:
            holding = holder.begin()
            holder.execute(select(s.jobs).where(s.jobs.c.job_id == terminal).with_for_update())
            disabled = pool.submit(
                browser.post,
                f"/v1/admin/workers/{WORKER_ID}/disable",
                headers=headers,
                json={"reason": REASON},
            )
            _await_lock_waiters(engine, 1)
            for job_id in (first, second):
                with engine.connect() as probe, pytest.raises(Exception) as locked:
                    probe.execute(
                        select(s.jobs).where(s.jobs.c.job_id == job_id).with_for_update(nowait=True)
                    )
                assert "lock" in str(locked.value).lower()
            holding.rollback()
            disabled = disabled.result()
        assert disabled.status_code == 202, disabled.text

        for job_id in (first, second):
            job = _job(engine, job_id)
            assert (job["state"], job["job_fence"]) == ("RECOVERING", 2)
            (fenced,) = _events(engine, job_id, "ATTEMPT_FENCED")
            assert fenced["reason"] == "WORKER_DISABLED"
        job = _job(engine, terminal)
        assert (job["state"], job["job_fence"], job["version"]) == (
            "SUCCEEDED",
            1,
            before[terminal]["version"],
        )
        (revoked,) = _events(engine, terminal, "LEASE_REVOKED")
        assert revoked["reason"] == "WORKER_DISABLED"
        with engine.connect() as connection:
            leases = connection.execute(
                select(s.attempt_leases.c.revoke_reason, s.allocations.c.state).join(
                    s.allocations,
                    s.allocations.c.allocation_id == s.attempt_leases.c.allocation_id,
                )
            ).all()
        assert sorted(leases) == [("WORKER_DISABLED", "QUARANTINED")] * 3
