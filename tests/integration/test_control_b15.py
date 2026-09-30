"""B15 control and recovery over PostgreSQL: CFP queueing, cancel, pause, resume, retry.

Database/HTTP integration only; Docker runs live in tests/docker/test_b15_control_recovery.py.
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, insert, select, update

from nexa.application.errors import ApplicationError
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration._factories import IMAGE_DIGEST, seed_authority, seed_job
from tests.integration.test_checkpoint_b14 import CheckpointFixture
from tests.integration.test_checkpoint_corruption_b14 import _committed_checkpoint
from tests.integration.test_checkpoint_restore_b14 import _next_attempt
from tests.integration.test_completion_race_b11 import _prepare_completion
from tests.integration.test_coordinator_b11 import seed_dispatchable
from tests.integration.test_retry_b14 import _job, _leader, _retry_wait, _schedule
from tests.integration.test_worker_api_b10 import _bootstrap_worker, _create_incarnation
from tests.integration.test_worker_authority_b10 import CONTAINER_ID, DIGEST, _running_authority

pytestmark = pytest.mark.postgres

CFP = "CHECKPOINT_FOR_PAUSE"


def test_checkpoint_for_pause_retry_is_promoted_and_dispatched_under_its_intent(
    migrated_postgres_engine,
):
    from nexa.domain.scheduling import Dispatch

    engine = migrated_postgres_engine
    _, _, (job_id,) = seed_dispatchable(engine)
    _retry_wait(
        engine,
        job_id,
        ready_in=timedelta(seconds=-1),
        desired_state="PAUSED",
        recovery_intent=CFP,
    )
    service, epoch = _leader(engine)

    assert service.promote_retries(epoch) == 1
    job = _job(engine, job_id)
    assert (job["state"], job["desired_state"], job["recovery_intent"]) == (
        "QUEUED",
        "PAUSED",
        CFP,
    )
    assert _schedule(engine, job_id)["closed_at"] is not None
    with engine.connect() as connection:
        assert connection.execute(select(s.queue_heads.c.candidate_job_id)).scalar_one() == job_id

    decision = service.tick(epoch)
    assert isinstance(decision, Dispatch) and decision.job_id == str(job_id)
    job = _job(engine, job_id)
    with engine.connect() as connection:
        attempt = connection.execute(select(s.attempts)).mappings().one()
    assert (job["state"], job["desired_state"], job["recovery_intent"]) == (
        "DISPATCHING",
        "PAUSED",
        CFP,
    )
    assert attempt["execution_intent"] == CFP
    assert attempt["job_fence"] == job["job_fence"] == 3


def _cfp_dispatched(engine, client, key):
    credential = _bootstrap_worker(client)
    incarnation = _create_incarnation(client, credential, nonce=str(new_uuid7()), key=key)
    value = _running_authority(engine, incarnation)
    authority = {k: v for k, v in value.items() if k not in {"grant_id", "job_id"}}
    with engine.begin() as connection:
        connection.execute(s.container_identities.delete())
        connection.execute(
            update(s.attempts).values(state="CREATED", started_at=None, execution_intent=CFP)
        )
        connection.execute(
            update(s.jobs).values(state="DISPATCHING", desired_state="PAUSED", recovery_intent=CFP)
        )
        nonce = connection.execute(select(s.attempts.c.startup_nonce)).scalar_one()
    return credential, value, authority, nonce


def test_checkpoint_for_pause_attempt_starts_into_pausing_and_never_reserves_a_result(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential, value, authority, nonce = _cfp_dispatched(
            engine, client, "b15-cfp-start-incarnation"
        )
        url = f"/v1/attempts/{value['attempt_id']}"

        def post(path, body):
            return client.post(
                url + path,
                headers={
                    "Authorization": f"Bearer {credential}",
                    "X-Callback-Id": str(new_uuid7()),
                },
                json=body,
            )

        claim = post("/claim", {"authority": authority})
        assert claim.status_code == 200, claim.text
        assert claim.json()["execution_context"]["execution_intent"] == CFP
        started = post(
            "/start",
            {
                "authority": authority,
                "startup_nonce": str(nonce),
                "executor_operation_sequence": 1,
                "container": {
                    "container_id": "a" * 64,
                    "runtime_identity_digest": "sha256:" + "b" * 64,
                },
            },
        )
        assert started.status_code == 200, started.text
        assert started.json()["job_state"] == "PAUSING"
        job = _job(engine, value["job_id"])
        assert (job["state"], job["desired_state"], job["recovery_intent"]) == (
            "PAUSING",
            "PAUSED",
            CFP,
        )

        renew = post("/renew", {"authority": authority, "progress_sequence": 0, "progress": None})
        assert renew.status_code == 200, renew.text
        result = post("/result-reservations", {"authority": authority})
        assert result.status_code == 409, result.text
        assert result.json()["code"] == "stale_authority"


PASSWORD = "b15-control-password"
REASON = "Operator note: stop — budget ✓"


class Control:
    """A RUNNING CheckpointFixture job owned by a MEMBER plus admin/member/outsider sessions.

    admin is the bootstrap SYSTEM_ADMIN with a TENANT_ADMIN membership; member owns the
    job; other is a MEMBER of the same tenant who does not own it.
    """

    def __init__(
        self,
        engine,
        tmp_path,
        *,
        label,
        checkpointable=True,
        restart_safe=True,
        template_id=None,
        template_values=None,
        input_media_type="application/json",
    ):
        self.engine = engine
        # Worker callbacks and browser sessions use separate clients: a cookie on the
        # worker client would make bearer authentication ambiguous.
        self._stack = ExitStack()
        self.worker_client = self._stack.enter_context(_client(engine, tmp_path))
        users = tmp_path / "users"
        users.mkdir()
        self.client = client = self._stack.enter_context(_client(engine, users))
        self.job = CheckpointFixture(
            engine,
            self.worker_client,
            label=label,
            checkpointable=checkpointable,
            restart_safe=restart_safe,
            template_id=template_id,
            template_values=template_values,
            input_media_type=input_media_type,
        )
        # Both apps must see the same checkpoint blobs (manual-retry verification).
        self.store = self.worker_client.app.state.services.artifact.store
        client.app.state.services.jobs.artifact_store = self.store
        self.tenant_id = self.job.graph["tenant_id"]
        self.label = label
        bootstrap = client.post(
            "/v1/internal/admin-bootstrap",
            headers={"X-Nexa-Bootstrap-Secret": "b" * 32, "Idempotency-Key": f"{label}-admin-key"},
            json={
                "username": f"{label}-admin@example.test",
                "display_name": "B15",
                "password": PASSWORD,
            },
        )
        assert bootstrap.status_code == 201, bootstrap.text
        self.users = {"admin": UUID(bootstrap.json()["user_id"])}
        self.login("admin")
        for name in ("member", "other"):
            created = client.post(
                "/v1/admin/users",
                headers={**self.headers, "Idempotency-Key": f"{label}-{name}-user-key"},
                json={
                    "username": f"{label}-{name}@example.test",
                    "display_name": name,
                    "password": PASSWORD,
                    "system_roles": [],
                },
            )
            assert created.status_code == 201, created.text
            self.users[name] = UUID(created.json()["user_id"])
        roles = {"admin": "TENANT_ADMIN", "member": "MEMBER", "other": "MEMBER"}
        with engine.begin() as connection:
            for name, role in roles.items():
                connection.execute(
                    insert(s.memberships).values(
                        tenant_id=self.tenant_id, user_id=self.users[name], role=role
                    )
                )
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.tenant_id == self.tenant_id)
                .values(submitter_user_id=self.users["member"])
            )
        self.graph = {**self.job.graph, "user_id": self.users["member"]}
        self.login("member")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._stack.close()

    def login(self, name):
        login = self.client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={"username": f"{self.label}-{name}@example.test", "password": PASSWORD},
        )
        assert login.status_code == 200, login.text
        self.headers = {"Origin": "https://nexa.test", "X-CSRF-Token": login.json()["csrf_token"]}

    def counters(self, *, outstanding, active):
        with self.engine.begin() as connection:
            for scope, scope_id in self._scopes():
                connection.execute(
                    insert(s.admission_counters).values(
                        scope_type=scope,
                        scope_id=scope_id,
                        outstanding=outstanding,
                        active_attempts=active,
                    )
                )

    def _scopes(self):
        return (
            ("GLOBAL", "global"),
            ("TENANT", str(self.tenant_id)),
            ("USER", f"{self.tenant_id}:{self.users['member']}"),
        )

    def counter_values(self):
        with self.engine.connect() as connection:
            return {
                scope: tuple(
                    connection.execute(
                        select(
                            s.admission_counters.c.outstanding,
                            s.admission_counters.c.active_attempts,
                        ).where(
                            s.admission_counters.c.scope_type == scope,
                            s.admission_counters.c.scope_id == scope_id,
                        )
                    ).one()
                )
                for scope, scope_id in self._scopes()
            }

    def seed(self, **changes):
        with self.engine.begin() as connection:
            job_id = seed_job(
                connection,
                self.graph,
                artifact_id=self.job.graph["artifact_id"],
                canonical_spec={"template_id": self.graph["template_id"]},
            )["job_id"]
            if changes:
                connection.execute(
                    update(s.jobs).where(s.jobs.c.job_id == job_id).values(**changes)
                )
        return job_id

    def control(self, operation, *, job_id=None, if_match="current", key=None, body=None):
        job_id = job_id or self.job.job_id
        if if_match == "current":
            if_match = f'"v{_job(self.engine, job_id)["version"]}"'
        headers = {
            **self.headers,
            "X-Nexa-Tenant-Id": str(self.tenant_id),
            "Idempotency-Key": key or f"b15-control-{new_uuid7()}",
        }
        if if_match is not None:
            headers["If-Match"] = if_match
        return self.client.post(
            f"/v1/jobs/{job_id}/{operation}",
            headers=headers,
            json=body if body is not None else {"reason": REASON},
        )

    def authority_rows(self):
        authority = self.job.authority
        with self.engine.connect() as connection:

            def one(table, column, value):
                return (
                    connection.execute(select(table).where(column == UUID(value))).mappings().one()
                )

            return {
                "attempt": one(s.attempts, s.attempts.c.attempt_id, authority["attempt_id"]),
                "lease": one(s.attempt_leases, s.attempt_leases.c.lease_id, authority["lease_id"]),
                "allocation": one(
                    s.allocations, s.allocations.c.allocation_id, authority["allocation_id"]
                ),
                "grant": one(
                    s.attempt_authority_grants,
                    s.attempt_authority_grants.c.grant_id,
                    str(self.job.grant_id),
                ),
            }

    def cleanup(self):
        nonce = self.authority_rows()["attempt"]["startup_nonce"]
        proof = {
            "proof_type": "CONTAINER_STOPPED",
            "startup_nonce": str(nonce),
            "executor_operation_sequence": 2,
            "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
            "stopped_at": datetime.now(UTC).isoformat(),
            "exit_code": 143,
            "inspection_checksum": "sha256:" + "c" * 64,
        }
        return self.job.post(
            "/cleanup",
            {**{k: v for k, v in self.job.authority.items() if k != "lease_id"}, "proof": proof},
        )


def _last_event(engine, job_id):
    with engine.connect() as connection:
        return (
            connection.execute(
                select(s.events)
                .where(s.events.c.job_id == job_id)
                .order_by(s.events.c.sequence.desc())
                .limit(1)
            )
            .mappings()
            .one()
        )


def _audits(engine, job_id, action):
    with engine.connect() as connection:
        return (
            connection.execute(
                select(s.audit_records).where(
                    s.audit_records.c.target_id == str(job_id),
                    s.audit_records.c.action == action,
                )
            )
            .mappings()
            .all()
        )


def _set_mode(engine, mode):
    with engine.begin() as connection:
        connection.execute(
            update(s.policy_versions)
            .where(s.policy_versions.c.is_current.is_(True))
            .values(operational_mode=mode)
        )


def test_cancel_queued_job_is_terminal_once_and_replays_before_if_match(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-cancel-queued") as control:
        control.counters(outstanding=2, active=1)
        queued = control.seed()
        with engine.begin() as connection:
            connection.execute(
                insert(s.reservations).values(
                    reservation_id=new_uuid7(),
                    tenant_id=control.tenant_id,
                    job_id=queued,
                    eligibility_since=func.now(),
                    policy_version=connection.execute(
                        select(s.policy_versions.c.policy_version).where(
                            s.policy_versions.c.is_current.is_(True)
                        )
                    ).scalar_one(),
                )
            )
        key = "b15-cancel-queued-key-0001"
        cancelled = control.control("cancel", job_id=queued, key=key, if_match='"v1"')
        assert cancelled.status_code == 202, cancelled.text
        assert cancelled.headers["ETag"] == '"v2"'
        body = cancelled.json()
        assert (body["state"], body["desired_state"], body["version"]) == (
            "CANCELLED",
            "CANCELLED",
            2,
        )
        job = _job(engine, queued)
        assert job["terminal_at"] is not None and job["recovery_intent"] is None
        assert control.counter_values() == {
            "GLOBAL": (1, 1),
            "TENANT": (1, 1),
            "USER": (1, 1),
        }
        event = _last_event(engine, queued)
        assert (event["event_type"], event["reason"], event["actor_type"]) == (
            "JOB_CANCELLED",
            "USER_CANCEL",
            "USER",
        )
        assert event["actor_id"] == str(control.users["member"])
        (audit,) = _audits(engine, queued, "job.cancel")
        assert (audit["reason"], audit["before_version"], audit["after_version"]) == (REASON, 1, 2)
        with engine.connect() as connection:
            reservation = connection.execute(select(s.reservations)).mappings().one()
            record = (
                connection.execute(
                    select(s.idempotency_records).where(
                        s.idempotency_records.c.operation_id == "cancelJob"
                    )
                )
                .mappings()
                .one()
            )
        assert reservation["invalidated_at"] is not None
        assert reservation["invalidation_reason"] == "job_cancelled"
        assert record["resource_id"] == queued
        assert record["expires_at"] >= job["terminal_at"] + timedelta(days=30)

        # Replay resolves before If-Match: missing or stale preconditions return the original.
        for if_match in (None, '"v9"'):
            replay = control.control("cancel", job_id=queued, key=key, if_match=if_match)
            assert replay.status_code == 202, replay.text
            assert replay.json() == body and replay.headers["ETag"] == '"v2"'
        changed = control.control(
            "cancel", job_id=queued, key=key, body={"reason": "different"}, if_match='"v2"'
        )
        assert changed.status_code == 409 and changed.json()["code"] == "idempotency_conflict"
        again = control.control("cancel", job_id=queued, if_match='"v2"')
        assert again.status_code == 409 and again.json()["code"] == "state_conflict"
        assert _job(engine, queued)["version"] == 2
        assert control.counter_values()["GLOBAL"] == (1, 1)


@pytest.mark.parametrize("state", ["RETRY_WAIT", "PAUSED"])
def test_cancel_waiting_or_paused_job_closes_schedule_and_clears_intent(
    migrated_postgres_engine, tmp_path, state
):
    engine = migrated_postgres_engine
    label = f"b15-cancel-{state.lower().replace('_', '-')}"
    with Control(engine, tmp_path, label=label) as control:
        control.counters(outstanding=2, active=1)
        job_id = control.seed()
        if state == "RETRY_WAIT":
            _retry_wait(
                engine,
                job_id,
                ready_in=timedelta(seconds=30),
                desired_state="PAUSED",
                recovery_intent=CFP,
            )
        else:
            with engine.begin() as connection:
                connection.execute(
                    update(s.jobs)
                    .where(s.jobs.c.job_id == job_id)
                    .values(state="PAUSED", desired_state="PAUSED")
                )
        response = control.control("cancel", job_id=job_id)
        assert response.status_code == 202, response.text
        job = _job(engine, job_id)
        assert (job["state"], job["desired_state"], job["recovery_intent"]) == (
            "CANCELLED",
            "CANCELLED",
            None,
        )
        if state == "RETRY_WAIT":
            assert _schedule(engine, job_id)["closed_at"] is not None
        assert control.counter_values()["USER"] == (1, 1)


def test_control_preconditions_authorization_and_frozen_mode(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-control-guards") as control:
        client = control.client
        control.counters(outstanding=2, active=1)
        queued = control.seed()
        missing = control.control("cancel", if_match=None)
        assert missing.status_code == 428 and missing.json()["code"] == "precondition_required"
        malformed = control.control("cancel", if_match="v1")
        assert malformed.status_code == 400
        stale = control.control("cancel", if_match='"v7"')
        assert stale.status_code == 412 and stale.json()["code"] == "version_conflict"
        for body, status in (
            ({"reason": ""}, 422),
            ({"reason": "x" * 257}, 422),
            ({"reason": "ok", "extra": 1}, 400),
            ({}, 422),
        ):
            invalid = control.control("cancel", body=body)
            assert invalid.status_code == status, (body, invalid.text)
        with engine.begin() as connection:
            outsider = seed_job(
                connection,
                {**control.graph},
                artifact_id=control.job.graph["artifact_id"],
                canonical_spec={"template_id": control.graph["template_id"]},
            )["job_id"]
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == outsider)
                .values(submitter_user_id=control.users["other"])
            )
        # A MEMBER cannot see another member's job; the tenant header must be a membership.
        foreign = control.control("cancel", job_id=outsider)
        assert foreign.status_code == 404 and foreign.json()["code"] == "resource_not_found"
        unknown = control.control("cancel", job_id=new_uuid7(), if_match='"v1"')
        assert unknown.status_code == 404
        response = client.post(
            f"/v1/jobs/{queued}/cancel",
            headers={
                **control.headers,
                "X-Nexa-Tenant-Id": str(new_uuid7()),
                "Idempotency-Key": "b15-guards-foreign-tenant",
                "If-Match": '"v1"',
            },
            json={"reason": REASON},
        )
        assert response.status_code == 403
        assert _job(engine, control.job.job_id)["version"] == 1

        # WRITE_FROZEN admits only SYSTEM_ADMIN browser sessions (B04), so the admin
        # stores the cancel whose replay must survive the freeze.
        control.login("admin")
        key = "b15-guards-frozen-replay"
        assert control.control("cancel", job_id=queued, key=key).status_code == 202
        _set_mode(engine, "WRITE_FROZEN")
        replay = control.control("cancel", job_id=queued, key=key, if_match=None)
        assert replay.status_code == 202, replay.text
        frozen = control.control("cancel")
        assert frozen.status_code == 409 and frozen.json()["code"] == "state_conflict"
        assert _job(engine, control.job.job_id)["state"] == "RUNNING"


def test_cancel_running_fences_authority_and_cleanup_cancels(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-cancel-running") as control:
        control.counters(outstanding=1, active=1)
        worker = control.worker_client.app.state.services.worker
        complete, payload_hash, _ = _prepare_completion(
            control.worker_client,
            engine,
            control.job.credential,
            control.job.authority,
            "b15-cancel",
        )
        # TENANT_ADMIN cancels a member's job; ADMISSION_OFF still permits control.
        control.login("admin")
        _set_mode(engine, "ADMISSION_OFF")
        response = control.control("cancel", if_match='"v1"')
        assert response.status_code == 202, response.text
        body = response.json()
        assert (body["state"], body["desired_state"], body["job_fence"]) == (
            "CANCELLING",
            "CANCELLED",
            2,
        )
        rows = control.authority_rows()
        assert rows["lease"]["revoked_at"] is not None
        assert rows["lease"]["revoke_reason"] == "USER_CANCEL"
        assert rows["grant"]["ended_at"] is not None
        assert rows["allocation"]["state"] == "QUARANTINED"
        assert (
            rows["attempt"]["state"],
            rows["attempt"]["failure_class"],
            rows["attempt"]["failure_reason"],
        ) == ("STOPPING", "USER_CANCEL", "USER_CANCEL")
        with engine.connect() as connection:
            reservation = connection.execute(select(s.result_reservations)).mappings().one()
        assert reservation["state"] == "ABANDONED"
        assert control.counter_values()["GLOBAL"] == (1, 1)
        assert _job(engine, control.job.job_id)["terminal_at"] is None
        assert _last_event(engine, control.job.job_id)["event_type"] == "CANCEL_REQUESTED"

        with pytest.raises(ApplicationError) as stale:
            worker.complete_attempt(
                credential=control.job.credential,
                attempt_id=UUID(control.job.authority["attempt_id"]),
                callback_id=new_uuid7(),
                payload_hash=payload_hash,
                request=complete,
            )
        assert (stale.value.status, stale.value.code) == (409, "stale_authority")
        renew = control.job.post(
            "/renew", {"authority": control.job.authority, "progress_sequence": 0, "progress": None}
        )
        assert renew.status_code == 409 and renew.json()["code"] == "stale_authority"
        repeat = control.control("cancel")
        assert repeat.status_code == 409 and repeat.json()["code"] == "state_conflict"

        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        job = _job(engine, control.job.job_id)
        assert job["state"] == "CANCELLED" and job["terminal_at"] is not None
        assert control.authority_rows()["attempt"]["state"] == "CANCELLED"
        assert control.counter_values()["GLOBAL"] == (0, 0)


def test_completion_committed_before_cancel_wins(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-complete-first") as control:
        control.counters(outstanding=1, active=1)
        worker = control.worker_client.app.state.services.worker
        complete, payload_hash, _ = _prepare_completion(
            control.worker_client,
            engine,
            control.job.credential,
            control.job.authority,
            "b15-first",
        )
        worker.complete_attempt(
            credential=control.job.credential,
            attempt_id=UUID(control.job.authority["attempt_id"]),
            callback_id=new_uuid7(),
            payload_hash=payload_hash,
            request=complete,
        )
        job = _job(engine, control.job.job_id)
        assert job["state"] == "SUCCEEDED" and job["terminal_at"] is not None
        response = control.control("cancel")
        assert response.status_code == 409 and response.json()["code"] == "state_conflict"
        assert _job(engine, control.job.job_id)["version"] == job["version"]
        assert control.counter_values()["GLOBAL"] == (0, 1)


def test_concurrent_cancel_and_complete_have_exactly_one_winner(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-cancel-race") as control:
        client = control.client
        control.counters(outstanding=1, active=1)
        worker = control.worker_client.app.state.services.worker
        complete, payload_hash, _ = _prepare_completion(
            control.worker_client, engine, control.job.credential, control.job.authority, "b15-race"
        )
        headers = {
            **control.headers,
            "X-Nexa-Tenant-Id": str(control.tenant_id),
            "Idempotency-Key": "b15-cancel-race-key-01",
            "If-Match": '"v1"',
        }

        def cancel():
            return client.post(
                f"/v1/jobs/{control.job.job_id}/cancel", headers=headers, json={"reason": REASON}
            ).status_code

        def finish():
            try:
                worker.complete_attempt(
                    credential=control.job.credential,
                    attempt_id=UUID(control.job.authority["attempt_id"]),
                    callback_id=new_uuid7(),
                    payload_hash=payload_hash,
                    request=complete,
                )
                return 200
            except ApplicationError as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(
                future.result() for future in (pool.submit(cancel), pool.submit(finish))
            )
        job = _job(engine, control.job.job_id)
        if job["state"] == "SUCCEEDED":
            # The committed completion bumped the version, so the If-Match check (412)
            # precedes the terminal-state guard.
            assert outcomes == [200, 412]
            assert control.counter_values()["GLOBAL"] == (0, 1)
        else:
            assert outcomes == [202, 409] and job["state"] == "CANCELLING"
            assert control.counter_values()["GLOBAL"] == (1, 1)


def test_another_worker_cannot_learn_that_a_job_was_cancelled(migrated_postgres_engine, tmp_path):
    from sqlalchemy.orm import Session

    from nexa.api.schemas import Authority
    from nexa.application.worker_service import WorkerService

    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-cancel-foreign") as control:
        control.counters(outstanding=1, active=1)
        other = new_uuid7()
        foreign = Authority.model_validate({**control.job.authority, "worker_id": str(other)})

        def rejection():
            with Session(engine) as session, pytest.raises(ApplicationError) as failure:
                WorkerService._authority_rows(session, foreign, worker_id=other)
            return failure.value.code, failure.value.message

        before = rejection()
        assert control.control("cancel").status_code == 202
        # F8: the cancel is reported only after the worker identity matches.
        assert rejection() == before == ("stale_authority", "Worker authority is stale")
        own = Authority.model_validate(control.job.authority)
        with Session(engine) as session, pytest.raises(ApplicationError) as failure:
            WorkerService._authority_rows(session, own, worker_id=own.worker_id)
        assert failure.value.message == "Job cancellation is committed"


def test_cancel_of_a_dispatching_offer_fences_it_before_any_claim(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-cancel-dispatching") as control:
        control.counters(outstanding=1, active=1)
        # A committed, unclaimed offer: CREATED attempt, live lease, HELD allocation.
        with engine.begin() as connection:
            connection.execute(s.container_identities.delete())
            connection.execute(
                update(s.attempts).values(state="CREATED", claimed_at=None, started_at=None)
            )
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == control.job.job_id)
                .values(state="DISPATCHING")
            )
        response = control.control("cancel")
        assert response.status_code == 202, response.text
        assert (response.json()["state"], response.json()["job_fence"]) == ("CANCELLING", 2)
        rows = control.authority_rows()
        assert rows["lease"]["revoke_reason"] == "USER_CANCEL"
        assert rows["grant"]["ended_at"] is not None
        assert (rows["attempt"]["state"], rows["allocation"]["state"]) == (
            "STOPPING",
            "QUARANTINED",
        )
        # The worker polls or claims too late: the fenced offer is stale.
        claim = control.job.post("/claim", {"authority": control.job.authority})
        assert claim.status_code == 409 and claim.json()["code"] == "stale_authority"
        assert control.counter_values()["GLOBAL"] == (1, 1)
        authority = {k: v for k, v in control.job.authority.items() if k != "lease_id"}
        cleaned = control.job.post(
            "/cleanup",
            {
                **authority,
                "proof": {
                    "proof_type": "NO_CONTAINER",
                    "startup_nonce": str(rows["attempt"]["startup_nonce"]),
                    "executor_operation_sequence": 1,
                    "tombstone_sequence": 1,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "inspection_checksum": "sha256:" + "a" * 64,
                },
            },
        )
        assert cleaned.status_code == 200, cleaned.text
        assert cleaned.json()["allocation_state"] == "RELEASED"
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["retry_count"], job["job_fence"]) == ("CANCELLED", 0, 2)
        assert control.authority_rows()["attempt"]["state"] == "CANCELLED"
        assert control.counter_values()["GLOBAL"] == (0, 0)


def test_cancel_recovering_job_waits_for_cleanup_without_new_fence(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-cancel-recovering") as control:
        control.counters(outstanding=1, active=1)
        failed = control.job.post(
            "/fail",
            {
                "authority": control.job.authority,
                "failure_class": "INFRASTRUCTURE",
                "reason_code": "EXECUTOR_UNAVAILABLE",
                "observation": {
                    "observation_type": "CONTAINER",
                    "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
                    "observed_at": datetime.now(UTC).isoformat(),
                    "exit_code": 137,
                    "oom_killed": False,
                    "runtime_limit_reached": False,
                },
            },
        )
        assert failed.status_code == 200, failed.text
        response = control.control("cancel")
        assert response.status_code == 202, response.text
        assert (response.json()["state"], response.json()["job_fence"]) == ("CANCELLING", 2)
        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["retry_count"]) == ("CANCELLED", 0)
        assert job["terminal_at"] is not None
        assert control.counter_values()["GLOBAL"] == (0, 0)


def test_cancel_of_a_reaped_job_keeps_the_lost_attempt_terminal(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-cancel-lost") as control:
        control.counters(outstanding=1, active=1)
        _expire_lease(engine, control.job.authority["lease_id"])
        service, epoch = _leader(engine)
        assert service.reap_leases(epoch) == 1
        assert control.authority_rows()["attempt"]["state"] == "LOST"
        response = control.control("cancel")
        assert response.status_code == 202, response.text
        assert (response.json()["state"], response.json()["job_fence"]) == ("CANCELLING", 2)
        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        assert _job(engine, control.job.job_id)["state"] == "CANCELLED"
        # F9 (B15-R34): LOST is terminal (SM:77, SM:80); SM:78 cancels only an active or
        # STOPPING Attempt, so the verified cleanup ends the job but keeps LOST.
        rows = control.authority_rows()
        assert (rows["attempt"]["state"], rows["allocation"]["state"]) == ("LOST", "RELEASED")
        assert rows["attempt"]["ended_at"] is not None
        assert control.counter_values()["GLOBAL"] == (0, 0)


def _expire_lease(engine, lease_id, *, seconds=1):
    with engine.begin() as connection:
        connection.execute(
            update(s.attempt_leases)
            .where(s.attempt_leases.c.lease_id == UUID(lease_id))
            .values(expires_at=func.now() - timedelta(seconds=seconds))
        )


def _events(engine, job_id, event_type):
    """Events of the job, of one type unless `event_type` is None, in sequence order."""
    query = select(s.events).where(s.events.c.job_id == job_id).order_by(s.events.c.sequence)
    if event_type is not None:
        query = query.where(s.events.c.event_type == event_type)
    with engine.connect() as connection:
        return connection.execute(query).mappings().all()


def test_reaper_moves_expired_running_lease_to_recovering_once(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-reap-running") as control:
        control.counters(outstanding=1, active=1)
        reserved = control.job.reserve()
        assert reserved.status_code == 201, reserved.text
        service, epoch = _leader(engine)
        # A live lease is never reaped.
        assert service.reap_leases(epoch) == 0
        _expire_lease(engine, control.job.authority["lease_id"])

        assert service.reap_leases(epoch) == 1
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["desired_state"], job["job_fence"]) == (
            "RECOVERING",
            "RUNNING",
            2,
        )
        rows = control.authority_rows()
        assert (
            rows["attempt"]["state"],
            rows["attempt"]["failure_class"],
            rows["attempt"]["failure_reason"],
        ) == ("LOST", "INFRASTRUCTURE", "LEASE_EXPIRED")
        assert rows["lease"]["revoke_reason"] == "LEASE_EXPIRED"
        assert rows["grant"]["ended_at"] is not None
        assert rows["allocation"]["state"] == "QUARANTINED"
        with engine.connect() as connection:
            checkpoint = connection.execute(select(s.checkpoint_reservations)).mappings().one()
        assert checkpoint["state"] == "ABANDONED"
        event = _last_event(engine, control.job.job_id)
        assert (event["event_type"], event["reason"], event["actor_type"]) == (
            "ATTEMPT_LOST",
            "LEASE_EXPIRED",
            "COORDINATOR",
        )
        # Quarantine keeps capacity and counters charged.
        assert control.counter_values()["GLOBAL"] == (1, 1)
        assert service.reap_leases(epoch) == 0
        assert len(_events(engine, control.job.job_id, "ATTEMPT_LOST")) == 1

        renew = control.job.post(
            "/renew", {"authority": control.job.authority, "progress_sequence": 0, "progress": None}
        )
        assert renew.status_code == 409, renew.text
        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["retry_count"]) == ("RETRY_WAIT", 1)
        attempt = control.authority_rows()["attempt"]
        # LOST is terminal for the attempt; cleanup only stamps its end.
        assert attempt["state"] == "LOST" and attempt["ended_at"] is not None
        assert control.counter_values()["GLOBAL"] == (1, 0)


def test_reaper_keeps_desired_paused_for_expired_pausing_lease(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-reap-pausing") as control:
        with engine.begin() as connection:
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == control.job.job_id)
                .values(state="PAUSING", desired_state="PAUSED")
            )
        _expire_lease(engine, control.job.authority["lease_id"])
        service, epoch = _leader(engine)
        assert service.reap_leases(epoch) == 1
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["desired_state"], job["job_fence"]) == (
            "RECOVERING",
            "PAUSED",
            2,
        )


def test_reaper_revokes_live_lease_of_succeeded_job_without_fence(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-reap-succeeded") as control:
        control.counters(outstanding=1, active=1)
        worker = control.worker_client.app.state.services.worker
        complete, payload_hash, _ = _prepare_completion(
            control.worker_client, engine, control.job.credential, control.job.authority, "b15-rs"
        )
        worker.complete_attempt(
            credential=control.job.credential,
            attempt_id=UUID(control.job.authority["attempt_id"]),
            callback_id=new_uuid7(),
            payload_hash=payload_hash,
            request=complete,
        )
        before = _job(engine, control.job.job_id)
        _expire_lease(engine, control.job.authority["lease_id"])
        service, epoch = _leader(engine)
        assert service.reap_leases(epoch) == 1
        job = _job(engine, control.job.job_id)
        # L1: the appended event does not bump the terminal job's version (ETag).
        assert (job["state"], job["job_fence"], job["version"], job["event_sequence"]) == (
            "SUCCEEDED",
            before["job_fence"],
            before["version"],
            before["event_sequence"] + 1,
        )
        rows = control.authority_rows()
        assert rows["attempt"]["state"] == "SUCCEEDED"
        assert rows["lease"]["revoke_reason"] == "LEASE_EXPIRED"
        assert rows["allocation"]["state"] == "QUARANTINED"
        event = _last_event(engine, control.job.job_id)
        assert (event["event_type"], event["reason"]) == ("LEASE_REVOKED", "LEASE_EXPIRED")
        assert control.counter_values()["GLOBAL"] == (0, 1)
        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        assert control.counter_values()["GLOBAL"] == (0, 0)


def test_reaper_abandons_the_leftover_result_reservation_of_a_terminal_job(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-reap-leftover-result") as control:
        control.counters(outstanding=1, active=1)
        _prepare_completion(
            control.worker_client, engine, control.job.credential, control.job.authority, "b15-lr"
        )
        with engine.begin() as connection:
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == control.job.job_id)
                .values(state="FAILED", terminal_at=func.clock_timestamp())
            )
        before = _job(engine, control.job.job_id)
        _expire_lease(engine, control.job.authority["lease_id"])
        service, epoch = _leader(engine)
        assert service.reap_leases(epoch) == 1
        assert _job(engine, control.job.job_id)["version"] == before["version"]
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


def test_reaper_writes_nothing_while_frozen_or_without_leadership(
    migrated_postgres_engine, tmp_path
):
    from nexa.coordinator.service import LeadershipLost

    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-reap-frozen") as control:
        _expire_lease(engine, control.job.authority["lease_id"])
        service, epoch = _leader(engine)
        before = _job(engine, control.job.job_id)
        with pytest.raises(LeadershipLost):
            service.reap_leases(epoch + 1)
        _set_mode(engine, "WRITE_FROZEN")
        assert service.reap_leases(epoch) == 0
        assert _job(engine, control.job.job_id)["version"] == before["version"]
        assert control.authority_rows()["lease"]["revoked_at"] is None
        _set_mode(engine, "NORMAL")
        assert service.reap_leases(epoch) == 1


def _between_probe_and_lock(monkeypatch, action):
    """Run `action` after the reaper's unlocked probe returned, before it locks the job."""
    from nexa.coordinator import reaper

    probed = []
    real_probe = reaper.expired_leases

    def probe(session, limit=reaper.BATCH_SIZE):
        due = real_probe(session, limit)
        probed.extend(due)
        action()
        return due

    monkeypatch.setattr(reaper, "expired_leases", probe)
    return probed


