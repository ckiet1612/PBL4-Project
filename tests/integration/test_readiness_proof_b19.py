"""B19-R02: ADMISSION_OFF -> NORMAL needs DB/schema/storage and every worker READY.

Each failed condition is a 409 with the one fixed message and no `reason`; mode and
version stay unchanged. The failed condition is logged, never returned. Freeze and
restore proofs stay fail-closed (B21).
"""

import logging
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from nexa.application.readiness_proof import ReadinessRecoveryProofProvider
from nexa.infrastructure.artifacts.store import ArtifactError
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.observability.logging import _CONTEXT
from tests.integration.test_admin_workers_b15 import _admin_audits, _reconcile
from tests.integration.test_control_b15 import Control, _set_mode
from tests.integration.test_worker_api_b10 import WORKER_ID, _inventory
from tests.integration.test_worker_authority_b10 import CONTAINER_ID, DIGEST

pytestmark = pytest.mark.postgres

REOPEN_REFUSED = "Readiness and worker reconciliation are required"


def _policy(engine):
    with engine.connect() as connection:
        return (
            connection.execute(
                select(s.policy_versions).where(s.policy_versions.c.is_current.is_(True))
            )
            .mappings()
            .one()
        )


def _patch_mode(control, mode, *, key=None):
    headers = {
        **control.headers,
        "If-Match": f'"v{_policy(control.engine)["policy_version"]}"',
        "Idempotency-Key": key or f"b19-mode-{new_uuid7()}",
    }
    return control.client.patch(
        "/v1/admin/policy", headers=headers, json={"operational_mode": mode}
    )


