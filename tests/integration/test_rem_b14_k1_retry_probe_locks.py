"""B14-K1: the retry probe takes decision locks only when promotion has work to write.

A due retry whose block reason is unchanged is re-evaluated each second without the
leadership, policy, GLOBAL counter, job or schedule locks. Retry semantics are
unchanged: a changed reason is still announced once, a compatible retry is promoted
once, and every write still rechecks leadership, mode and the job under locks.
"""

import re
import time
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select, update

from nexa.coordinator import retry
from nexa.infrastructure.persistence import schema as s
from tests.integration.test_control_b15 import Control
from tests.integration.test_coordinator_b11 import seed_dispatchable
from tests.integration.test_retry_b14 import _job, _job_events, _leader, _retry_wait, _schedule

pytestmark = pytest.mark.postgres

_LOCKING = re.compile(r"^\s*(UPDATE|INSERT|DELETE)\b|\bFOR (NO KEY )?(UPDATE|SHARE)\b", re.I)


@contextmanager
def _locking_statements(engine):
    """Row-locking reads and writes the coordinator sends while the block is open."""
    seen = []

    def capture(_connection, _cursor, statement, *_args):
        if _LOCKING.search(statement):
            seen.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def _cpu_limit(engine, job_id, value):
    with engine.begin() as connection:
        tenant_id = connection.execute(
            select(s.jobs.c.tenant_id).where(s.jobs.c.job_id == job_id)
        ).scalar_one()
        connection.execute(
            update(s.tenant_policies)
            .where(s.tenant_policies.c.tenant_id == tenant_id)
            .values(cpu_limit_millis=value)
        )


def _blocked_retry(engine):
    """A due retry that the tenant quota blocks, already announced as blocked."""
    _, _, (job_id,) = seed_dispatchable(engine)
    _cpu_limit(engine, job_id, 0)
    _retry_wait(engine, job_id, ready_in=timedelta(seconds=-1))
    service, epoch = _leader(engine)
    with _locking_statements(engine) as first:
        assert service.promote_retries(epoch) == 0
    # A new reason is work: it is written under the locks and announced once.
    assert any("FOR UPDATE" in statement for statement in first)
    assert _job_events(engine, job_id)[-1] == ("RETRY_BLOCKED", "waiting_for_quota", "COORDINATOR")
    return service, epoch, job_id


def test_an_unchanged_blocked_retry_is_probed_without_any_lock(migrated_postgres_engine):
    engine = migrated_postgres_engine
    service, epoch, job_id = _blocked_retry(engine)
    blocked = _job(engine, job_id)
    events = _job_events(engine, job_id)
    with _locking_statements(engine) as seen:
        for _ in range(3):
            assert service.promote_retries(epoch) == 0
    assert seen == []
    assert _job(engine, job_id)["version"] == blocked["version"]
    assert _job_events(engine, job_id) == events
    assert _schedule(engine, job_id)["closed_at"] is None


def test_the_idle_probe_does_not_wait_for_a_held_policy_lock(migrated_postgres_engine):
    engine = migrated_postgres_engine
    service, epoch, job_id = _blocked_retry(engine)
    with engine.connect() as holder:
        transaction = holder.begin()
        holder.execute(
            select(s.policy_versions.c.policy_version)
            .where(s.policy_versions.c.is_current.is_(True))
            .with_for_update()
        )
        started = time.monotonic()
        assert service.promote_retries(epoch) == 0
        # Well under the 1000 ms lock_timeout the locked path would wait for.
        assert time.monotonic() - started < 0.5
        transaction.rollback()
    # Work appears: the retry fits again and is promoted once, under the locks.
    _cpu_limit(engine, job_id, 1_000_000)
    assert service.promote_retries(epoch) == 1
    promoted = _job(engine, job_id)
    assert promoted["state"] == "QUEUED"
    assert [event[0] for event in _job_events(engine, job_id)].count("RETRY_READY") == 1
    assert _schedule(engine, job_id)["closed_at"] is not None
    assert service.promote_retries(epoch) == 0


def test_a_cancel_committed_after_the_probe_wins_over_promotion(
    migrated_postgres_engine, tmp_path, monkeypatch
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="rem-k1-cancel") as control:
        control.counters(outstanding=2, active=1)
        job_id = control.seed()
        _retry_wait(engine, job_id, ready_in=timedelta(seconds=-1))
        service, epoch = _leader(engine)
        probe = retry.retry_actionable

        def cancel_after_probe(session, now, **kwargs):
            actionable = probe(session, now, **kwargs)
            assert actionable
            cancelled = control.control("cancel", job_id=job_id)
            assert cancelled.status_code == 202, cancelled.text
            return actionable

        monkeypatch.setattr(retry, "retry_actionable", cancel_after_probe)
        assert service.promote_retries(epoch) == 0
        monkeypatch.undo()
        job = _job(engine, job_id)
        assert (job["state"], job["desired_state"]) == ("CANCELLED", "CANCELLED")
        kinds = [event[0] for event in _job_events(engine, job_id)]
        assert "RETRY_READY" not in kinds and "RETRY_BLOCKED" not in kinds
        assert service.promote_retries(epoch) == 0


def test_a_leader_change_after_the_probe_writes_nothing_and_the_new_leader_promotes_once(
    migrated_postgres_engine, monkeypatch
):
    from nexa.coordinator.service import LeadershipLost

    engine = migrated_postgres_engine
    _, _, (job_id,) = seed_dispatchable(engine)
    _retry_wait(engine, job_id, ready_in=timedelta(seconds=-1))
    stale, stale_epoch = _leader(engine)
    probe = retry.retry_actionable
    successor = {}

    def take_over_after_probe(session, now, **kwargs):
        actionable = probe(session, now, **kwargs)
        with engine.begin() as connection:
            connection.execute(update(s.coordinator_leadership).values(lease_expires_at=func.now()))
        successor["service"], successor["epoch"] = _leader(engine)
        return actionable

    monkeypatch.setattr(retry, "retry_actionable", take_over_after_probe)
    with pytest.raises(LeadershipLost):
        stale.promote_retries(stale_epoch)
    monkeypatch.undo()
    assert successor["epoch"] == stale_epoch + 1
    assert _job(engine, job_id)["state"] == "RETRY_WAIT"
    assert _schedule(engine, job_id)["closed_at"] is None
    assert successor["service"].promote_retries(successor["epoch"]) == 1
    assert [event[0] for event in _job_events(engine, job_id)].count("RETRY_READY") == 1