def _reaper_left_job_alone(engine, job_id, before):
    job = _job(engine, job_id)
    assert (job["state"], job["job_fence"], job["version"], job["event_sequence"]) == before
    assert _events(engine, job_id, "ATTEMPT_LOST") == []
    assert [e["reason"] for e in _events(engine, job_id, "LEASE_REVOKED")] in ([], ["USER_CANCEL"])


def test_renew_committed_after_the_probe_keeps_the_attempt(
    migrated_postgres_engine, tmp_path, monkeypatch
):
    """B15-R23: the probe returns a due lease; a renewal commits before the reaper's
    locks, and the CAS recheck (`expires_at > now`) leaves the attempt alone."""
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-reap-renew") as control:
        control.counters(outstanding=1, active=1)
        service, epoch = _leader(engine)
        lease_id = control.job.authority["lease_id"]
        _expire_lease(engine, lease_id)

        def renew():
            # The renewal checked a still-live lease and commits only now, after the
            # probe read the lease as due.
            _expire_lease(engine, lease_id, seconds=-2)
            assert _renew(control).status_code == 200

        probed = _between_probe_and_lock(monkeypatch, renew)
        job = _job(engine, control.job.job_id)
        before = (job["state"], job["job_fence"], job["version"], job["event_sequence"])
        assert service.reap_leases(epoch) == 0
        assert probed == [UUID(lease_id)]
        _reaper_left_job_alone(engine, control.job.job_id, before)
        rows = control.authority_rows()
        assert rows["lease"]["revoked_at"] is None
        assert (rows["attempt"]["state"], rows["allocation"]["state"]) == ("RUNNING", "HELD")
        with engine.connect() as connection:
            assert rows["lease"]["expires_at"] > connection.execute(select(func.now())).scalar()