def _ready(control):
    """A reconciled worker whose latest heartbeat passed every READY check."""
    assert _reconcile(control).status_code == 200
    heartbeat = control.worker_client.post(
        f"/v1/workers/{WORKER_ID}/heartbeat",
        headers={
            "Authorization": f"Bearer {control.job.credential}",
            "X-Callback-Id": str(new_uuid7()),
        },
        json={
            "worker_incarnation_id": control.job.authority["worker_incarnation_id"],
            "observed_health": "READY",
            "reconcile_complete": True,
            "inventory": _inventory(),
            # The RUNNING fixture Attempt's container is still observed.
            "observed_containers": [
                {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST}
            ],
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    with control.engine.connect() as connection:
        worker = (
            connection.execute(select(s.workers).where(s.workers.c.worker_id == UUID(WORKER_ID)))
            .mappings()
            .one()
        )
    assert (worker["admin_state"], worker["health"]) == ("ENABLED", "READY")


class _Refusals(logging.Handler):
    """Captures refusal log fields and the log context bound at emit time."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.seen: list[tuple[str, str | None]] = []

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "nexa_fields", {})
        if fields.get("event") == "mode_reopen_refused":
            self.seen.append((fields["failed"], dict(_CONTEXT.get()).get("request_id")))


@contextmanager
def _capture():
    logger = logging.getLogger("nexa.application.readiness_proof")
    # The in-process Alembic fixture's fileConfig disables loggers created before it.
    disabled, logger.disabled = logger.disabled, False
    handler = _Refusals()
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.disabled = disabled


def _refused(control, failed):
    before = _policy(control.engine)
    with _capture() as refusals:
        response = _patch_mode(control, "NORMAL")
    assert response.status_code == 409, response.text
    body = response.json()
    assert (body["code"], body["message"]) == ("state_conflict", REOPEN_REFUSED)
    assert "reason" not in body
    after = _policy(control.engine)
    assert (after["policy_version"], after["operational_mode"]) == (
        before["policy_version"],
        "ADMISSION_OFF",
    )
    assert refusals.seen == [(failed, response.headers["X-Request-Id"])]


@pytest.fixture
def control(migrated_postgres_engine, tmp_path):
    with Control(migrated_postgres_engine, tmp_path, label="b19-reopen") as control:
        control.login("admin")
        # Like Control's job service, the admin app probes the one verified store.
        control.client.app.state.services.policy.proofs._store = control.store
        _ready(control)
        _set_mode(migrated_postgres_engine, "ADMISSION_OFF")
        yield control


def _workers(engine, **values):
    with engine.begin() as connection:
        connection.execute(update(s.workers).values(**values))


def _incarnations(engine, **values):
    with engine.begin() as connection:
        connection.execute(update(s.worker_incarnations).values(**values))


@pytest.mark.parametrize(
    ("change", "failed"),
    [
        (lambda e: _workers(e, admin_state="DRAINING"), "worker_not_enabled"),
        (lambda e: _workers(e, admin_state="DISABLED"), "worker_not_enabled"),
        (lambda e: _workers(e, health="SUSPECT"), "worker_not_ready"),
        (
            lambda e: _workers(e, last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=31)),
            "worker_heartbeat_stale",
        ),
        (
            lambda e: _incarnations(e, ready_checked_at=datetime.now(UTC) - timedelta(hours=1)),
            "worker_heartbeat_not_ready",
        ),
        (lambda e: _incarnations(e, reconciliation_drained=False), "worker_not_reconciled"),
        (lambda e: _workers(e, current_inventory_version=None), "worker_inventory"),
    ],
    ids=["draining", "disabled", "suspect", "stale", "unchecked", "unreconciled", "inventory"],
)
def test_each_failed_worker_condition_is_a_409_and_keeps_the_mode(control, change, failed):
    change(control.engine)
    _refused(control, failed)


def test_storage_and_schema_failures_refuse_the_reopen(control, monkeypatch):
    services = control.client.app.state.services
    store = control.store

    def broken(**_):
        raise ArtifactError("storage_unavailable", "probe failed")

    monkeypatch.setattr(store, "check_readiness", broken)
    _refused(control, "storage")
    monkeypatch.undo()
    monkeypatch.setattr(services.policy.proofs, "_head", "not-the-head")
    _refused(control, "schema")


def test_a_ready_system_reopens_with_audit_even_with_quarantine(control):
    # B19-R02: QUARANTINED allocations stay charged to capacity; the provider adds no
    # quarantine condition (the next heartbeat would report the worker not READY).
    with control.engine.begin() as connection:
        connection.execute(
            update(s.allocations).values(state="QUARANTINED", quarantined_at=datetime.now(UTC))
        )
    before = _policy(control.engine)
    key = f"b19-reopen-{new_uuid7()}"
    with _capture() as refusals:
        reopened = _patch_mode(control, "NORMAL", key=key)
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["operational_mode"] == "NORMAL"
    assert reopened.json()["version"] == before["policy_version"] + 1
    assert refusals.seen == []
    audits = _admin_audits(control.engine, "admin.policy.global.update")
    assert [a["after_version"] for a in audits] == [before["policy_version"] + 1]
    # A replay answers from the stored response even once readiness is lost.
    _workers(control.engine, health="SUSPECT")
    replay = control.client.patch(
        "/v1/admin/policy",
        headers={**control.headers, "If-Match": '"v1"', "Idempotency-Key": key},
        json={"operational_mode": "NORMAL"},
    )
    assert (replay.status_code, replay.json()) == (200, reopened.json())


def test_worker_rows_are_share_locked_until_the_mode_commits(control, monkeypatch):
    proofs = control.client.app.state.services.policy.proofs
    real = proofs.readiness_verified
    observed = []

    def verified(session):
        result = real(session)
        # A heartbeat takes the worker row FOR UPDATE: it must wait for this commit.
        with control.engine.connect() as other:
            try:
                other.execute(text("SELECT worker_id FROM workers FOR UPDATE NOWAIT"))
            except OperationalError as exc:
                observed.append(getattr(exc.orig, "sqlstate", None))
        return result

    monkeypatch.setattr(proofs, "readiness_verified", verified)
    assert _patch_mode(control, "NORMAL").status_code == 200
    assert observed == ["55P03"]  # lock_not_available


def test_frozen_and_freeze_transitions_stay_fail_closed(control):
    _workers(control.engine, health="READY")
    frozen = _patch_mode(control, "WRITE_FROZEN")
    assert frozen.status_code == 409
    assert frozen.json()["message"] == (
        "Freeze requires stopped containers and reconciled unreleased allocations"
    )
    _set_mode(control.engine, "WRITE_FROZEN")
    for target, message in [
        ("NORMAL", "Only a verified restore transition may mutate frozen policy"),
        ("ADMISSION_OFF", "Restore verification is required before leaving frozen mode"),
    ]:
        response = _patch_mode(control, target)
        assert (response.status_code, response.json()["message"]) == (409, message)
        assert "reason" not in response.json()
    assert _policy(control.engine)["operational_mode"] == "WRITE_FROZEN"


def test_no_worker_is_not_ready(migrated_postgres_engine):
    store = type("Store", (), {"check_readiness": lambda self, **_: None})()
    settings = SimpleNamespace(storage_critical_watermark_percent=95)
    provider = ReadinessRecoveryProofProvider(settings, store)
    with Session(migrated_postgres_engine) as session:
        assert session.execute(select(s.workers.c.worker_id)).first() is None
        assert provider.readiness_verified(session) is False
        assert provider._failure(session) == "no_worker"
        assert provider.freeze_ready(session) is False
        assert provider.restore_verified(session) is False
    assert provider.storage_ready() is True


def test_every_worker_must_be_ready(control):
    # A second, never-started worker identity blocks the reopen too.
    with control.engine.begin() as connection:
        connection.execute(
            insert(s.workers).values(
                worker_id=new_uuid7(), admin_state="ENABLED", health="STARTING"
            )
        )
    _refused(control, "worker_not_ready")


def _counting_probe(control, monkeypatch):
    policy = control.client.app.state.services.policy
    real, calls = policy.storage_probe, []

    def probe():
        calls.append(1)
        return real()

    monkeypatch.setattr(policy, "storage_probe", probe)
    return calls


def test_storage_is_probed_only_for_an_authorized_fresh_reopen(control, monkeypatch):
    # B19-RV05: the fsync probe runs after authorization and idempotency replay, and only
    # for ADMISSION_OFF -> NORMAL; it stays outside the transaction.
    calls = _counting_probe(control, monkeypatch)
    control.login("member")
    assert _patch_mode(control, "NORMAL").status_code == 403
    assert calls == []

    control.login("admin")
    key = f"b19-probe-{new_uuid7()}"
    reopened = _patch_mode(control, "NORMAL", key=key)
    assert reopened.status_code == 200, reopened.text
    assert calls == [1]
    headers = {
        **control.headers,
        "If-Match": f'"v{_policy(control.engine)["policy_version"] - 1}"',
        "Idempotency-Key": key,
    }
    replayed = control.client.patch(
        "/v1/admin/policy", headers=headers, json={"operational_mode": "NORMAL"}
    )
    assert replayed.status_code == 200 and replayed.json() == reopened.json()
    assert calls == [1]

    assert _patch_mode(control, "ADMISSION_OFF").status_code == 200
    assert _patch_mode(control, "NORMAL", key=f"b19-probe-{new_uuid7()}").status_code == 200
    assert calls == [1, 1]
    assert _patch_mode(control, "NORMAL").status_code == 200  # NORMAL -> NORMAL: no reopen
    assert calls == [1, 1]
