"""B15-R18 residual: an expired record that is not yet due is re-examined later, not each tick.

The retention rule is unchanged (contracts.md "Idempotency"): records of a live Job
stay, and a terminal Job keeps its records `terminal_at + retention`.
"""

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.schema_v16 import SWEPT_OPERATIONS
from tests.integration.test_control_b15 import Control, _set_mode
from tests.integration.test_retention_b15 import _record, _remaining
from tests.integration.test_retry_b14 import _job, _leader

pytestmark = pytest.mark.postgres

records = s.idempotency_records


def _walked(engine):
    """Records the sweep walk would read now: expired COMPLETED job-request records."""
    with engine.connect() as connection:
        return set(
            connection.execute(
                select(records.c.idempotency_id).where(
                    records.c.state == "COMPLETED",
                    records.c.operation_id.in_(SWEPT_OPERATIONS),
                    records.c.expires_at < func.now(),
                )
            ).scalars()
        )


def _expires(engine, record_id):
    with engine.connect() as connection:
        return connection.execute(
            select(records.c.expires_at).where(records.c.idempotency_id == record_id)
        ).scalar_one()


def test_an_expired_record_of_a_live_job_leaves_the_walk_until_it_can_be_due(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="rem-r18-defer") as control:
        queued = control.seed()
        terminal = control.seed(
            state="CANCELLED", desired_state="CANCELLED", terminal_at=func.now()
        )
        with engine.begin() as connection:
            live = _record(connection, control, queued)
            orphan = _record(connection, control, None)
            gone = _record(connection, control, terminal)
        assert _walked(engine) == {live, orphan, gone}
        service, epoch = _leader(engine)
        assert service.sweep_idempotency(epoch) == 1
        assert _remaining(engine, {live, orphan, gone}) == {live, orphan}
        # Kept, and no longer read by every tick.
        assert _walked(engine) == set()
        deferred = {record: _expires(engine, record) for record in (live, orphan)}
        assert service.sweep_idempotency(epoch) == 0
        assert {record: _expires(engine, record) for record in (live, orphan)} == deferred


def test_a_deferred_record_still_gets_exactly_the_terminal_retention(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="rem-r18-exact") as control:
        control.counters(outstanding=2, active=1)
        queued = control.seed()
        with engine.begin() as connection:
            live = _record(connection, control, queued)
        service, epoch = _leader(engine)
        assert service.sweep_idempotency(epoch) == 0
        assert _walked(engine) == set()

        cancelled = control.control("cancel", job_id=queued, if_match='"v1"')
        assert cancelled.status_code == 202, cancelled.text
        job = _job(engine, queued)
        assert job["state"] == "CANCELLED"
        # The terminal path wins over the deferral: terminal_at + 30 days, as before.
        assert _expires(engine, live) == job["terminal_at"] + timedelta(days=30)
        assert service.sweep_idempotency(epoch) == 0
        assert _remaining(engine, {live}) == {live}


def test_each_tick_examines_one_bounded_batch_of_expired_records(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="rem-r18-batch") as control:
        queued = control.seed()
        terminal = control.seed(
            state="CANCELLED", desired_state="CANCELLED", terminal_at=func.now()
        )
        with engine.begin() as connection:
            live = {_record(connection, control, queued, age_days=2) for _ in range(150)}
            # Expires after every live record, so it is walked last.
            gone = _record(connection, control, terminal, age_days=1)
        service, epoch = _leader(engine)
        assert service.sweep_idempotency(epoch) == 0
        assert len(_walked(engine)) == 51
        assert service.sweep_idempotency(epoch) == 1
        assert _walked(engine) == set()
        assert _remaining(engine, live | {gone}) == live
        assert service.sweep_idempotency(epoch) == 0


def test_a_deferral_is_a_write_that_needs_leadership_and_a_normal_mode(
    migrated_postgres_engine, tmp_path
):
    from nexa.coordinator.service import LeadershipLost

    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="rem-r18-frozen") as control:
        queued = control.seed()
        with engine.begin() as connection:
            live = _record(connection, control, queued)
        expired = _expires(engine, live)
        service, epoch = _leader(engine)
        with pytest.raises(LeadershipLost):
            service.sweep_idempotency(epoch + 1)
        _set_mode(engine, "WRITE_FROZEN")
        assert service.sweep_idempotency(epoch) == 0
        assert _expires(engine, live) == expired
        assert _walked(engine) == {live}
        _set_mode(engine, "NORMAL")
        assert service.sweep_idempotency(epoch) == 0
        assert _walked(engine) == set()


def test_a_sweep_parent_record_waits_for_its_last_child_record_outside_the_walk(
    migrated_postgres_engine, tmp_path, monkeypatch
):
    from tests.api.test_http_contract import _client
    from tests.integration.test_rem_b16_r05_sweep_retention import _expire_everything
    from tests.integration.test_sweep_b16 import (
        _crash_after,
        _crashed_post,
        _dataset,
        _members,
        _post,
        _request,
    )
    from tests.integration.test_templates_b16 import _register_all

    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _other), write = _members(client, engine)
        body = _request(_dataset(engine, tenant_id))
        finished = _post(client, write, tenant_id, "rem-r18-sweep-0001", body)
        assert finished.status_code == 207, finished.text
        _crash_after(monkeypatch, 1)
        _crashed_post(client, write, tenant_id, "rem-r18-sweep-0002", body)
        monkeypatch.undo()
    _expire_everything(engine)
    with engine.connect() as connection:
        parents = dict(
            connection.execute(
                select(records.c.resource_id, records.c.idempotency_id).where(
                    records.c.operation_id == "submitSweep"
                )
            ).all()
        )
    assert len(parents) == 2
    service, epoch = _leader(engine)
    # Children first, then the finished parent follows its children's new expiry.
    assert [service.sweep_idempotency(epoch) for _ in range(3)] == [0, 0, 0]
    assert _walked(engine) == set()
    with engine.connect() as connection:
        still_walked = connection.execute(
            select(func.count()).where(
                records.c.operation_id == "submitSweep", records.c.expires_at < func.now()
            )
        ).scalar_one()
        child_expiry = connection.execute(
            select(func.max(records.c.expires_at))
            .select_from(
                records.join(s.sweep_children, s.sweep_children.c.job_id == records.c.resource_id)
            )
            .where(
                records.c.operation_id == "submitJob",
                s.sweep_children.c.sweep_id == finished.json()["sweep_id"],
            )
        ).scalar_one()
    assert still_walked == 0
    assert _expires(engine, parents[UUID(finished.json()["sweep_id"])]) == child_expiry