def test_cancel_committed_after_the_probe_is_not_reaped(
    migrated_postgres_engine, tmp_path, monkeypatch
):
    """B15-R23: a cancel revokes the due lease between probe and locks; the CAS recheck
    (`revoked_at`) keeps the reaper from a second fence or a LEASE_EXPIRED event."""
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-reap-cancel-cas") as control:
        control.counters(outstanding=1, active=1)
        service, epoch = _leader(engine)
        lease_id = control.job.authority["lease_id"]
        _expire_lease(engine, lease_id)

        cancelled = []

        def cancel():
            assert control.control("cancel").status_code == 202
            job = _job(engine, control.job.job_id)
            cancelled.append(
                (job["state"], job["job_fence"], job["version"], job["event_sequence"])
            )

        probed = _between_probe_and_lock(monkeypatch, cancel)
        assert service.reap_leases(epoch) == 0
        assert probed == [UUID(lease_id)]
        assert cancelled[0][:2] == ("CANCELLING", 2)
        _reaper_left_job_alone(engine, control.job.job_id, cancelled[0])
        assert control.authority_rows()["attempt"]["state"] == "STOPPING"


def _second_running_lease(control):
    """Seed another RUNNING job and live authority on the same worker; return its ids."""
    with control.engine.begin() as connection:
        job = seed_job(
            connection,
            control.graph,
            artifact_id=control.job.graph["artifact_id"],
            canonical_spec={"template_id": control.graph["template_id"]},
            state="RUNNING",
        )
        ids = seed_authority(
            connection,
            control.graph,
            job,
            {
                "worker_id": UUID(control.job.authority["worker_id"]),
                "incarnation_id": UUID(control.job.authority["worker_incarnation_id"]),
            },
        )
        connection.execute(
            update(s.attempts)
            .where(s.attempts.c.attempt_id == ids["attempt_id"])
            .values(state="RUNNING", started_at=func.now())
        )
    return job["job_id"], ids


