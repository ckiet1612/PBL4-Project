"""B19 coordinator metrics on PostgreSQL: counts only after commit, ops /readyz with role."""

import json
import socket
import time
import urllib.error
import urllib.request
from datetime import timedelta
from threading import Event, Thread

import pytest
from sqlalchemy import create_engine, func, update

from nexa.coordinator.ops import CoordinatorOps
from nexa.coordinator.runtime import CoordinatorStatus, run
from nexa.coordinator.service import CoordinatorService
from nexa.domain.scheduling import Dispatch, NoDecision
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.observability import metrics_coordinator as m
from tests.integration.test_coordinator_b11 import seed_dispatchable

pytestmark = pytest.mark.postgres


def _value(metric, *labels):
    target = metric.labels(*labels) if labels else metric
    return target._value.get()


def _histogram_count(histogram) -> float:
    for metric in histogram.collect():
        for sample in metric.samples:
            if sample.name.endswith("_count"):
                return sample.value
    return 0.0


def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def test_decisions_dispatch_wait_and_reaped_leases_count_after_commit(
    migrated_postgres_engine, monkeypatch
):
    seed_dispatchable(migrated_postgres_engine, count=2)
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    offers, nones = _value(m.DECISIONS, "offer"), _value(m.DECISIONS, "none")
    waits = _histogram_count(m.DISPATCH_WAIT)

    # A transaction that fails after apply_decision wrote its rows counts nothing.
    import nexa.coordinator.dispatch as dispatch_module

    original = dispatch_module.apply_decision

    def apply_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected failure after apply_decision")

    monkeypatch.setattr(dispatch_module, "apply_decision", apply_then_fail)
    service._retry_probe_at = float("inf")
    with pytest.raises(RuntimeError, match="injected"):
        service.tick(epoch)
    monkeypatch.setattr(dispatch_module, "apply_decision", original)
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(func.count(s.attempts.c.attempt_id)).scalar_one() == 0
    assert _value(m.DECISIONS, "offer") == offers
    assert _histogram_count(m.DISPATCH_WAIT) == waits

    assert isinstance(service.tick(epoch), Dispatch)
    assert _value(m.DECISIONS, "offer") == offers + 1
    assert _histogram_count(m.DISPATCH_WAIT) == waits + 1
    assert isinstance(service.tick(epoch), NoDecision)
    assert _value(m.DECISIONS, "none") >= nones + 1

    expired = _value(m.LEASE_EXPIRED)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.attempt_leases).values(
                expires_at=func.clock_timestamp() - timedelta(seconds=1)
            )
        )
    assert service.reap_leases(epoch) == 1
    assert _value(m.LEASE_EXPIRED) == expired + 1
    assert service.reap_leases(epoch) == 0
    assert _value(m.LEASE_EXPIRED) == expired + 1


def test_maintenance_failure_is_counted_and_does_not_stop_dispatch(
    migrated_postgres_engine, monkeypatch
):
    seed_dispatchable(migrated_postgres_engine)
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    before = _value(m.MAINTENANCE_FAILURES, "promote")

    def broken(_epoch):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(service, "promote_retries", broken)
    assert isinstance(service.tick(epoch), Dispatch)
    assert _value(m.MAINTENANCE_FAILURES, "promote") == before + 1


def test_coordinator_ops_ready_role_and_database_failure(migrated_postgres_engine):
    status = CoordinatorStatus()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    ops = CoordinatorOps(("127.0.0.1", port), migrated_postgres_engine, status)
    ops.start()
    stop = Event()
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    thread = Thread(target=run, args=(service, stop, status), daemon=True)
    try:
        code, body = _get(port, "/readyz")
        assert code == 200
        assert json.loads(body) == {
            "status": "ready",
            "checks": {"database": "ok", "schema": "ok"},
            "role": "standby",
        }
        thread.start()
        # Poll until leadership shows through the two-second readiness cache.
        deadline = time.monotonic() + 15
        while json.loads(_get(port, "/readyz")[1]).get("role") != "leader":
            assert time.monotonic() < deadline, "coordinator never reported leader"
            stop.wait(0.1)
        code, metrics = _get(port, "/metrics")
        assert "nexa_coordinator_leader 1.0" in metrics
        assert "nexa_coordinator_tick_duration_seconds_count" in metrics
        assert 'nexa_ready{check="database"} 1.0' in metrics
        assert _get(port, "/livez")[0] == 200
    finally:
        stop.set()
        if thread.is_alive():
            thread.join(timeout=10)
        ops.stop()
    assert not status.leader
    assert _value(m.LEADER) == 0

    unreachable = create_engine(
        "postgresql+psycopg://nexa@127.0.0.1:9/nexa_b05_test_unreachable",
        connect_args={"connect_timeout": 2},
    )
    probe_ops = CoordinatorOps(("127.0.0.1", port), unreachable, CoordinatorStatus())
    report = probe_ops._probe()
    assert report.checks == {"database": "fail", "schema": "skip"}
    assert not report.ready and report.body()["role"] == "standby"
    unreachable.dispose()


def test_coordinator_metrics_scrape_alone_tracks_readiness(migrated_postgres_engine):
    # B19-RV02: only /metrics is scraped; a schema mismatch shows 0, then 1 after recovery.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    ops = CoordinatorOps(("127.0.0.1", port), migrated_postgres_engine, CoordinatorStatus())
    head = ops._database._head
    ops.start()

    def poll(expected: float) -> None:
        deadline = time.monotonic() + 10
        while f'nexa_ready{{check="schema"}} {expected}' not in _get(port, "/metrics")[1]:
            assert time.monotonic() < deadline, f"schema readiness never became {expected}"
            time.sleep(0.2)

    try:
        poll(1.0)
        ops._database._head = "20990101_9999"
        poll(0.0)
        ops._database._head = head
        poll(1.0)
    finally:
        ops.stop()
