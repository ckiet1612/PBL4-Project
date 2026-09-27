"""B15 idempotency retention sweep over PostgreSQL: bounds, terminal guard, frozen/leadership."""

from datetime import timedelta

import pytest
from sqlalchemy import func, insert, select, update

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.integration.test_control_b15 import Control, _set_mode
from tests.integration.test_retry_b14 import _job, _leader

pytestmark = pytest.mark.postgres

_HASH = "sha256:" + "0" * 64


def _record(
    connection, control, resource_id, *, operation="submitJob", state="COMPLETED", age_days=1
):
    record_id = new_uuid7()
    connection.execute(
        insert(s.idempotency_records).values(
            idempotency_id=record_id,
            context=str(control.tenant_id),
            principal_id=str(control.users["member"]),
            operation_id=operation,
            idempotency_key=f"b15-retention-{record_id}",
            request_hash=_HASH,
            state=state,
            resource_id=resource_id,
            expires_at=func.now() - timedelta(days=age_days),
        )
    )
    return record_id


def _remaining(engine, record_ids):
    with engine.connect() as connection:
        return set(
            connection.execute(
                select(s.idempotency_records.c.idempotency_id).where(
                    s.idempotency_records.c.idempotency_id.in_(record_ids)
                )
            ).scalars()
        )


def test_sweep_deletes_only_expired_completed_job_records_of_terminal_jobs(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-retention") as control:
        control.counters(outstanding=2, active=1)
        terminal = control.seed()
        key = "b15-retention-cancel-key-01"
        cancelled = control.control("cancel", job_id=terminal, key=key, if_match='"v1"')
        assert cancelled.status_code == 202, cancelled.text
        assert control.control("cancel", job_id=terminal, key=key).status_code == 202
        queued = control.seed()
        with engine.begin() as connection:
            (cancel_record,) = connection.execute(
                update(s.idempotency_records)
                .where(s.idempotency_records.c.idempotency_key == key)
                .values(expires_at=func.now() - timedelta(seconds=1))
                .returning(s.idempotency_records.c.idempotency_id)
            ).scalars()
            swept = {
                cancel_record,
                *(
                    _record(connection, control, terminal, operation=operation)
                    for operation in ("submitJob", "pauseJob", "resumeJob", "retryFailedJob")
                ),
            }
            kept = {
                _record(connection, control, terminal, state="PENDING"),
                _record(connection, control, terminal, age_days=-1),
                _record(connection, control, terminal, operation="uploadJobInput"),
                _record(connection, control, queued),
                _record(connection, control, None),
            }
        service, epoch = _leader(engine)
        assert service.sweep_idempotency(epoch) == len(swept)
        assert _remaining(engine, swept | kept) == kept
        assert service.sweep_idempotency(epoch) == 0
        # Past retention the key is free: the same request is no longer a replay.
        again = control.control("cancel", job_id=terminal, key=key, if_match='"v2"')
        assert again.status_code == 409 and again.json()["code"] == "state_conflict"
        assert _job(engine, terminal)["version"] == 2


def test_sweep_is_bounded_per_transaction(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-retention-batch") as control:
        terminal = control.seed(
            state="CANCELLED", desired_state="CANCELLED", terminal_at=func.now()
        )
        with engine.begin() as connection:
            records = {_record(connection, control, terminal) for _ in range(101)}
        service, epoch = _leader(engine)
        assert service.sweep_idempotency(epoch) == 100
        assert len(_remaining(engine, records)) == 1
        assert service.sweep_idempotency(epoch) == 1
        assert _remaining(engine, records) == set()


def test_sweep_writes_nothing_while_frozen_or_without_leadership(
    migrated_postgres_engine, tmp_path
):
    from nexa.coordinator.service import LeadershipLost

    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b15-retention-frozen") as control:
        terminal = control.seed(state="FAILED", desired_state="RUNNING", terminal_at=func.now())
        with engine.begin() as connection:
            record = _record(connection, control, terminal)
        service, epoch = _leader(engine)
        with pytest.raises(LeadershipLost):
            service.sweep_idempotency(epoch + 1)
        _set_mode(engine, "WRITE_FROZEN")
        assert service.sweep_idempotency(epoch) == 0
        assert _remaining(engine, {record}) == {record}
        _set_mode(engine, "NORMAL")
        with engine.begin() as connection:
            # A draining worker ends the tick after the probes, before any snapshot.
            connection.execute(update(s.workers).values(admin_state="DRAINING"))
        assert service.tick(epoch).reason == "worker_or_mode_unavailable"
        assert _remaining(engine, {record}) == set()