def test_one_failing_lease_does_not_stop_the_reaper_or_the_tick(
    migrated_postgres_engine, tmp_path, monkeypatch
):
    """B15-R26: a lease whose job stays locked past lock_timeout (55P03) is skipped
    and retried on a later probe; the other due lease is reaped, and the tick
    still reaches dispatch."""
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-reap-isolate") as control:
        control.counters(outstanding=2, active=2)
        other_job, other = _second_running_lease(control)
        # The locked lease is due first, so the probe returns it first.
        _expire_lease(engine, control.job.authority["lease_id"], seconds=3)
        _expire_lease(engine, str(other["lease_id"]))
        service, epoch = _leader(engine)
        with engine.connect() as holder:
            transaction = holder.begin()
            holder.execute(
                select(s.jobs.c.job_id)
                .where(s.jobs.c.job_id == control.job.job_id)
                .with_for_update()
            ).one()
            assert service.reap_leases(epoch) == 1
            assert (_job(engine, other_job)["state"], _job(engine, other_job)["job_fence"]) == (
                "RECOVERING",
                2,
            )
            assert _job(engine, control.job.job_id)["state"] == "RUNNING"

            def failing(_epoch):
                raise RuntimeError("maintenance failed")

            monkeypatch.setattr(service, "promote_retries", failing)
            with engine.begin() as connection:
                # A draining worker ends the tick at the dispatch snapshot.
                connection.execute(update(s.workers).values(admin_state="DRAINING"))
            # Still locked: the reaper fails again, promotion fails, dispatch is reached.
            assert service.tick(epoch).reason == "worker_or_mode_unavailable"
            transaction.rollback()
        monkeypatch.undo()
        assert service.reap_leases(epoch) == 1
        assert _job(engine, control.job.job_id)["state"] == "RECOVERING"


