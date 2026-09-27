"""B15-R27: worker callbacks lock the worker row before inserting their receipt."""

import threading
import time

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from nexa.application.worker_service import WorkerService
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.integration.test_coordinator_b11 import seed_dispatchable

pytestmark = pytest.mark.postgres


def _callback(session, worker_id, operation_id):
    return WorkerService._begin_callback(
        session,
        worker_id=worker_id,
        operation_id=operation_id,
        callback_id=new_uuid7(),
        payload_hash="sha256:" + "0" * 64,
    )


def _lock_worker(session, worker_id):
    session.execute(
        select(s.workers.c.worker_id).where(s.workers.c.worker_id == worker_id).with_for_update()
    ).one()


def test_two_callbacks_of_one_worker_serialise_without_a_deadlock(migrated_postgres_engine):
    engine = migrated_postgres_engine
    seed_dispatchable(engine, count=0)
    with engine.connect() as connection:
        worker_id = connection.execute(select(s.workers.c.worker_id)).scalar_one()

    first = Session(engine)
    errors: list[BaseException] = []
    pid: list[int] = []

    def second() -> None:
        try:
            with Session(engine) as session, session.begin():
                pid.append(session.execute(text("select pg_backend_pid()")).scalar_one())
                session.execute(text("set local deadlock_timeout = '100ms'"))
                _callback(session, worker_id, "workerRenewLease")
                _lock_worker(session, worker_id)
        except BaseException as error:  # noqa: BLE001 - reported to the main thread
            errors.append(error)

    try:
        with first.begin():
            first.execute(text("set local deadlock_timeout = '100ms'"))
            _callback(first, worker_id, "workerHeartbeat")
            thread = threading.Thread(target=second)
            thread.start()
            deadline = time.monotonic() + 10
            while True:
                with engine.connect() as probe:
                    waiting = (
                        pid
                        and probe.execute(
                            text("select wait_event_type from pg_stat_activity where pid = :pid"),
                            {"pid": pid[0]},
                        ).scalar_one_or_none()
                        == "Lock"
                    )
                if waiting:
                    break
                assert time.monotonic() < deadline, "second callback never waited"
                time.sleep(0.01)
            # The first callback now takes the lock that _worker_auth takes.
            _lock_worker(first, worker_id)
    except BaseException as error:  # noqa: BLE001
        errors.append(error)
    finally:
        first.close()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert [type(error).__name__ for error in errors] == []


def _hold_submitter(engine, job_id):
    """A request transaction holds the submitter row as _revalidate_principal does."""
    holder = engine.connect()
    transaction = holder.begin()
    holder.execute(
        select(s.users.c.user_id)
        .join(s.jobs, s.jobs.c.submitter_user_id == s.users.c.user_id)
        .where(s.jobs.c.job_id == job_id)
        .with_for_update(of=s.users)
    ).one()
    return holder, transaction


def test_dispatch_updates_the_job_once_and_ignores_a_locked_submitter(migrated_postgres_engine):
    from nexa.domain.scheduling import Dispatch
    from tests.integration.test_retry_b14 import _job, _leader

    engine = migrated_postgres_engine
    _, _, (job_id,) = seed_dispatchable(engine)
    service, epoch = _leader(engine)
    holder, transaction = _hold_submitter(engine, job_id)
    try:
        # A second UPDATE of a row version this transaction created re-checks its
        # foreign keys (KEY SHARE on users) and waits behind the request (B15-R04).
        decision = service.tick(epoch)
    finally:
        transaction.rollback()
        holder.close()
    assert isinstance(decision, Dispatch)
    assert _job(engine, job_id)["state"] == "DISPATCHING"


def test_retry_promotion_and_block_update_the_job_once(migrated_postgres_engine):
    from datetime import timedelta

    from sqlalchemy import update

    from nexa.coordinator.retry import promote_due_locked
    from tests.integration.test_retry_b14 import _job, _leader, _retry_wait

    engine = migrated_postgres_engine
    _, _, (job_id,) = seed_dispatchable(engine)
    _retry_wait(engine, job_id, ready_in=timedelta(seconds=-1))
    service, epoch = _leader(engine)
    with engine.begin() as connection:
        limit = connection.execute(select(s.tenant_policies.c.cpu_limit_millis)).scalar_one()
        connection.execute(update(s.tenant_policies).values(cpu_limit_millis=1))
    holder, transaction = _hold_submitter(engine, job_id)
    try:
        assert service.promote_retries(epoch) == 0
    finally:
        transaction.rollback()
        holder.close()
    assert _job(engine, job_id)["waiting_reason"] == "waiting_for_quota"

    with engine.begin() as connection:
        connection.execute(update(s.tenant_policies).values(cpu_limit_millis=limit))
    # Promotion to QUEUED still takes a users KEY SHARE through the B13
    # queue_submitters trigger (B15-R32); the job row itself is updated once.
    updated = text("select pg_stat_get_xact_tuples_updated('jobs'::regclass)")
    with Session(engine) as session, session.begin():
        before = session.execute(updated).scalar_one()
        now = session.execute(select(func.clock_timestamp())).scalar_one()
        assert promote_due_locked(session, now=now, holder_id=new_uuid7()) == 1
        assert session.execute(updated).scalar_one() - before == 1
    assert _job(engine, job_id)["state"] == "QUEUED"
