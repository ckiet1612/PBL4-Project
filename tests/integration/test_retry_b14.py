"""B14 retry admission after verified cleanup and minimal RETRY_WAIT promotion.

Database integration only; no Docker. B15 owns cancel/pause of waiting retries.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, insert, select, update

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration.test_checkpoint_b14 import CheckpointFixture
from tests.integration.test_checkpoint_restore_b14 import _commit
from tests.integration.test_coordinator_b11 import seed_dispatchable
from tests.integration.test_worker_authority_b10 import CONTAINER_ID, DIGEST

pytestmark = pytest.mark.postgres


def _counters(fixture):
    with fixture.engine.begin() as connection:
        job = (
            connection.execute(select(s.jobs).where(s.jobs.c.job_id == fixture.job_id))
            .mappings()
            .one()
        )
        for scope, scope_id in (
            ("GLOBAL", "global"),
            ("TENANT", str(job["tenant_id"])),
            ("USER", f"{job['tenant_id']}:{job['submitter_user_id']}"),
        ):
            connection.execute(
                insert(s.admission_counters).values(
                    scope_type=scope, scope_id=scope_id, outstanding=1, active_attempts=1
                )
            )


def _fail_and_cleanup(fixture):
    """INFRASTRUCTURE failure with a stopped container, then verified cleanup."""
    _counters(fixture)
    authority = fixture.authority
    nonce = fixture.rows()["attempt"]["startup_nonce"]
    container = {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST}
    failed = fixture.post(
        "/fail",
        {
            "authority": authority,
            "failure_class": "INFRASTRUCTURE",
            "reason_code": "EXECUTOR_UNAVAILABLE",
            "observation": {
                "observation_type": "CONTAINER",
                "container": container,
                "observed_at": datetime.now(UTC).isoformat(),
                "exit_code": 137,
                "oom_killed": False,
                "runtime_limit_reached": False,
            },
        },
    )
    assert failed.status_code == 200, failed.text
    proof = {
        "proof_type": "CONTAINER_STOPPED",
        "startup_nonce": str(nonce),
        "executor_operation_sequence": 2,
        "container": container,
        "stopped_at": datetime.now(UTC).isoformat(),
        "exit_code": 137,
        "inspection_checksum": "sha256:" + "c" * 64,
    }
    cleanup = fixture.post(
        "/cleanup", {**{k: v for k, v in authority.items() if k != "lease_id"}, "proof": proof}
    )
    assert cleanup.status_code == 200, cleanup.text
    assert cleanup.json()["allocation_state"] == "RELEASED"
    with fixture.engine.connect() as connection:
        schedules = (
            connection.execute(
                select(s.retry_schedules).where(s.retry_schedules.c.job_id == fixture.job_id)
            )
            .mappings()
            .all()
        )
        outstanding = connection.execute(
            select(s.admission_counters.c.outstanding).where(
                s.admission_counters.c.scope_type == "GLOBAL"
            )
        ).scalar_one()
    return fixture.rows()["job"], schedules, outstanding


def _mark_corrupt(fixture, checkpoint_id):
    with fixture.engine.begin() as connection:
        connection.execute(
            insert(s.checkpoint_corruptions).values(
                checkpoint_id=UUID(checkpoint_id),
                tenant_id=fixture.graph["tenant_id"],
                reason_code="CHECKPOINT_CHECKSUM_MISMATCH",
            )
        )


@pytest.mark.parametrize("restart_safe", [False, True])
def test_cleanup_retries_when_a_committed_checkpoint_or_restart_safe(
    migrated_postgres_engine, tmp_path, restart_safe
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        fixture = CheckpointFixture(
            migrated_postgres_engine,
            client,
            label="retry-safe" if restart_safe else "retry-unsafe",
            restart_safe=restart_safe,
        )
        if not restart_safe:
            _commit(fixture, step=10, accumulator=5)
        job, schedules, outstanding = _fail_and_cleanup(fixture)
    assert job["state"] == "RETRY_WAIT"
    assert job["retry_count"] == 1
    assert job["waiting_reason"] == "waiting_for_retry"
    assert len(schedules) == 1 and schedules[0]["closed_at"] is None
    assert job["retry_ready_at"] == schedules[0]["ready_at"]
    assert job["eligible_since"] is None
    assert outstanding == 1


@pytest.mark.parametrize("checkpoint", ["none", "corrupt"])
def test_cleanup_fails_non_restart_safe_job_without_valid_checkpoint(
    migrated_postgres_engine, tmp_path, checkpoint
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        fixture = CheckpointFixture(
            migrated_postgres_engine, client, label=f"noretry-{checkpoint}", restart_safe=False
        )
        if checkpoint == "corrupt":
            committed = _commit(fixture, step=10, accumulator=5)
            _mark_corrupt(fixture, committed["record"]["checkpoint_id"])
        job, schedules, outstanding = _fail_and_cleanup(fixture)
    assert job["state"] == "FAILED"
    assert job["retry_count"] == 0
    assert job["retry_ready_at"] is None
    assert schedules == []
    assert outstanding == 0


def test_cleanup_with_exhausted_budget_fails_even_with_checkpoint(
    migrated_postgres_engine, tmp_path
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        fixture = CheckpointFixture(
            migrated_postgres_engine, client, label="exhausted", restart_safe=False
        )
        _commit(fixture, step=10, accumulator=5)
        with fixture.engine.begin() as connection:
            connection.execute(
                update(s.jobs).where(s.jobs.c.job_id == fixture.job_id).values(retry_count=2)
            )
        job, schedules, outstanding = _fail_and_cleanup(fixture)
    assert job["state"] == "FAILED"
    assert job["retry_count"] == 2
    assert schedules == []
    assert outstanding == 0


# Promotion RETRY_WAIT -> QUEUED.


def _retry_wait(engine, job_id, *, ready_in, **changes):
    with engine.begin() as connection:
        job = connection.execute(select(s.jobs).where(s.jobs.c.job_id == job_id)).mappings().one()
        ready_at = connection.execute(select(func.clock_timestamp())).scalar_one() + ready_in
        connection.execute(
            update(s.jobs)
            .where(s.jobs.c.job_id == job_id)
            .values(
                state="RETRY_WAIT",
                retry_count=1,
                job_fence=2,
                retry_ready_at=ready_at,
                waiting_reason="waiting_for_retry",
                eligible_since=None,
                **changes,
            )
        )
        connection.execute(
            insert(s.retry_schedules).values(
                job_id=job_id,
                tenant_id=job["tenant_id"],
                retry_number=1,
                ready_at=ready_at,
                jitter_milliseconds=0,
                reason="INFRASTRUCTURE",
            )
        )
    return ready_at


def _job(engine, job_id):
    with engine.connect() as connection:
        return connection.execute(select(s.jobs).where(s.jobs.c.job_id == job_id)).mappings().one()


def _global_counter(engine):
    with engine.connect() as connection:
        return (
            connection.execute(
                select(s.admission_counters).where(
                    s.admission_counters.c.scope_type == "GLOBAL",
                    s.admission_counters.c.scope_id == "global",
                )
            )
            .mappings()
            .one()
        )


def _job_events(engine, job_id):
    with engine.connect() as connection:
        return connection.execute(
            select(s.events.c.event_type, s.events.c.reason, s.events.c.actor_type)
            .where(s.events.c.job_id == job_id)
            .order_by(s.events.c.sequence)
        ).all()


def _schedule(engine, job_id):
    with engine.connect() as connection:
        return (
            connection.execute(
                select(s.retry_schedules).where(s.retry_schedules.c.job_id == job_id)
            )
            .mappings()
            .one()
        )


def _leader(engine):
    from nexa.coordinator.service import CoordinatorService

    service = CoordinatorService(create_session_factory(engine), holder_id=new_uuid7())
    epoch = service.acquire()
    assert epoch is not None
    return service, epoch


def test_due_retry_is_promoted_once_then_dispatched_with_a_new_fence(migrated_postgres_engine):
    from nexa.domain.scheduling import Dispatch

    engine = migrated_postgres_engine
    _, _, (job_id,) = seed_dispatchable(engine)
    ready_at = _retry_wait(engine, job_id, ready_in=timedelta(seconds=-1))
    before_job = _job(engine, job_id)
    before_counter = _global_counter(engine)
    service, epoch = _leader(engine)

    assert service.promote_retries(epoch) == 1
    job = _job(engine, job_id)
    counter = _global_counter(engine)
    assert job["state"] == "QUEUED"
    assert job["retry_count"] == 1
    assert job["waiting_reason"] is None
    assert job["ready_sequence"] == before_counter["version"]
    assert job["version"] == before_job["version"] + 1
    assert job["event_sequence"] == before_job["event_sequence"] + 1
    assert job["eligible_since"] >= ready_at
    assert counter["version"] == before_counter["version"] + 1
    assert counter["outstanding"] == before_counter["outstanding"]
    assert counter["active_attempts"] == before_counter["active_attempts"]
    assert _schedule(engine, job_id)["closed_at"] is not None
    assert _job_events(engine, job_id)[-1] == ("RETRY_READY", "BACKOFF_ELAPSED", "COORDINATOR")
    with engine.connect() as connection:
        assert connection.execute(
            select(s.queue_heads.c.candidate_job_id, s.queue_heads.c.ready_sequence)
        ).one() == (job_id, job["ready_sequence"])

    # A replayed promotion is a no-op: the schedule is closed and the job left RETRY_WAIT.
    assert service.promote_retries(epoch) == 0
    assert _job(engine, job_id)["version"] == job["version"]

    decision = service.tick(epoch)
    assert isinstance(decision, Dispatch) and decision.job_id == str(job_id)
    with engine.connect() as connection:
        attempt = connection.execute(select(s.attempts)).mappings().one()
    assert attempt["job_fence"] == 3
    assert _job(engine, job_id)["retry_count"] == 1


def test_retry_waits_for_ready_at_and_live_leadership(migrated_postgres_engine):
    from nexa.coordinator.service import LeadershipLost

    engine = migrated_postgres_engine
    _, _, (job_id,) = seed_dispatchable(engine)
    _retry_wait(engine, job_id, ready_in=timedelta(seconds=60))
    service, epoch = _leader(engine)
    assert service.promote_retries(epoch) == 0
    assert _job(engine, job_id)["state"] == "RETRY_WAIT"

    with engine.begin() as connection:
        connection.execute(
            update(s.retry_schedules).values(ready_at=func.clock_timestamp() - timedelta(seconds=1))
        )
        connection.execute(
            update(s.coordinator_leadership).values(
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1)
            )
        )
    newer, newer_epoch = _leader(engine)
    with pytest.raises(LeadershipLost):
        service.promote_retries(epoch)
    assert _job(engine, job_id)["state"] == "RETRY_WAIT"
    assert _schedule(engine, job_id)["closed_at"] is None
    assert newer.promote_retries(newer_epoch) == 1
    assert _job(engine, job_id)["state"] == "QUEUED"


@pytest.mark.parametrize(
    "changes",
    [{"desired_state": "PAUSED"}, {"recovery_intent": "CHECKPOINT_FOR_PAUSE"}],
    ids=["desired-paused", "recovery-intent"],
)
def test_retry_promotion_requires_desired_running_without_recovery_intent(
    migrated_postgres_engine, changes
):
    engine = migrated_postgres_engine
    _, _, (job_id,) = seed_dispatchable(engine)
    _retry_wait(engine, job_id, ready_in=timedelta(seconds=-1), **changes)
    service, epoch = _leader(engine)
    before = _job(engine, job_id)
    assert service.promote_retries(epoch) == 0
    after = _job(engine, job_id)
    assert after["state"] == "RETRY_WAIT" and after["version"] == before["version"]
    assert _schedule(engine, job_id)["closed_at"] is None


def test_infeasible_retry_stays_waiting_with_a_visible_reason(migrated_postgres_engine):
    engine = migrated_postgres_engine
    _, _, (job_id,) = seed_dispatchable(engine)
    with engine.connect() as connection:
        original = connection.execute(select(s.worker_inventories)).mappings().one()

    def publish_inventory(version, *, compatible):
        capabilities = dict(original["workload_capabilities"])
        if not compatible:
            capabilities["images"] = []
        with engine.begin() as connection:
            connection.execute(
                s.worker_inventories.insert().values(
                    **{
                        key: original[key]
                        for key in (
                            "worker_id",
                            "worker_incarnation_id",
                            "architecture",
                            "host_cpu_millis",
                            "host_memory_bytes",
                            "allocatable_cpu_millis",
                            "allocatable_memory_bytes",
                            "runtime_capabilities",
                        )
                    },
                    inventory_id=new_uuid7(),
                    inventory_version=version,
                    allocatable_gpu_count=0,
                    workload_capabilities=capabilities,
                    checksum="sha256:" + f"{version:064x}",
                    observed_at=datetime.now(UTC),
                )
            )
            connection.execute(update(s.workers).values(current_inventory_version=version))

    publish_inventory(2, compatible=False)
    _retry_wait(engine, job_id, ready_in=timedelta(seconds=-1))
    service, epoch = _leader(engine)
    assert service.promote_retries(epoch) == 0
    blocked = _job(engine, job_id)
    assert blocked["state"] == "RETRY_WAIT"
    assert blocked["waiting_reason"] == "waiting_for_compatibility"
    assert _job_events(engine, job_id)[-1] == (
        "RETRY_BLOCKED",
        "waiting_for_compatibility",
        "COORDINATOR",
    )
    assert _schedule(engine, job_id)["closed_at"] is None
    # An unchanged block is not re-announced.
    assert service.promote_retries(epoch) == 0
    assert _job(engine, job_id)["version"] == blocked["version"]

    publish_inventory(3, compatible=True)
    assert service.promote_retries(epoch) == 1
    assert _job(engine, job_id)["state"] == "QUEUED"


def test_concurrent_promotion_and_ticks_promote_each_retry_once(migrated_postgres_engine):
    engine = migrated_postgres_engine
    _, _, job_ids = seed_dispatchable(engine, count=3)
    for job_id in job_ids:
        _retry_wait(engine, job_id, ready_in=timedelta(seconds=-1))
    service, epoch = _leader(engine)
    with ThreadPoolExecutor(max_workers=3) as pool:
        promoted = list(pool.map(lambda _: service.promote_retries(epoch), range(3)))
    assert sum(promoted) == 3
    for job_id in job_ids:
        assert [event[0] for event in _job_events(engine, job_id)].count("RETRY_READY") == 1
    with engine.connect() as connection:
        sequences = connection.execute(select(s.jobs.c.ready_sequence)).scalars().all()
    assert len(set(sequences)) == 3


def test_tick_promotes_due_retries_before_deciding(migrated_postgres_engine):
    from nexa.domain.scheduling import Dispatch

    engine = migrated_postgres_engine
    _, _, (job_id,) = seed_dispatchable(engine)
    _retry_wait(engine, job_id, ready_in=timedelta(seconds=-1))
    service, epoch = _leader(engine)
    decision = service.tick(epoch)
    assert isinstance(decision, Dispatch) and decision.job_id == str(job_id)
    assert [event[0] for event in _job_events(engine, job_id)][-2:] == [
        "RETRY_READY",
        "JOB_DISPATCHING",
    ]