def test_duplicate_reapers_and_cancel_fence_the_attempt_exactly_once(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-reap-race") as control:
        control.counters(outstanding=1, active=1)
        service, epoch = _leader(engine)
        _expire_lease(engine, control.job.authority["lease_id"])
        # A valid If-Match makes the cancel compete for the job lock with both reapers.
        if_match = f'"v{_job(engine, control.job.job_id)["version"]}"'
        with ThreadPoolExecutor(max_workers=3) as pool:
            reaped = [pool.submit(service.reap_leases, epoch) for _ in range(2)]
            cancelled = pool.submit(control.control, "cancel", if_match=if_match)
            results = [future.result() for future in reaped]
            cancel = cancelled.result()
        job = _job(engine, control.job.job_id)
        lost = _events(engine, control.job.job_id, "ATTEMPT_LOST")
        cancelling = _events(engine, control.job.job_id, "CANCEL_REQUESTED")
        # Exactly one winner fences the attempt; the others see its commit.
        assert job["job_fence"] == 2
        assert control.authority_rows()["lease"]["revoked_at"] is not None
        if cancel.status_code == 202:
            assert sorted(results) == [0, 0] and lost == []
            assert job["state"] == "CANCELLING" and len(cancelling) == 1
        else:
            # A reaper committed first; the cancel then saw a newer version.
            assert cancel.status_code == 412, cancel.text
            assert sorted(results) == [0, 1] and len(lost) == 1
            assert job["state"] == "RECOVERING" and cancelling == []


def _await_lock_waiters(engine, count):
    """Block until `count` sessions of this database wait on a lock."""
    from sqlalchemy import text

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with engine.connect() as observer:
            waiting = observer.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
        if waiting >= count:
            return
        time.sleep(0.02)
    pytest.fail(f"{count} sessions never queued on the job lock")


@pytest.mark.parametrize("first", ["cancel", "reaper"])
def test_three_way_race_queued_on_the_job_lock_has_one_winner(
    migrated_postgres_engine, tmp_path, first
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label=f"b15-race3-{first}") as control:
        control.counters(outstanding=1, active=1)
        service, epoch = _leader(engine)
        _expire_lease(engine, control.job.authority["lease_id"])
        if_match = f'"v{_job(engine, control.job.job_id)["version"]}"'
        with engine.connect() as holder, ThreadPoolExecutor(max_workers=3) as pool:
            transaction = holder.begin()
            holder.execute(
                select(s.jobs.c.job_id)
                .where(s.jobs.c.job_id == control.job.job_id)
                .with_for_update()
            ).one()
            # Row-lock waiters are granted in queue order, so `first` wins the job lock.
            futures = []
            order = (
                ["cancel", "reaper", "reaper"]
                if first == "cancel"
                else [
                    "reaper",
                    "cancel",
                    "reaper",
                ]
            )
            for index, name in enumerate(order, start=1):
                if name == "cancel":
                    cancelled = pool.submit(control.control, "cancel", if_match=if_match)
                else:
                    futures.append(pool.submit(service.reap_leases, epoch))
                _await_lock_waiters(engine, index)
            transaction.rollback()
            results = sorted(future.result() for future in futures)
            cancel = cancelled.result()
        job = _job(engine, control.job.job_id)
        lost = _events(engine, control.job.job_id, "ATTEMPT_LOST")
        cancelling = _events(engine, control.job.job_id, "CANCEL_REQUESTED")
        assert job["job_fence"] == 2
        if first == "cancel":
            assert cancel.status_code == 202, cancel.text
            # Both reapers recheck the lease under the lock and find it revoked.
            assert results == [0, 0] and lost == [] and len(cancelling) == 1
            assert job["state"] == "CANCELLING"
            assert control.authority_rows()["lease"]["revoke_reason"] == "USER_CANCEL"
        else:
            assert cancel.status_code == 412, cancel.text
            assert results == [0, 1] and len(lost) == 1 and cancelling == []
            assert job["state"] == "RECOVERING"
            assert control.authority_rows()["lease"]["revoke_reason"] == "LEASE_EXPIRED"


def test_reaper_queued_before_a_completion_fences_and_the_completion_is_stale(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-reap-complete-q") as control:
        control.counters(outstanding=1, active=1)
        worker = control.worker_client.app.state.services.worker
        complete, payload_hash, _ = _prepare_completion(
            control.worker_client, engine, control.job.credential, control.job.authority, "b15-rc"
        )
        service, epoch = _leader(engine)
        _expire_lease(engine, control.job.authority["lease_id"])

        def finish():
            try:
                worker.complete_attempt(
                    credential=control.job.credential,
                    attempt_id=UUID(control.job.authority["attempt_id"]),
                    callback_id=new_uuid7(),
                    payload_hash=payload_hash,
                    request=complete,
                )
                return 200, None
            except ApplicationError as exc:
                return exc.status, exc.code

        with engine.connect() as holder, ThreadPoolExecutor(max_workers=2) as pool:
            transaction = holder.begin()
            holder.execute(
                select(s.jobs.c.job_id)
                .where(s.jobs.c.job_id == control.job.job_id)
                .with_for_update()
            ).one()
            reaped = pool.submit(service.reap_leases, epoch)
            _await_lock_waiters(engine, 1)
            finished = pool.submit(finish)
            _await_lock_waiters(engine, 2)
            transaction.rollback()
            reaped, finished = reaped.result(), finished.result()
        assert reaped == 1
        assert finished == (409, "stale_authority")
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["job_fence"]) == ("RECOVERING", 2)
        rows = control.authority_rows()
        assert rows["attempt"]["state"] == "LOST"
        with engine.connect() as connection:
            assert connection.execute(select(s.results)).first() is None
            reservation = connection.execute(select(s.result_reservations.c.state)).scalar_one()
        assert reservation == "ABANDONED"
        assert control.counter_values()["GLOBAL"] == (1, 1)


@pytest.mark.parametrize("expires_again", [False, True])
def test_completion_committed_after_the_probe_is_not_fenced(
    migrated_postgres_engine, tmp_path, monkeypatch, expires_again
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label=f"b15-reap-complete-{int(expires_again)}") as control:
        control.counters(outstanding=1, active=1)
        worker = control.worker_client.app.state.services.worker
        complete, payload_hash, _ = _prepare_completion(
            control.worker_client, engine, control.job.credential, control.job.authority, "b15-cp"
        )
        service, epoch = _leader(engine)
        lease_id = control.job.authority["lease_id"]
        _expire_lease(engine, lease_id)
        completed = []

        def finish():
            # The completion checked a still-live lease and commits after the probe.
            _expire_lease(engine, lease_id, seconds=-2)
            worker.complete_attempt(
                credential=control.job.credential,
                attempt_id=UUID(control.job.authority["attempt_id"]),
                callback_id=new_uuid7(),
                payload_hash=payload_hash,
                request=complete,
            )
            completed.append(_job(engine, control.job.job_id))
            if expires_again:
                _expire_lease(engine, lease_id)

        probed = _between_probe_and_lock(monkeypatch, finish)
        reaped = service.reap_leases(epoch)
        assert probed == [UUID(lease_id)]
        (done,) = completed
        job = _job(engine, control.job.job_id)
        # SUCCEEDED is kept with its fence and version; no ATTEMPT_LOST (L1).
        assert (job["state"], job["job_fence"], job["version"]) == (
            "SUCCEEDED",
            done["job_fence"],
            done["version"],
        )
        assert _events(engine, control.job.job_id, "ATTEMPT_LOST") == []
        rows = control.authority_rows()
        assert rows["attempt"]["state"] == "SUCCEEDED"
        if expires_again:
            # The leftover authority is revoked and the allocation quarantined.
            assert reaped == 1
            assert rows["lease"]["revoke_reason"] == "LEASE_EXPIRED"
            assert rows["allocation"]["state"] == "QUARANTINED"
        else:
            assert reaped == 0
            assert rows["lease"]["revoked_at"] is None
            assert rows["allocation"]["state"] == "HELD"
        assert control.cleanup().status_code == 200
        assert control.counter_values()["GLOBAL"] == (0, 0)


def test_reaper_and_cancel_race_has_one_fence(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-reap-cancel") as control:
        control.counters(outstanding=1, active=1)
        service, epoch = _leader(engine)
        _expire_lease(engine, control.job.authority["lease_id"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            reaped = pool.submit(service.reap_leases, epoch)
            cancelled = pool.submit(control.control, "cancel", if_match='"v1"')
            reaped, cancel = reaped.result(), cancelled.result()
        job = _job(engine, control.job.job_id)
        lost = _events(engine, control.job.job_id, "ATTEMPT_LOST")
        assert job["job_fence"] == 2
        if reaped == 1:
            # The reaper committed first; cancel then saw a newer version.
            assert cancel.status_code == 412 and len(lost) == 1
            assert job["state"] == "RECOVERING"
            assert control.authority_rows()["attempt"]["state"] == "LOST"
        else:
            # Cancel revoked the lease first; the reaper's recheck finds nothing due.
            assert (reaped, cancel.status_code, job["state"]) == (0, 202, "CANCELLING")
            assert not lost and control.authority_rows()["attempt"]["state"] == "STOPPING"


def test_coordinator_tick_runs_the_reaper(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-reap-tick") as control:
        _expire_lease(engine, control.job.authority["lease_id"])
        with engine.begin() as connection:
            # A draining worker ends the tick after the reaper, before any snapshot.
            connection.execute(update(s.workers).values(admin_state="DRAINING"))
        service, epoch = _leader(engine)
        decision = service.tick(epoch)
        assert decision.reason == "worker_or_mode_unavailable"
        assert _job(engine, control.job.job_id)["state"] == "RECOVERING"


def test_reaped_unclaimed_offer_stays_unclaimed_for_tombstone_cleanup(
    migrated_postgres_engine, tmp_path
):
    """A reaped offer the worker never claimed must stay tombstonable (Docker C5a, run 5)."""
    from tests.integration.test_jobs_b08 import _submit_body
    from tests.integration.test_worker_api_b10 import WORKER_ID

    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b15-reap-unclaimed"
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
            job_id = seed_job(connection, graph, canonical_spec=spec)["job_id"]
            connection.execute(update(s.jobs).values(eligible_since=func.clock_timestamp()))
            connection.execute(update(s.admission_counters).values(outstanding=1))
        service, epoch = _leader(engine)
        service.tick(epoch)
        with engine.connect() as connection:
            attempt = connection.execute(select(s.attempts)).mappings().one()
            lease = connection.execute(select(s.attempt_leases)).mappings().one()
        assert attempt["state"] == "CREATED" and attempt["claimed_at"] is None
        _expire_lease(engine, str(lease["lease_id"]))
        assert service.reap_leases(epoch) == 1
        assert _job(engine, job_id)["state"] == "RECOVERING"

        page = client.get(
            f"/v1/workers/{WORKER_ID}/reconciliation?page_size=100",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"],
            },
        )
        assert page.status_code == 200, page.text
        (item,) = page.json()["items"]
        assert (item["authority_state"], item["claim_state"], item["expected_container"]) == (
            "REVOKED",
            "UNCLAIMED",
            None,
        )
        # The worker's reconciliation tombstone (NO_CONTAINER) releases the quarantine.
        cleanup = client.post(
            f"/v1/attempts/{attempt['attempt_id']}/cleanup",
            headers={"Authorization": f"Bearer {credential}", "X-Callback-Id": str(new_uuid7())},
            json={
                **{key: value for key, value in item["authority"].items() if key != "lease_id"},
                "proof": {
                    "proof_type": "NO_CONTAINER",
                    "startup_nonce": item["startup_nonce"],
                    "executor_operation_sequence": 1,
                    "tombstone_sequence": 1,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "inspection_checksum": "sha256:" + "a" * 64,
                },
            },
        )
        assert cleanup.status_code == 200, cleanup.text
        assert cleanup.json()["allocation_state"] == "RELEASED"
        assert _job(engine, job_id)["state"] != "RECOVERING"


def _reconcile_and_heartbeat(client, engine, credential, incarnation, observed):
    from tests.integration.test_worker_api_b10 import WORKER_ID, _inventory

    headers = {"Authorization": f"Bearer {credential}"}
    page = client.get(
        f"/v1/workers/{WORKER_ID}/reconciliation?page_size=100",
        headers={**headers, "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"]},
    )
    assert page.status_code == 200, page.text
    _assert_wire(page.json())
    beat = client.post(
        f"/v1/workers/{WORKER_ID}/heartbeat",
        headers={**headers, "X-Callback-Id": str(new_uuid7())},
        json={
            "worker_incarnation_id": incarnation["worker_incarnation_id"],
            "observed_health": "READY",
            "reconcile_complete": True,
            "inventory": _inventory(),
            "observed_containers": observed,
        },
    )
    assert beat.status_code == 200, beat.text
    _assert_wire(beat.json())
    with engine.connect() as connection:
        return connection.execute(
            select(s.workers.c.health).where(s.workers.c.worker_id == UUID(WORKER_ID))
        ).scalar_one()


def test_worker_with_a_dispatched_checkpoint_for_pause_offer_stays_ready_to_claim_it(
    migrated_postgres_engine, tmp_path
):
    """A CFP offer (desired PAUSED) must not block READY, or it is never polled (B15-R13)."""
    from tests.integration.test_jobs_b08 import _submit_body
    from tests.integration.test_worker_api_b10 import WORKER_ID

    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b15-cfp-offer-ready"
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
            job_id = seed_job(connection, graph, canonical_spec=spec)["job_id"]
            connection.execute(
                update(s.jobs).values(
                    eligible_since=func.clock_timestamp(),
                    desired_state="PAUSED",
                    recovery_intent=CFP,
                )
            )
            connection.execute(update(s.admission_counters).values(outstanding=1))
        service, epoch = _leader(engine)
        service.tick(epoch)
        with engine.connect() as connection:
            attempt = connection.execute(select(s.attempts)).mappings().one()
        assert (attempt["state"], attempt["execution_intent"]) == ("CREATED", CFP)

        assert _reconcile_and_heartbeat(client, engine, credential, incarnation, []) == "READY"
        poll = client.post(
            f"/v1/workers/{WORKER_ID}/poll",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"],
            },
            json={"worker_incarnation_id": incarnation["worker_incarnation_id"]},
        )
        assert poll.status_code == 200, poll.text
        assert poll.json()["offer"]["authority"]["attempt_id"] == str(attempt["attempt_id"])
        _assert_wire(poll.json())
        assert _job(engine, job_id)["state"] == "DISPATCHING"


def test_worker_running_a_pausing_attempt_stays_ready(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b15-pausing-attempt-ready"
        )
        _running_authority(engine, incarnation)
        with engine.begin() as connection:
            connection.execute(update(s.jobs).values(state="PAUSING", desired_state="PAUSED"))
        observed = [{"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST}]
        assert (
            _reconcile_and_heartbeat(client, engine, credential, incarnation, observed) == "READY"
        )


def _publish_checkpoint(job, **changes):
    reservation = job.reserve()
    assert reservation.status_code == 201, reservation.text
    manifest, artifact, _ = job.checkpoint(reservation.json(), **changes)
    return job.publish(manifest, artifact)


def _renew(control):
    return control.job.post(
        "/renew", {"authority": control.job.authority, "progress_sequence": 0, "progress": None}
    )


def test_stale_fence_incarnation_and_lease_answer_stale_authority(
    migrated_postgres_engine, tmp_path
):
    # SM:59 (B15-R30): a stale incarnation, fence or lease is 409 stale_authority.
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-stale-authority") as control:
        control.counters(outstanding=1, active=1)
        authority = control.job.authority

        def renew(**changes):
            return control.job.post(
                "/renew",
                {"authority": {**authority, **changes}, "progress_sequence": 0, "progress": None},
            )

        stale = [
            renew(job_fence=authority["job_fence"] + 1),
            renew(worker_incarnation_id=str(new_uuid7())),
            control.job.reserve(authority=dict(authority, job_fence=authority["job_fence"] + 1)),
        ]
        _expire_lease(engine, authority["lease_id"])
        stale += [_renew(control), _container_failure(control, "OOM", "CONTAINER_OOM", oom=True)]
        assert [(r.status_code, r.json()["code"]) for r in stale] == [
            (409, "stale_authority")
        ] * len(stale)
        rows = control.authority_rows()
        assert rows["attempt"]["state"] == "RUNNING"
        assert _job(engine, control.job.job_id)["state"] == "RUNNING"


def test_pause_checkpoints_then_cleanup_pauses_and_keeps_admission(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-pause") as control:
        control.counters(outstanding=1, active=1)
        response = control.control("pause")
        assert response.status_code == 202, response.text
        body = response.json()
        assert (body["state"], body["desired_state"], body["job_fence"]) == (
            "PAUSING",
            "PAUSED",
            1,
        )
        assert response.headers["ETag"] == f'"v{body["version"]}"'
        rows = control.authority_rows()
        assert rows["lease"]["revoked_at"] is None and rows["allocation"]["state"] == "HELD"
        event = _last_event(engine, control.job.job_id)
        assert (event["event_type"], event["reason"]) == ("PAUSE_REQUESTED", "USER_PAUSE")
        (audit,) = _audits(engine, control.job.job_id, "job.pause")
        assert audit["reason"] == REASON

        renew = _renew(control)
        assert renew.status_code == 200 and renew.json()["desired_state"] == "PAUSED"
        result = control.job.post("/result-reservations", {"authority": control.job.authority})
        assert result.status_code == 409 and result.json()["code"] == "stale_authority"
        early = control.cleanup()
        assert early.status_code == 409, early.text

        published = _publish_checkpoint(control.job)
        assert published.status_code == 201, published.text
        assert control.authority_rows()["attempt"]["state"] == "STOPPING"
        assert _job(engine, control.job.job_id)["state"] == "PAUSING"
        assert _renew(control).status_code == 200
        repeat = control.control("pause")
        assert repeat.status_code == 409 and repeat.json()["code"] == "state_conflict"

        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["desired_state"], job["recovery_intent"]) == (
            "PAUSED",
            "PAUSED",
            None,
        )
        assert job["terminal_at"] is None and job["retry_count"] == 0
        rows = control.authority_rows()
        assert (rows["attempt"]["state"], rows["attempt"]["failure_reason"]) == (
            "CANCELLED",
            "PAUSE",
        )
        assert rows["allocation"]["state"] == "RELEASED"
        # Paused keeps the admission (outstanding) but releases the active attempt.
        assert control.counter_values()["GLOBAL"] == (1, 0)


def test_pause_finishes_an_open_interval_cycle(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-pause-open") as control:
        control.counters(outstanding=1, active=1)
        reservation = control.job.reserve()
        assert reservation.status_code == 201, reservation.text
        paused = control.control("pause")
        assert paused.status_code == 202, paused.text
        manifest, artifact, _ = control.job.checkpoint(reservation.json())
        published = control.job.publish(manifest, artifact)
        assert published.status_code == 201, published.text
        assert control.authority_rows()["attempt"]["state"] == "STOPPING"


def test_rejected_pause_checkpoint_aborts_the_pause(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-pause-abort") as control:
        control.counters(outstanding=1, active=1)
        assert control.control("pause").status_code == 202
        rejected = _publish_checkpoint(control.job, provenance=control.job.provenance(job_fence=7))
        assert rejected.status_code == 422, rejected.text
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["desired_state"]) == ("RUNNING", "RUNNING")
        event = _last_event(engine, control.job.job_id)
        assert (event["event_type"], event["reason"]) == (
            "PAUSE_ABORTED",
            "CHECKPOINT_PROVENANCE_MISMATCH",
        )
        rows = control.authority_rows()
        assert rows["attempt"]["state"] == "RUNNING" and rows["lease"]["revoked_at"] is None
        with engine.connect() as connection:
            states = connection.execute(select(s.checkpoint_reservations.c.state)).scalars().all()
        assert states == ["REJECTED"]


def test_rejected_checkpoint_for_pause_checkpoint_fails_without_a_retry(
    migrated_postgres_engine, tmp_path
):
    # B15-R28: no pause abort for a CHECKPOINT_FOR_PAUSE Attempt; the worker
    # reports the deterministic defect as INTERNAL and the job fails.
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-cfp-reject") as control:
        control.counters(outstanding=1, active=1)
        with engine.begin() as connection:
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == control.job.job_id)
                .values(
                    state="PAUSING",
                    desired_state="PAUSED",
                    recovery_intent=CFP,
                    retry_count=1,
                )
            )
            connection.execute(update(s.attempts).values(execution_intent=CFP))
        rejected = _publish_checkpoint(control.job, provenance=control.job.provenance(job_fence=7))
        assert rejected.status_code == 422, rejected.text
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["desired_state"], job["recovery_intent"]) == (
            "PAUSING",
            "PAUSED",
            CFP,
        )
        assert _last_event(engine, control.job.job_id)["event_type"] == "CHECKPOINT_REJECTED"

        failed = _container_failure(control, "INTERNAL", "CHECKPOINT_PROTOCOL_ERROR")
        assert failed.status_code == 200, failed.text
        _assert_wire(failed.json())
        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["retry_count"], job["recovery_intent"]) == ("FAILED", 1, None)
        with engine.connect() as connection:
            assert (
                connection.execute(select(func.count()).select_from(s.retry_schedules)).scalar_one()
                == 0
            )
        assert control.counter_values()["GLOBAL"] == (0, 0)


def test_pause_requires_checkpointable_running_job(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-pause-guard", checkpointable=False) as control:
        control.counters(outstanding=1, active=1)
        infeasible = control.control("pause")
        assert infeasible.status_code == 422, infeasible.text
        assert infeasible.json()["code"] == "infeasible_request"


def test_pause_of_a_queued_job_is_a_state_conflict(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-pause-queued") as control:
        control.counters(outstanding=1, active=1)
        queued = control.seed()
        response = control.control("pause", job_id=queued)
        assert response.status_code == 409 and response.json()["code"] == "state_conflict"
        assert _job(engine, queued)["state"] == "QUEUED"


def test_pause_crash_after_checkpoint_pauses_without_retry(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-crash-after") as control:
        control.counters(outstanding=1, active=1)
        assert control.control("pause").status_code == 202
        assert _publish_checkpoint(control.job).status_code == 201
        _expire_lease(engine, control.job.authority["lease_id"])
        service, epoch = _leader(engine)
        assert service.reap_leases(epoch) == 1
        assert _job(engine, control.job.job_id)["desired_state"] == "PAUSED"
        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["retry_count"], job["recovery_intent"]) == ("PAUSED", 0, None)
        assert control.authority_rows()["attempt"]["state"] == "LOST"
        assert control.counter_values()["GLOBAL"] == (1, 0)


@pytest.mark.parametrize(
    ("restart_safe", "retry_count", "expected"),
    [(True, 0, "RETRY_WAIT"), (False, 0, "FAILED"), (True, 2, "FAILED")],
)
def test_pause_crash_before_checkpoint(
    migrated_postgres_engine, tmp_path, restart_safe, retry_count, expected
):
    engine = migrated_postgres_engine
    label = f"b15-crash-before-{int(restart_safe)}-{retry_count}"
    with Control(engine, tmp_path, label=label, restart_safe=restart_safe) as control:
        control.counters(outstanding=1, active=1)
        with engine.begin() as connection:
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == control.job.job_id)
                .values(retry_count=retry_count)
            )
        assert control.control("pause").status_code == 202
        _expire_lease(engine, control.job.authority["lease_id"])
        service, epoch = _leader(engine)
        assert service.reap_leases(epoch) == 1
        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        job = _job(engine, control.job.job_id)
        assert job["state"] == expected
        if expected == "RETRY_WAIT":
            # The infrastructure retry is consumed once and only to produce a checkpoint.
            assert (job["retry_count"], job["desired_state"], job["recovery_intent"]) == (
                1,
                "PAUSED",
                CFP,
            )
            assert _schedule(engine, control.job.job_id)["retry_number"] == 1
            assert control.counter_values()["GLOBAL"] == (1, 0)
            resume = control.control("resume")
            assert resume.status_code == 409, resume.text
        else:
            assert (job["retry_count"], job["recovery_intent"]) == (retry_count, None)
            assert job["terminal_at"] is not None
            assert control.counter_values()["GLOBAL"] == (0, 0)


@pytest.mark.parametrize(
    ("failure_class", "expected"), [("INFRASTRUCTURE", "PAUSED"), ("TIMEOUT", "FAILED")]
)
def test_pausing_failure_with_committed_checkpoint(
    migrated_postgres_engine, tmp_path, failure_class, expected
):
    engine = migrated_postgres_engine
    label = f"b15-pause-fail-{failure_class.lower()}"
    with Control(engine, tmp_path, label=label) as control:
        control.counters(outstanding=1, active=1)
        # An interval checkpoint committed while RUNNING, then a pause and a failure.
        assert _publish_checkpoint(control.job).status_code == 201
        assert control.control("pause").status_code == 202
        failed = control.job.post(
            "/fail",
            {
                "authority": control.job.authority,
                "failure_class": failure_class,
                "reason_code": "EXECUTOR_UNAVAILABLE"
                if failure_class == "INFRASTRUCTURE"
                else "RUNTIME_LIMIT_REACHED",
                "observation": {
                    "observation_type": "CONTAINER",
                    "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
                    "observed_at": datetime.now(UTC).isoformat(),
                    "exit_code": 137,
                    "oom_killed": False,
                    "runtime_limit_reached": failure_class == "TIMEOUT",
                },
            },
        )
        assert failed.status_code == 200, failed.text
        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        job = _job(engine, control.job.job_id)
        # B15-R02: a non-retryable class fails even with desired PAUSED and a checkpoint.
        assert (job["state"], job["retry_count"]) == (expected, 0)
        assert control.counter_values()["GLOBAL"] == ((1, 0) if expected == "PAUSED" else (0, 0))


def _container_failure(control, failure_class, reason_code, *, oom=False, timeout=False):
    return control.job.post(
        "/fail",
        {
            "authority": control.job.authority,
            "failure_class": failure_class,
            "reason_code": reason_code,
            "observation": {
                "observation_type": "CONTAINER",
                "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
                "observed_at": datetime.now(UTC).isoformat(),
                "exit_code": 137,
                "oom_killed": oom,
                "runtime_limit_reached": timeout,
            },
        },
    )


def test_failure_after_the_pause_checkpoint_committed_is_stale_and_cleanup_pauses(
    migrated_postgres_engine, tmp_path
):
    # B15-R21: publish while PAUSING moves the Attempt to STOPPING; SM:75 accepts
    # a failure callback only from RUNNING/CHECKPOINTING/STARTING/CLAIMED, and
    # SM:23 makes the committed pause checkpoint plus cleanup PAUSED.
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-fail-stopping") as control:
        control.counters(outstanding=1, active=1)
        assert control.control("pause").status_code == 202
        assert _publish_checkpoint(control.job).status_code == 201
        assert control.authority_rows()["attempt"]["state"] == "STOPPING"
        version = _job(engine, control.job.job_id)["version"]
        for failure in (
            _container_failure(control, "TIMEOUT", "RUNTIME_LIMIT_REACHED", timeout=True),
            _container_failure(control, "OOM", "CONTAINER_OOM", oom=True),
        ):
            assert failure.status_code == 409, failure.text
            assert failure.json()["code"] == "stale_authority"
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["version"]) == ("PAUSING", version)
        rows = control.authority_rows()
        assert rows["attempt"]["failure_class"] is None
        assert rows["lease"]["revoked_at"] is None and rows["allocation"]["state"] == "HELD"

        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["retry_count"], job["terminal_at"]) == ("PAUSED", 0, None)
        rows = control.authority_rows()
        assert (rows["attempt"]["state"], rows["attempt"]["failure_reason"]) == (
            "CANCELLED",
            "PAUSE",
        )
        assert control.counter_values()["GLOBAL"] == (1, 0)


def test_container_oom_fails_the_job_without_a_retry(migrated_postgres_engine, tmp_path):
    # ACC-22 (B15-R24): OOM is not retryable, even for a restart-safe job with a
    # committed checkpoint; the worker side of the chain is test_container_exit_b14.
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-oom") as control:
        control.counters(outstanding=1, active=1)
        assert _publish_checkpoint(control.job).status_code == 201
        failed = _container_failure(control, "OOM", "CONTAINER_OOM", oom=True)
        assert failed.status_code == 200, failed.text
        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["retry_count"], job["retry_ready_at"]) == ("FAILED", 0, None)
        assert job["terminal_at"] is not None
        rows = control.authority_rows()
        assert (rows["attempt"]["failure_class"], rows["attempt"]["failure_reason"]) == (
            "OOM",
            "CONTAINER_OOM",
        )
        with engine.connect() as connection:
            schedules = connection.execute(
                select(func.count())
                .select_from(s.retry_schedules)
                .where(s.retry_schedules.c.job_id == control.job.job_id)
            ).scalar_one()
        assert schedules == 0
        assert control.counter_values()["GLOBAL"] == (0, 0)


def _pause_to_paused(control):
    assert control.control("pause").status_code == 202
    assert _publish_checkpoint(control.job).status_code == 201
    cleaned = control.cleanup()
    assert cleaned.status_code == 200, cleaned.text
    assert _job(control.engine, control.job.job_id)["state"] == "PAUSED"


def _global_version(engine):
    with engine.connect() as connection:
        return connection.execute(
            select(s.admission_counters.c.version).where(
                s.admission_counters.c.scope_type == "GLOBAL"
            )
        ).scalar_one()


def test_resume_requeues_the_same_job_without_consuming_retry(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-resume", restart_safe=False) as control:
        control.counters(outstanding=1, active=1)
        running = control.control("resume")
        assert running.status_code == 409 and running.json()["code"] == "state_conflict"
        _pause_to_paused(control)
        _set_mode(engine, "ADMISSION_OFF")
        version = _global_version(engine)
        before = _job(engine, control.job.job_id)
        key = "b15-resume-key-0001"
        response = control.control("resume", key=key)
        assert response.status_code == 202, response.text
        body = response.json()
        assert (body["job_id"], body["state"], body["desired_state"]) == (
            str(control.job.job_id),
            "QUEUED",
            "RUNNING",
        )
        job = _job(engine, control.job.job_id)
        assert (job["retry_count"], job["job_fence"], job["recovery_intent"]) == (
            before["retry_count"],
            before["job_fence"],
            None,
        )
        assert job["ready_sequence"] == version and _global_version(engine) == version + 1
        assert control.counter_values()["GLOBAL"] == (1, 0)
        event = _last_event(engine, control.job.job_id)
        assert (event["event_type"], event["reason"]) == ("JOB_RESUMED", "USER_RESUME")
        replay = control.control("resume", key=key, if_match='"v1"')
        assert (replay.status_code, replay.json()) == (202, body)
        again = control.control("resume")
        assert again.status_code == 409


def test_resume_needs_an_uncorrupted_checkpoint_or_restart_safe_input(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-resume-corrupt", restart_safe=False) as control:
        control.counters(outstanding=1, active=1)
        _pause_to_paused(control)
        with engine.begin() as connection:
            checkpoint_id = connection.execute(select(s.checkpoints.c.checkpoint_id)).scalar_one()
            connection.execute(
                insert(s.checkpoint_corruptions).values(
                    checkpoint_id=checkpoint_id,
                    tenant_id=control.tenant_id,
                    reason_code="CHECKPOINT_BLOB_MISSING",
                )
            )
        version = _job(engine, control.job.job_id)["version"]
        response = control.control("resume")
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "infeasible_request"
        assert _job(engine, control.job.job_id)["version"] == version


def _tenant_policy(control):
    from decimal import Decimal

    with control.engine.begin() as connection:
        connection.execute(
            update(s.policy_versions)
            .where(s.policy_versions.c.is_current.is_(True))
            .values(global_outstanding_limit=1000)
        )
        connection.execute(
            s.tenant_policies.insert().values(
                tenant_id=control.tenant_id,
                version=1,
                weight=Decimal(1),
                cpu_limit_millis=6000,
                memory_limit_bytes=12 * 1024**3,
                gpu_limit=1,
                outstanding_limit=100,
                user_outstanding_limit=100,
                tenant_active_limit=1,
                user_active_limit=1,
                tenant_rate_per_second=Decimal(10),
                tenant_rate_burst=Decimal(10),
                user_rate_per_second=Decimal(10),
                user_rate_burst=Decimal(10),
                is_current=True,
            )
        )


def _fail_to_terminal(control):
    """A TIMEOUT failure plus verified cleanup: the job ends FAILED."""
    failed = control.job.post(
        "/fail",
        {
            "authority": control.job.authority,
            "failure_class": "TIMEOUT",
            "reason_code": "RUNTIME_LIMIT_REACHED",
            "observation": {
                "observation_type": "CONTAINER",
                "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
                "observed_at": datetime.now(UTC).isoformat(),
                "exit_code": 137,
                "oom_killed": False,
                "runtime_limit_reached": True,
            },
        },
    )
    assert failed.status_code == 200, failed.text
    cleaned = control.cleanup()
    assert cleaned.status_code == 200, cleaned.text
    assert _job(control.engine, control.job.job_id)["state"] == "FAILED"


# The catalog cpu-iterative schema and bounds, so the source spec is admissible again.
CATALOG_TEMPLATE = {
    "parameter_schema": [
        {"name": name, "type": "INTEGER", "required": True, "minimum": low, "maximum": high}
        for name, low, high in (
            ("iterations", 1, 1_000_000_000),
            ("seed", 0, 2_147_483_647),
            ("modulus", 2, 2_147_483_647),
        )
    ],
    "resource_bounds": {
        "resources": {"cpu_millis": 6000, "memory_bytes": 2 * 1024**3, "gpu_count": 0},
        "runtime_limit_seconds": 300,
        "checkpoint_interval_seconds": 60,
    },
}


def _retry_control(engine, tmp_path, label, **kwargs):
    control = Control(
        engine,
        tmp_path,
        label=label,
        template_id="cpu-iterative",
        template_values=CATALOG_TEMPLATE,
        input_media_type="application/vnd.nexa.cpu-iterative-input+json",
        **kwargs,
    )
    control.counters(outstanding=1, active=1)
    _tenant_policy(control)
    # The fixture heartbeat advertises another image; admission needs the template digest.
    with engine.begin() as connection:
        inventory = connection.execute(
            select(
                s.worker_inventories.c.inventory_id,
                s.worker_inventories.c.workload_capabilities,
            )
        ).one()
        capabilities = dict(inventory.workload_capabilities)
        capabilities["images"] = [
            {"image_digest": IMAGE_DIGEST, "architecture": "linux/amd64", "verified": True}
        ]
        connection.execute(
            update(s.worker_inventories)
            .where(s.worker_inventories.c.inventory_id == inventory.inventory_id)
            .values(workload_capabilities=capabilities)
        )
    return control


def _retry_body(checkpoint_id=None):
    return {
        "reason": REASON,
        "checkpoint_id": None if checkpoint_id is None else str(checkpoint_id),
    }


def _spec_row(engine, job_id):
    with engine.connect() as connection:
        return (
            connection.execute(select(s.job_specs).where(s.job_specs.c.job_id == job_id))
            .mappings()
            .one()
        )


def test_manual_retry_creates_a_new_job_and_leaves_the_source_unchanged(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with _retry_control(engine, tmp_path, "b15-retry") as control:
        source_id = control.job.job_id
        running = control.control("retry", body=_retry_body())
        assert running.status_code == 409 and running.json()["code"] == "state_conflict"
        _fail_to_terminal(control)
        source = _job(engine, source_id)
        source_events = len(_events(engine, source_id, None))

        missing = control.control("retry", if_match=None, body=_retry_body())
        assert missing.status_code == 428, missing.text
        stale = control.control("retry", if_match='"v999"', body=_retry_body())
        assert stale.status_code == 412, stale.text
        _set_mode(engine, "ADMISSION_OFF")
        off = control.control("retry", body=_retry_body())
        assert off.status_code == 409 and off.json()["code"] == "state_conflict"
        _set_mode(engine, "NORMAL")
        control.login("other")
        hidden = control.control("retry", body=_retry_body())
        assert hidden.status_code == 404, hidden.text
        control.login("member")

        key = "b15-retry-key-0001"
        response = control.control("retry", key=key, body=_retry_body())
        assert response.status_code == 202, response.text
        body = response.json()
        assert response.headers["ETag"] == '"v1"'
        new_id = UUID(body["job_id"])
        assert new_id != source_id
        assert (body["retry_of_job_id"], body["state"], body["desired_state"]) == (
            str(source_id),
            "QUEUED",
            "RUNNING",
        )
        assert (body["retry_count"], body["job_fence"], body["version"]) == (0, 0, 1)
        assert body["session_id"] != str(control.job.session_id)
        assert (
            _spec_row(engine, new_id)["spec_checksum"]
            == _spec_row(engine, source_id)["spec_checksum"]
        )
        assert body["spec"] == _spec_row(engine, source_id)["canonical_spec"]
        (accepted,) = _events(engine, new_id, None)
        assert (accepted["sequence"], accepted["event_type"], accepted["reason"]) == (
            1,
            "JOB_ACCEPTED",
            "MANUAL_RETRY",
        )
        (audit,) = _audits(engine, new_id, "job.retry")
        assert audit["reason"] == REASON
        assert audit["safe_metadata"]["retry_of_job_id"] == str(source_id)
        # SM:41 — the terminal source is untouched; the new job takes the admission.
        after = _job(engine, source_id)
        assert (after["state"], after["version"], after["event_sequence"]) == (
            "FAILED",
            source["version"],
            source["event_sequence"],
        )
        assert len(_events(engine, source_id, None)) == source_events
        assert control.counter_values()["GLOBAL"] == (1, 0)
        with engine.connect() as connection:
            assert connection.execute(select(s.checkpoint_references)).first() is None

        replay = control.control("retry", key=key, if_match='"v999"', body=_retry_body())
        assert (replay.status_code, replay.json()) == (202, body)
        new_retry = control.control("retry", job_id=new_id, body=_retry_body())
        assert new_retry.status_code == 409, new_retry.text
        assert control.counter_values()["GLOBAL"] == (1, 0)


def _checkpoint_id(engine, job_id):
    with engine.connect() as connection:
        return connection.execute(
            select(s.checkpoints.c.checkpoint_id).where(s.checkpoints.c.job_id == job_id)
        ).scalar_one()


def test_manual_retry_references_a_verified_checkpoint(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with _retry_control(engine, tmp_path, "b15-retry-ckpt", restart_safe=False) as control:
        assert _publish_checkpoint(control.job).status_code == 201
        _fail_to_terminal(control)
        checkpoint_id = _checkpoint_id(engine, control.job.job_id)

        unknown = control.control("retry", body=_retry_body(new_uuid7()))
        assert unknown.status_code == 404, unknown.text
        # A committed checkpoint of another tenant is not visible either.
        _other_graph, foreign_id = _committed_checkpoint(engine, "b15-retry-foreign")
        foreign = control.control("retry", body=_retry_body(foreign_id))
        assert foreign.status_code == 404, foreign.text
        assert foreign.json()["code"] == "resource_not_found"
        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(s.jobs)
                    .where(s.jobs.c.retry_of_job_id == control.job.job_id)
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    select(func.count()).select_from(s.checkpoint_references)
                ).scalar_one()
                == 0
            )
        response = control.control("retry", body=_retry_body(checkpoint_id))
        assert response.status_code == 202, response.text
        new_id = UUID(response.json()["job_id"])
        with engine.connect() as connection:
            reference = connection.execute(select(s.checkpoint_references)).mappings().one()
            owner = connection.execute(
                select(s.checkpoints.c.job_id).where(s.checkpoints.c.checkpoint_id == checkpoint_id)
            ).scalar_one()
        assert (
            reference["tenant_id"],
            reference["source_checkpoint_id"],
            reference["target_job_id"],
            reference["reason"],
        ) == (control.tenant_id, checkpoint_id, new_id, "MANUAL_RETRY")
        # Inheritance is by reference: ownership of the source checkpoint is unchanged.
        assert owner == control.job.job_id
        (audit,) = _audits(engine, new_id, "job.retry")
        assert audit["safe_metadata"]["checkpoint_id"] == str(checkpoint_id)


def _first_attempt(control, job_id):
    """Seed attempt 1 of a queued job like a dispatch and point the fixture's authority at it."""
    old = control.job.authority
    with control.engine.begin() as connection:
        connection.execute(
            update(s.jobs).where(s.jobs.c.job_id == job_id).values(state="DISPATCHING")
        )
        ids = seed_authority(
            connection,
            control.job.graph,
            {"job_id": job_id},
            {
                "worker_id": UUID(old["worker_id"]),
                "incarnation_id": UUID(old["worker_incarnation_id"]),
            },
        )
    control.job.authority = {
        **old,
        "attempt_id": str(ids["attempt_id"]),
        "allocation_id": str(ids["allocation_id"]),
        "lease_id": str(ids["lease_id"]),
        "job_fence": 1,
    }
    control.job.base = f"/v1/attempts/{ids['attempt_id']}"


def test_manual_retry_first_attempt_restores_the_referenced_checkpoint(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with _retry_control(engine, tmp_path, "b15-retry-restore", restart_safe=False) as control:
        published = _publish_checkpoint(control.job)
        assert published.status_code == 201, published.text
        _fail_to_terminal(control)
        checkpoint_id = _checkpoint_id(engine, control.job.job_id)
        response = control.control("retry", body=_retry_body(checkpoint_id))
        assert response.status_code == 202, response.text
        new_id = UUID(response.json()["job_id"])

        _first_attempt(control, new_id)
        claimed = control.job.post("/claim", {"authority": control.job.authority})
        assert claimed.status_code == 200, claimed.text
        restore = claimed.json()["execution_context"]["restore_checkpoint"]
        # The source record is restored as-is: owner job/session provenance is unchanged.
        assert restore["record"] == published.json()
        assert restore["record"]["job_id"] == str(control.job.job_id)
        assert [
            (row["event_type"], row["reason"])
            for row in _events(engine, new_id, None)
            if row["event_type"].startswith("CHECKPOINT_RESTORE")
            or row["event_type"] == "CHECKPOINT_RESTORED"
        ] == [("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED")]
        # The source job keeps its terminal history.
        assert _job(engine, control.job.job_id)["state"] == "FAILED"


@pytest.mark.parametrize("defect", ["corrupt_mark", "missing_blob", "changed_blob"])
def test_manual_retry_rejects_an_unrestorable_checkpoint(
    migrated_postgres_engine, tmp_path, defect
):
    engine = migrated_postgres_engine
    with _retry_control(engine, tmp_path, f"b15-retry-{defect.replace('_', '-')}") as control:
        assert _publish_checkpoint(control.job).status_code == 201
        _fail_to_terminal(control)
        checkpoint_id = _checkpoint_id(engine, control.job.job_id)
        with engine.connect() as connection:
            state_key = connection.execute(
                select(s.artifacts.c.blob_key)
                .join(
                    s.artifact_references,
                    s.artifact_references.c.artifact_id == s.artifacts.c.artifact_id,
                )
                .where(
                    s.artifact_references.c.owner_id == checkpoint_id,
                    s.artifact_references.c.purpose == "CHECKPOINT_FILE",
                )
            ).scalar_one()
        path = control.store._path_for_key(state_key)
        if defect == "corrupt_mark":
            with engine.begin() as connection:
                connection.execute(
                    insert(s.checkpoint_corruptions).values(
                        checkpoint_id=checkpoint_id,
                        tenant_id=control.tenant_id,
                        reason_code="CHECKPOINT_CHECKSUM_MISMATCH",
                    )
                )
        elif defect == "missing_blob":
            path.unlink()
        else:
            import os

            os.chmod(path, 0o600)
            path.write_bytes(path.read_bytes()[:-1] + b" ")
        before = control.counter_values()
        response = control.control("retry", body=_retry_body(checkpoint_id))
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "infeasible_request"
        assert response.json()["message"] == "Checkpoint is not a compatible committed checkpoint"
        with engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(s.jobs)).scalar_one() == 1
            assert connection.execute(select(s.checkpoint_references)).first() is None
        assert control.counter_values() == before


# --- reads (B14-R03, listJobAttempts) ------------------------------------------------

MS_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_ANY_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _wire_stamps(value):
    if isinstance(value, dict):
        return [stamp for item in value.values() for stamp in _wire_stamps(item)]
    if isinstance(value, list):
        return [stamp for item in value for stamp in _wire_stamps(item)]
    return [value] if isinstance(value, str) and _ANY_TIMESTAMP.match(value) else []


def _assert_wire(body):
    """Every timestamp has exactly three millisecond digits (B15-R19/R29)."""
    stamps = _wire_stamps(body)
    assert stamps and all(MS_TIMESTAMP.match(stamp) for stamp in stamps), stamps


def _read(control, path, **params):
    return control.client.get(
        path, headers={"X-Nexa-Tenant-Id": str(control.tenant_id)}, params=params
    )


def test_job_events_have_exactly_three_millisecond_digits(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-read-events") as control:
        control.counters(outstanding=1, active=1)
        assert control.control("cancel").status_code == 202
        page = _read(control, f"/v1/jobs/{control.job.job_id}/events")
        assert page.status_code == 200, page.text
        stamps = [item["created_at"] for item in page.json()["items"]]
        assert stamps and all(MS_TIMESTAMP.match(stamp) for stamp in stamps), stamps


def test_adopt_transfers_the_reservation_exactly_as_reserved(migrated_postgres_engine, tmp_path):
    # The worker proves a transferred reservation by comparing it with the reserve
    # answer it journaled, so both must carry the same wire timestamps (B15-R19).
    with _client(migrated_postgres_engine, tmp_path) as client:
        fixture = CheckpointFixture(migrated_postgres_engine, client, label="b15-adopt-wire")
        reserved = fixture.reserve()
        assert reserved.status_code == 201, reserved.text
        current = _create_incarnation(
            client, fixture.credential, nonce=str(new_uuid7()), key="b15-adopt-wire-incarnation"
        )
        adopted = client.post(
            f"{fixture.base}/adopt",
            headers={
                "Authorization": f"Bearer {fixture.credential}",
                "X-Callback-Id": str(new_uuid7()),
            },
            json={
                "prior_authority": fixture.authority,
                "current_worker_incarnation_id": current["worker_incarnation_id"],
                "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
            },
        )
        assert adopted.status_code == 200, adopted.text
        body = adopted.json()
        assert body["transferred_checkpoint_reservation"] == reserved.json()
        stamps = [body["server_time"], body["lease_expires_at"], reserved.json()["reserved_at"]]
        assert all(MS_TIMESTAMP.match(stamp) for stamp in stamps), stamps


def test_list_job_attempts_is_newest_first_with_a_signed_keyset_cursor(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-read-attempts") as control:
        first = dict(control.job.authority)
        second = _next_attempt(control.job)
        path = f"/v1/jobs/{control.job.job_id}/attempts"
        page = _read(control, path, page_size=1)
        assert page.status_code == 200, page.text
        (item,) = page.json()["items"]
        assert item["attempt_id"] == second["attempt_id"]
        assert (item["attempt_number"], item["job_fence"], item["lease_id"]) == (
            2,
            3,
            second["lease_id"],
        )
        assert item["allocation_id"] == second["allocation_id"]
        assert all(
            MS_TIMESTAMP.match(item[key]) for key in ("created_at",) if item[key] is not None
        )
        cursor = page.json()["page"]["next_cursor"]
        assert cursor is not None and page.json()["page"]["page_size"] == 1

        rest = _read(control, path, page_size=1, cursor=cursor)
        assert rest.status_code == 200, rest.text
        (older,) = rest.json()["items"]
        assert older["attempt_id"] == first["attempt_id"]
        assert (older["state"], older["failure_class"]) == ("FAILED", "INFRASTRUCTURE")
        assert MS_TIMESTAMP.match(older["ended_at"]) and MS_TIMESTAMP.match(older["started_at"])
        assert rest.json()["page"]["next_cursor"] is None

        # A cursor is bound to its job; a hidden job is indistinguishable from none.
        other = _read(control, f"/v1/jobs/{new_uuid7()}/attempts", cursor=cursor)
        assert other.status_code == 404, other.text
        forged = _read(control, path, cursor=cursor[:-4] + "AAAA")
        assert forged.status_code == 400 and forged.json()["code"] == "invalid_cursor"
        assert _read(control, path, page_size=101).status_code == 400


def test_renew_and_read_routes_write_three_digit_timestamps(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-wire-reads") as control:
        control.counters(outstanding=1, active=1)
        renew = _renew(control)
        assert renew.status_code == 200, renew.text
        _assert_wire(renew.json())
        with engine.connect() as connection:
            session_id = connection.execute(
                select(s.logical_sessions.c.session_id).where(
                    s.logical_sessions.c.job_id == control.job.job_id
                )
            ).scalar_one()
        worker = control.worker_client.app.state.services.worker
        complete, payload_hash, _ = _prepare_completion(
            control.worker_client, engine, control.job.credential, control.job.authority, "b15-wire"
        )
        worker.complete_attempt(
            credential=control.job.credential,
            attempt_id=UUID(control.job.authority["attempt_id"]),
            callback_id=new_uuid7(),
            payload_hash=payload_hash,
            request=complete,
        )
        # The job list is checked in test_jobs_b08 (this fixture's template id
        # is not a contract template, so JobPage validation rejects it).
        for path in (
            f"/v1/sessions/{session_id}",
            f"/v1/jobs/{control.job.job_id}/result",
            "/v1/artifacts",
        ):
            response = _read(control, path)
            assert response.status_code == 200, (path, response.text)
            _assert_wire(response.json())
