"""B13 queue and accounting behavior against the guarded PostgreSQL database."""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import event, func, literal_column, select, text, update
from sqlalchemy.exc import DBAPIError

from nexa.coordinator.accounting import account_locked
from nexa.coordinator.service import CoordinatorService
from nexa.coordinator.snapshot import read_snapshot
from nexa.domain.scheduling import CreateReservation, Dispatch, DrainForReservation, NoDecision
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration._factories import IMAGE_DIGEST, seed_job, seed_tenant_graph, seed_worker
from tests.integration.test_coordinator_b11 import seed_dispatchable
from tests.integration.test_jobs_b08 import _bootstrap_member, _seed_cpu_inputs, _submit_body
from tests.integration.test_worker_api_b10 import (
    WORKER_ID,
    _bootstrap_worker,
    _create_incarnation,
)

pytestmark = pytest.mark.postgres


def _snapshot_windows(engine, cursors=None):
    with create_session_factory(engine)() as session:
        if session.execute(select(s.fairness_state.c.singleton_key)).first() is None:
            session.execute(
                s.fairness_state.insert().values(singleton_key="local", virtual_floor=Decimal(0))
            )
        now = session.execute(select(func.clock_timestamp())).scalar_one()
        account_locked(session, now)
        policy = (
            session.execute(
                select(s.policy_versions).where(s.policy_versions.c.is_current.is_(True))
            )
            .mappings()
            .one()
        )
        worker = session.execute(select(s.workers)).mappings().one()
        inventory = session.execute(select(s.worker_inventories)).mappings().one()
        snapshot = read_snapshot(session, now, policy, worker, inventory, cursors or {})
        session.commit()
        return dict(snapshot.candidates_by_tenant)


@pytest.mark.parametrize("resume", [False, True])
def test_null_eligible_age_never_enters_oldest_or_resumed_window(migrated_postgres_engine, resume):
    graph, _, ids = seed_dispatchable(migrated_postgres_engine, count=2)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.jobs).where(s.jobs.c.job_id == ids[0]).values(eligible_since=None)
        )
        if resume:
            connection.execute(
                update(s.admission_counters)
                .where(
                    s.admission_counters.c.scope_type == "TENANT",
                    s.admission_counters.c.scope_id == str(graph["tenant_id"]),
                )
                .values(eligible_resumed_at=func.clock_timestamp())
            )
    window = _snapshot_windows(migrated_postgres_engine)[str(graph["tenant_id"])]
    assert [item.job_id for item in window.normal] == [str(ids[1])]
    assert window.oldest_eligible.job_id == str(ids[1])


def test_tenant_disable_enable_resets_eligible_age(migrated_postgres_engine, tmp_path):
    graph, _, ids = seed_dispatchable(migrated_postgres_engine)
    reservation_id = new_uuid7()
    with migrated_postgres_engine.begin() as connection:
        old_age = connection.execute(select(func.clock_timestamp())).scalar_one() - timedelta(
            seconds=180
        )
        connection.execute(
            update(s.jobs).where(s.jobs.c.job_id == ids[0]).values(eligible_since=old_age)
        )
        connection.execute(
            s.reservations.insert().values(
                reservation_id=reservation_id,
                tenant_id=graph["tenant_id"],
                job_id=ids[0],
                eligibility_since=old_age,
                policy_version=connection.execute(
                    select(s.policy_versions.c.policy_version).where(
                        s.policy_versions.c.is_current.is_(True)
                    )
                ).scalar_one(),
            )
        )
    with _client(migrated_postgres_engine, tmp_path) as client:
        _, csrf = _bootstrap_member(client)
        headers = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
        disabled = client.patch(
            f"/v1/admin/tenants/{graph['tenant_id']}",
            headers={**headers, "Idempotency-Key": "b13-disable-tenant", "If-Match": '"v1"'},
            json={"enabled": False},
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["enabled"] is False
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(s.reservations.c.invalidation_reason).where(
                        s.reservations.c.reservation_id == reservation_id
                    )
                ).scalar_one()
                == "tenant_disabled"
            )
            resumed_at = connection.execute(select(func.clock_timestamp())).scalar_one()
        enabled = client.patch(
            f"/v1/admin/tenants/{graph['tenant_id']}",
            headers={**headers, "Idempotency-Key": "b13-enable-tenant", "If-Match": '"v2"'},
            json={"enabled": True},
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["enabled"] is True
    _snapshot_windows(migrated_postgres_engine)
    window = _snapshot_windows(migrated_postgres_engine)[str(graph["tenant_id"])]
    assert window.oldest_eligible is not None
    assert window.oldest_eligible.eligible_wait_seconds < 5
    with migrated_postgres_engine.connect() as connection:
        recorded = connection.execute(
            select(s.admission_counters.c.eligible_resumed_at).where(
                s.admission_counters.c.scope_type == "TENANT",
                s.admission_counters.c.scope_id == str(graph["tenant_id"]),
            )
        ).scalar_one()
    assert recorded >= resumed_at


def test_pending_eligibility_replay_preserves_active_reservation(migrated_postgres_engine):
    graph, _, ids = seed_dispatchable(migrated_postgres_engine, count=0)
    with migrated_postgres_engine.begin() as connection:
        for index in range(513):
            ids.append(seed_job(connection, graph, cpu_millis=2000, ready_sequence=index)["job_id"])
        connection.execute(update(s.jobs).values(eligible_since=func.clock_timestamp()))
        reservation_id = new_uuid7()
        connection.execute(
            s.reservations.insert().values(
                reservation_id=reservation_id,
                tenant_id=graph["tenant_id"],
                job_id=ids[0],
                eligibility_since=connection.execute(select(func.clock_timestamp())).scalar_one(),
                policy_version=connection.execute(
                    select(s.policy_versions.c.policy_version).where(
                        s.policy_versions.c.is_current.is_(True)
                    )
                ).scalar_one(),
            )
        )
        connection.execute(update(s.tenant_policies).values(cpu_limit_millis=1000))
        connection.execute(update(s.tenant_policies).values(cpu_limit_millis=6000))

    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    decision = service.tick(service.acquire())
    assert isinstance(decision, NoDecision)
    with migrated_postgres_engine.connect() as connection:
        assert (
            connection.execute(
                select(s.reservations.c.invalidated_at).where(
                    s.reservations.c.reservation_id == reservation_id
                )
            ).scalar_one()
            is None
        )
        assert (
            connection.execute(
                select(func.count())
                .select_from(s.queue_eligibility_events)
                .where(s.queue_eligibility_events.c.completed_at.is_(None))
            ).scalar_one()
            > 0
        )

    with migrated_postgres_engine.begin() as connection:
        connection.execute(update(s.tenant_policies).values(cpu_limit_millis=1000))
    for _ in range(40):
        service.tick(service.acquire())
        with migrated_postgres_engine.connect() as connection:
            reason = connection.execute(
                select(s.reservations.c.invalidation_reason).where(
                    s.reservations.c.reservation_id == reservation_id
                )
            ).scalar_one()
        if reason is not None:
            break
    assert reason == "no_longer_eligible"


def test_api_submit_under_held_resource_quota_ages_from_quota_release(
    migrated_postgres_engine, tmp_path
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        admin_id, csrf = _bootstrap_member(client)
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
        tenant = client.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": "b13-age-tenant-0001"},
            json={"slug": "b13-age", "display_name": "B13 Age"},
        )
        assert tenant.status_code == 201, tenant.text
        tenant_id = tenant.json()["tenant_id"]
        membership = client.post(
            f"/v1/admin/tenants/{tenant_id}/memberships",
            headers={**write, "Idempotency-Key": "b13-age-member-0001", "If-Match": '"v1"'},
            json={"user_id": admin_id, "role": "MEMBER"},
        )
        assert membership.status_code == 200, membership.text
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={"username": "b08-admin@example.test", "password": "correct-horse-battery-staple"},
        )
        assert login.status_code == 200, login.text
        write["X-CSRF-Token"] = login.json()["csrf_token"]
        artifact_id = _seed_cpu_inputs(migrated_postgres_engine, tenant_id)
        with migrated_postgres_engine.begin() as connection:
            seed_worker(connection, label="b13-api-quota")
            connection.execute(
                update(s.worker_inventories).values(
                    architecture="linux/amd64",
                    allocatable_gpu_count=0,
                    workload_capabilities={
                        "adapters": [{"adapter_id": "cpu.iterative", "adapter_version": "1.0.0"}],
                        "images": [
                            {
                                "image_digest": IMAGE_DIGEST,
                                "architecture": "linux/amd64",
                                "verified": True,
                            }
                        ],
                        "frameworks": [
                            {
                                "framework": "NEXA_CPU",
                                "framework_version": "1.0.0",
                                "device": "CPU",
                            }
                        ],
                    },
                )
            )
            connection.execute(update(s.workers).values(last_heartbeat_at=func.clock_timestamp()))
        headers = {**write, "X-Nexa-Tenant-Id": tenant_id}
        first = client.post(
            "/v1/jobs",
            headers={**headers, "Idempotency-Key": "b13-age-first-0001"},
            json=_submit_body(artifact_id),
        )
        assert first.status_code == 202, first.text
        service = CoordinatorService(create_session_factory(migrated_postgres_engine))
        assert isinstance(service.tick(service.acquire()), Dispatch)
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                update(s.tenant_policies)
                .where(s.tenant_policies.c.tenant_id == UUID(tenant_id))
                .values(cpu_limit_millis=1000, tenant_active_limit=4, user_active_limit=4)
            )
        second = client.post(
            "/v1/jobs",
            headers={**headers, "Idempotency-Key": "b13-age-second-0001"},
            json=_submit_body(artifact_id),
        )
        assert second.status_code == 202, second.text
        second_id = second.json()["job_id"]
        blocked = _snapshot_windows(migrated_postgres_engine)[tenant_id]
        assert second_id not in {item.job_id for item in blocked.normal}
        assert blocked.oldest_eligible is None
        with migrated_postgres_engine.begin() as connection:
            _assert_queue_sizes_match(connection)
            released_at = connection.execute(select(func.clock_timestamp())).scalar_one()
            connection.execute(
                update(s.allocations).values(state="RELEASED", released_at=released_at)
            )
        time.sleep(1.2)
        window = _snapshot_windows(migrated_postgres_engine)[tenant_id]
        assert [item.job_id for item in window.normal] == [second_id]
        assert window.oldest_eligible.job_id == second_id
        assert window.oldest_eligible.eligible_wait_seconds <= 2
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(func.count()).select_from(s.queue_eligibility_events)
                ).scalar_one()
                == 0
            )


def _job_versions(connection):
    return dict(connection.execute(text("SELECT job_id, xmin::text FROM jobs")).all())


def _written_jobs(before, after):
    return {job_id for job_id, version in after.items() if before.get(job_id) != version}


def _assert_queue_sizes_match(connection):
    expected = connection.execute(
        text(
            "SELECT j.tenant_id, sp.cpu_millis, sp.memory_bytes, count(*) "
            "FROM jobs j JOIN job_specs sp ON sp.job_id = j.job_id "
            "WHERE j.state = 'QUEUED' AND j.desired_state = 'RUNNING' "
            "AND j.recovery_intent IS NULL AND sp.gpu_count = 0 GROUP BY 1, 2, 3"
        )
    ).all()
    recorded = connection.execute(
        select(
            s.queue_request_sizes.c.tenant_id,
            s.queue_request_sizes.c.cpu_millis,
            s.queue_request_sizes.c.memory_bytes,
            s.queue_request_sizes.c.queued_jobs,
        )
    ).all()
    assert sorted(map(tuple, recorded)) == sorted(map(tuple, expected))


def _quota_steps(connection, tenant_id, resource):
    return connection.execute(
        select(s.quota_headroom_steps.c.upper_bound, s.quota_headroom_steps.c.resumed_at)
        .where(
            s.quota_headroom_steps.c.tenant_id == tenant_id,
            s.quota_headroom_steps.c.resource == resource,
        )
        .order_by(s.quota_headroom_steps.c.upper_bound)
    ).all()


def test_default_quota_boundary_writes_bounded_job_rows_and_redispatches_after_release(
    migrated_postgres_engine,
):
    graph, _, ids = seed_dispatchable(migrated_postgres_engine, count=0)
    with migrated_postgres_engine.begin() as connection:
        for index in range(1000):
            ids.append(seed_job(connection, graph, cpu_millis=2000, ready_sequence=index)["job_id"])
        connection.execute(update(s.jobs).values(eligible_since=func.clock_timestamp()))
        connection.execute(update(s.admission_counters).values(outstanding=1000))
        # Half of the 6000m worker: one 2000m allocation leaves 1000m quota headroom.
        connection.execute(
            update(s.tenant_policies).values(
                cpu_limit_millis=3000, tenant_active_limit=4, user_active_limit=4
            )
        )
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    with migrated_postgres_engine.connect() as connection:
        before = _job_versions(connection)
    first = service.tick(epoch)
    assert isinstance(first, Dispatch)
    assert first.job_id == str(ids[0])
    with migrated_postgres_engine.connect() as connection:
        after_dispatch = _job_versions(connection)
    assert _written_jobs(before, after_dispatch) == {ids[0]}

    candidate_queries = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "FROM jobs JOIN job_specs" in statement:
            candidate_queries.append(statement)

    event.listen(migrated_postgres_engine, "before_cursor_execute", capture)
    try:
        assert isinstance(service.tick(epoch), NoDecision)
    finally:
        event.remove(migrated_postgres_engine, "before_cursor_execute", capture)
    assert candidate_queries == []
    with migrated_postgres_engine.begin() as connection:
        assert _written_jobs(after_dispatch, _job_versions(connection)) == set()
        connection.execute(
            update(s.allocations).values(state="RELEASED", released_at=func.clock_timestamp())
        )
    with migrated_postgres_engine.connect() as connection:
        after_release = _job_versions(connection)
        assert _written_jobs(after_dispatch, after_release) == set()
        assert _quota_steps(connection, graph["tenant_id"], "cpu")[-1][0] == 3000

    second = service.tick(epoch)
    assert isinstance(second, Dispatch)
    assert second.job_id == str(ids[1])
    with migrated_postgres_engine.connect() as connection:
        assert _written_jobs(after_release, _job_versions(connection)) == {ids[1]}
        assert (
            connection.execute(
                select(func.count()).select_from(s.queue_eligibility_events)
            ).scalar_one()
            == 0
        )
        _assert_queue_sizes_match(connection)


def test_quota_release_resets_age_only_for_sizes_it_unblocked(migrated_postgres_engine):
    graph, _, _ = seed_dispatchable(migrated_postgres_engine, count=0)
    with migrated_postgres_engine.begin() as connection:
        first = seed_job(connection, graph, cpu_millis=1000, ready_sequence=0)["job_id"]
        small = seed_job(connection, graph, cpu_millis=500, ready_sequence=1)["job_id"]
        large = seed_job(connection, graph, cpu_millis=1000, ready_sequence=2)["job_id"]
        now = connection.execute(select(func.clock_timestamp())).scalar_one()
        connection.execute(update(s.jobs).values(eligible_since=now - timedelta(seconds=100)))
        connection.execute(
            update(s.jobs)
            .where(s.jobs.c.job_id == first)
            .values(eligible_since=now - timedelta(seconds=110))
        )
        connection.execute(update(s.admission_counters).values(outstanding=3))
        connection.execute(
            update(s.tenant_policies).values(
                cpu_limit_millis=1500, tenant_active_limit=4, user_active_limit=4
            )
        )
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    # Ages stay below the 120 s reservation threshold so the head dispatches directly.
    dispatched = service.tick(service.acquire())
    assert isinstance(dispatched, Dispatch)
    assert dispatched.job_id == str(first)
    blocked = _snapshot_windows(migrated_postgres_engine)[str(graph["tenant_id"])]
    assert [item.job_id for item in blocked.normal] == [str(small)]

    with migrated_postgres_engine.begin() as connection:
        released_at = connection.execute(select(func.clock_timestamp())).scalar_one()
        connection.execute(update(s.allocations).values(state="RELEASED", released_at=released_at))
    window = _snapshot_windows(migrated_postgres_engine)[str(graph["tenant_id"])]
    waits = {item.job_id: item.eligible_wait_seconds for item in window.normal}
    assert waits[str(small)] >= 95
    assert waits[str(large)] <= 5
    assert window.oldest_eligible.job_id == str(small)
    with migrated_postgres_engine.connect() as connection:
        ages = dict(
            connection.execute(
                select(s.jobs.c.job_id, s.jobs.c.eligible_since).where(
                    s.jobs.c.job_id.in_([small, large])
                )
            ).all()
        )
        steps = _quota_steps(connection, graph["tenant_id"], "cpu")
    # The release is recorded once per tenant/resource, not on each queued job.
    assert ages[small] == ages[large] == now - timedelta(seconds=100)
    assert steps[0] == (500, None)
    assert steps[-1][0] == 1500 and steps[-1][1] >= released_at

    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            s.reservations.insert().values(
                reservation_id=new_uuid7(),
                tenant_id=graph["tenant_id"],
                job_id=large,
                eligibility_since=released_at,
                policy_version=connection.execute(
                    select(s.policy_versions.c.policy_version).where(
                        s.policy_versions.c.is_current.is_(True)
                    )
                ).scalar_one(),
            )
        )
    reserved = _snapshot_windows(migrated_postgres_engine)[str(graph["tenant_id"])]
    # The protected candidate keeps the quota resume boundary of its own size.
    assert reserved.oldest_eligible.job_id == str(large)
    assert reserved.oldest_eligible.eligible_wait_seconds <= 5


def test_concurrent_releases_leave_exact_quota_headroom(migrated_postgres_engine):
    graph, _, _ = seed_dispatchable(migrated_postgres_engine, count=2)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.tenant_policies).values(
                cpu_limit_millis=3000, tenant_active_limit=4, user_active_limit=4
            )
        )
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    assert isinstance(service.tick(epoch), Dispatch)
    assert isinstance(service.tick(epoch), Dispatch)
    with migrated_postgres_engine.connect() as connection:
        allocation_ids = connection.execute(select(s.allocations.c.allocation_id)).scalars().all()
        assert _quota_steps(connection, graph["tenant_id"], "cpu")[-1][0] == 1000
    barrier = Barrier(2)

    def release(allocation_id):
        with migrated_postgres_engine.begin() as connection:
            barrier.wait()
            connection.execute(
                update(s.allocations)
                .where(s.allocations.c.allocation_id == allocation_id)
                .values(state="RELEASED", released_at=func.clock_timestamp())
            )

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(release, allocation_ids))
    with migrated_postgres_engine.connect() as connection:
        steps = _quota_steps(connection, graph["tenant_id"], "cpu")
    assert [bound for bound, _ in steps] == sorted({bound for bound, _ in steps})
    assert steps[-1][0] == 3000
    assert steps[0] == (1000, None)


def test_queue_heads_follow_submit_priority_and_terminal_transition(migrated_postgres_engine):
    graph, _, ids = seed_dispatchable(migrated_postgres_engine, count=3)
    with migrated_postgres_engine.begin() as connection:
        for sequence, job_id in enumerate(ids):
            connection.execute(
                update(s.jobs).where(s.jobs.c.job_id == job_id).values(ready_sequence=sequence)
            )
        heads = {
            row["priority"]: row["candidate_job_id"]
            for row in connection.execute(
                select(s.queue_heads).where(s.queue_heads.c.tenant_id == graph["tenant_id"])
            ).mappings()
        }
        assert heads == {1: ids[0]}
        assert (
            connection.execute(select(s.queue_submitters.c.user_id)).scalar_one()
            == graph["user_id"]
        )

        connection.execute(update(s.jobs).where(s.jobs.c.job_id == ids[1]).values(base_priority=2))
        heads = {
            row["priority"]: row["candidate_job_id"]
            for row in connection.execute(
                select(s.queue_heads).where(s.queue_heads.c.tenant_id == graph["tenant_id"])
            ).mappings()
        }
        assert heads == {1: ids[0], 2: ids[1]}

        connection.execute(
            update(s.jobs).where(s.jobs.c.job_id == ids[0]).values(state="DISPATCHING")
        )
        head = connection.execute(
            select(s.queue_heads.c.candidate_job_id).where(
                s.queue_heads.c.tenant_id == graph["tenant_id"],
                s.queue_heads.c.priority == 1,
            )
        ).scalar_one()
        assert head == ids[2]
        connection.execute(
            update(s.jobs).where(s.jobs.c.job_id.in_(ids[1:])).values(state="FAILED")
        )
        assert connection.execute(select(s.queue_submitters)).all() == []


def test_later_submit_does_not_rewrite_unchanged_queue_head(migrated_postgres_engine):
    graph, _, ids = seed_dispatchable(migrated_postgres_engine, count=1)

    def head_version():
        with migrated_postgres_engine.connect() as connection:
            return connection.execute(
                select(s.queue_heads.c.candidate_job_id, literal_column("xmin::text")).where(
                    s.queue_heads.c.tenant_id == graph["tenant_id"]
                )
            ).one()

    before = head_version()
    with migrated_postgres_engine.begin() as connection:
        seed_job(connection, graph, ready_sequence=2**31)
    after = head_version()
    assert before[0] == after[0] == ids[0]
    assert before[1] == after[1]


def test_queue_head_tracks_eligibility_time_of_its_job(migrated_postgres_engine):
    graph, _, ids = seed_dispatchable(migrated_postgres_engine, count=1)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.jobs)
            .where(s.jobs.c.job_id == ids[0])
            .values(eligible_since=func.clock_timestamp() - timedelta(seconds=30))
        )
        head = connection.execute(
            select(s.queue_heads.c.eligible_since).where(
                s.queue_heads.c.tenant_id == graph["tenant_id"]
            )
        ).scalar_one()
        job_time = connection.execute(
            select(s.jobs.c.eligible_since).where(s.jobs.c.job_id == ids[0])
        ).scalar_one()
        assert head == job_time


def test_cursor_reaches_fitting_seventeenth_after_sixteen_no_fit(migrated_postgres_engine):
    graph, _, ids = seed_dispatchable(migrated_postgres_engine, count=1)
    with migrated_postgres_engine.begin() as connection:
        ids.extend(seed_job(connection, graph, cpu_millis=2000)["job_id"] for _ in range(16))
        ids.append(seed_job(connection, graph)["job_id"])
        connection.execute(update(s.worker_inventories).values(allocatable_cpu_millis=2000))
        connection.execute(
            update(s.tenant_policies).values(tenant_active_limit=2, user_active_limit=2)
        )
        connection.execute(update(s.admission_counters).values(outstanding=18))
        for sequence, job_id in enumerate(ids):
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == job_id)
                .values(ready_sequence=sequence, eligible_since=func.clock_timestamp())
            )

    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    first = service.tick(epoch)
    assert isinstance(first, Dispatch)
    assert first.job_id == str(ids[0])
    assert isinstance(service.tick(epoch), NoDecision)
    with migrated_postgres_engine.connect() as connection:
        pending = connection.execute(
            select(func.count())
            .select_from(s.queue_eligibility_events)
            .where(s.queue_eligibility_events.c.completed_at.is_(None))
        ).scalar_one()
        small_age = connection.execute(
            select(s.jobs.c.eligible_since).where(s.jobs.c.job_id == ids[17])
        ).scalar_one()
    assert pending == 0
    assert small_age is not None
    selected = service.tick(epoch)
    assert isinstance(selected, Dispatch)
    assert selected.job_id == str(ids[17])


def test_aged_low_priority_job_outside_first_sixteen_is_promoted(migrated_postgres_engine):
    _, _, ids = seed_dispatchable(migrated_postgres_engine, count=18)
    with migrated_postgres_engine.begin() as connection:
        for sequence, job_id in enumerate(ids):
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == job_id)
                .values(
                    ready_sequence=sequence,
                    base_priority=0,
                    eligible_since=func.clock_timestamp()
                    - timedelta(seconds=61 if sequence == 17 else 0),
                )
            )
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    selected = service.tick(service.acquire())
    assert isinstance(selected, Dispatch)
    assert selected.job_id == str(ids[17])


def test_retry_wait_does_not_age_before_ready_time(migrated_postgres_engine):
    _, _, ids = seed_dispatchable(migrated_postgres_engine, count=2)
    with migrated_postgres_engine.begin() as connection:
        now = connection.execute(select(func.clock_timestamp())).scalar_one()
        connection.execute(
            update(s.jobs)
            .where(s.jobs.c.job_id == ids[0])
            .values(
                base_priority=0,
                ready_sequence=0,
                eligible_since=now - timedelta(seconds=180),
                retry_ready_at=now - timedelta(seconds=1),
            )
        )
        connection.execute(
            update(s.jobs)
            .where(s.jobs.c.job_id == ids[1])
            .values(base_priority=1, ready_sequence=1, eligible_since=now)
        )

    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    selected = service.tick(service.acquire())
    assert isinstance(selected, Dispatch)
    assert selected.job_id == str(ids[1])


def test_unchanged_queue_does_not_rewrite_job_eligibility_each_tick(migrated_postgres_engine):
    seed_dispatchable(migrated_postgres_engine)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(update(s.tenant_policies).values(cpu_limit_millis=500))
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    assert isinstance(service.tick(epoch), NoDecision)

    updates = []
    candidate_queries = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("UPDATE jobs"):
            updates.append(statement)
        if "FROM jobs JOIN job_specs" in statement:
            candidate_queries.append(statement)

    event.listen(migrated_postgres_engine, "before_cursor_execute", capture)
    try:
        assert isinstance(service.tick(epoch), NoDecision)
    finally:
        event.remove(migrated_postgres_engine, "before_cursor_execute", capture)
    assert updates == []
    assert len(candidate_queries) <= 1


def test_dispatch_with_ample_quota_does_not_reconcile_whole_queue(
    migrated_postgres_engine,
):
    seed_dispatchable(migrated_postgres_engine, count=2)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.tenant_policies).values(
                cpu_limit_millis=10000,
                memory_limit_bytes=32 * 1024**3,
            )
        )
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    assert isinstance(service.tick(epoch), Dispatch)

    updates = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("UPDATE jobs"):
            updates.append(statement)

    event.listen(migrated_postgres_engine, "before_cursor_execute", capture)
    try:
        assert isinstance(service.tick(epoch), NoDecision)
    finally:
        event.remove(migrated_postgres_engine, "before_cursor_execute", capture)
    assert updates == []


def test_coordinator_queue_reads_run_without_jit(migrated_postgres_engine):
    # At 100 tenants the batched queue read is estimated near 800k, above
    # jit_optimize_above_cost; compiling it took ~1.1 s of a 75 ms statement.
    seed_dispatchable(migrated_postgres_engine, count=2)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(update(s.tenant_policies).values(cpu_limit_millis=10000))
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    settings = []

    def capture(_connection, cursor, statement, _parameters, _context, _executemany):
        if "FROM jobs" in statement:
            settings.append(
                cursor.connection.execute("SELECT current_setting('jit')").fetchone()[0]
            )

    event.listen(migrated_postgres_engine, "after_cursor_execute", capture)
    try:
        assert isinstance(service.tick(epoch), Dispatch)
    finally:
        event.remove(migrated_postgres_engine, "after_cursor_execute", capture)
    assert settings
    assert set(settings) == {"off"}


def test_accounting_uses_bounded_statement_count_for_multiple_tenants(
    migrated_postgres_engine,
):
    from nexa.coordinator.accounting import account_locked
    from nexa.infrastructure.persistence.locking import clock_timestamp

    seed_dispatchable(migrated_postgres_engine, count=0)
    with migrated_postgres_engine.begin() as connection:
        for index in range(3):
            graph = seed_tenant_graph(connection, label=f"b13-account-{index}")
            connection.execute(
                s.tenant_policies.insert().values(
                    tenant_id=graph["tenant_id"],
                    version=1,
                    weight=Decimal(1),
                    cpu_limit_millis=6000,
                    memory_limit_bytes=12 * 1024**3,
                    gpu_limit=0,
                    outstanding_limit=2000,
                    user_outstanding_limit=2000,
                    tenant_active_limit=2,
                    user_active_limit=1,
                    tenant_rate_per_second=Decimal(5),
                    tenant_rate_burst=Decimal(20),
                    user_rate_per_second=Decimal(2),
                    user_rate_burst=Decimal(10),
                    is_current=True,
                )
            )
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith(("INSERT INTO fairness_ledgers", "UPDATE fairness_ledgers")):
            statements.append(statement)

    event.listen(migrated_postgres_engine, "before_cursor_execute", capture)
    try:
        with create_session_factory(migrated_postgres_engine).begin() as session:
            account_locked(session, clock_timestamp(session))
    finally:
        event.remove(migrated_postgres_engine, "before_cursor_execute", capture)
    assert len(statements) <= 2


@pytest.mark.parametrize("resume", [False, True])
def test_candidate_lookup_batches_tenants_with_and_without_resume(migrated_postgres_engine, resume):
    first, _, _ = seed_dispatchable(migrated_postgres_engine, count=1)
    with migrated_postgres_engine.begin() as connection:
        requirements = connection.execute(
            select(s.template_versions.c.capability_requirements).where(
                s.template_versions.c.template_id == first["template_id"]
            )
        ).scalar_one()
        for index in range(3):
            graph = seed_tenant_graph(connection, label=f"b13-batch-{index}")
            connection.execute(
                update(s.template_versions)
                .where(s.template_versions.c.template_id == graph["template_id"])
                .values(capability_requirements=requirements)
            )
            connection.execute(
                s.tenant_policies.insert().values(
                    tenant_id=graph["tenant_id"],
                    version=1,
                    weight=Decimal(1),
                    cpu_limit_millis=6000,
                    memory_limit_bytes=12 * 1024**3,
                    gpu_limit=0,
                    outstanding_limit=100,
                    user_outstanding_limit=100,
                    tenant_active_limit=1,
                    user_active_limit=1,
                    tenant_rate_per_second=Decimal(5),
                    tenant_rate_burst=Decimal(20),
                    user_rate_per_second=Decimal(2),
                    user_rate_burst=Decimal(10),
                    is_current=True,
                )
            )
            seed_job(connection, graph, ready_sequence=index + 1)
            for scope, scope_id in (
                ("TENANT", str(graph["tenant_id"])),
                ("USER", f"{graph['tenant_id']}:{graph['user_id']}"),
            ):
                connection.execute(
                    s.admission_counters.insert().values(
                        scope_type=scope, scope_id=scope_id, outstanding=1
                    )
                )
        connection.execute(
            update(s.admission_counters)
            .where(s.admission_counters.c.scope_type == "GLOBAL")
            .values(outstanding=4)
        )
        connection.execute(update(s.jobs).values(eligible_since=func.clock_timestamp()))
        if resume:
            connection.execute(
                update(s.admission_counters)
                .where(s.admission_counters.c.scope_type.in_(("TENANT", "USER")))
                .values(eligible_resumed_at=func.clock_timestamp())
            )

    candidate_queries = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("SELECT") and "FROM jobs JOIN job_specs" in statement:
            candidate_queries.append(statement)

    event.listen(migrated_postgres_engine, "before_cursor_execute", capture)
    try:
        service = CoordinatorService(create_session_factory(migrated_postgres_engine))
        assert isinstance(service.tick(service.acquire()), Dispatch)
    finally:
        event.remove(migrated_postgres_engine, "before_cursor_execute", capture)
    assert len(candidate_queries) <= 2


def test_user_concurrency_block_skips_candidate_queue_scan(migrated_postgres_engine):
    graph, _, _ = seed_dispatchable(migrated_postgres_engine)
    with migrated_postgres_engine.begin() as connection:
        for _ in range(99):
            seed_job(connection, graph)
        connection.execute(
            update(s.tenant_policies).values(tenant_active_limit=2, user_active_limit=1)
        )
        connection.execute(
            update(s.admission_counters)
            .where(s.admission_counters.c.scope_type == "TENANT")
            .values(active_attempts=1, outstanding=100)
        )
        connection.execute(
            update(s.admission_counters)
            .where(s.admission_counters.c.scope_type == "USER")
            .values(active_attempts=1, outstanding=100)
        )
    queries = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "FROM jobs JOIN job_specs" in statement:
            queries.append(statement)

    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    assert isinstance(service.tick(epoch), NoDecision)
    event.listen(migrated_postgres_engine, "before_cursor_execute", capture)
    try:
        assert isinstance(service.tick(epoch), NoDecision)
    finally:
        event.remove(migrated_postgres_engine, "before_cursor_execute", capture)
    assert queries == []


def test_user_concurrency_block_does_not_rewrite_queued_jobs(migrated_postgres_engine):
    graph, _, _ = seed_dispatchable(migrated_postgres_engine, count=0)
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    assert isinstance(service.tick(epoch), NoDecision)
    with migrated_postgres_engine.begin() as connection:
        ids = [seed_job(connection, graph)["job_id"]]
        for _ in range(99):
            seed_job(connection, graph)
        connection.execute(update(s.jobs).values(eligible_since=func.clock_timestamp()))
        connection.execute(update(s.tenant_policies).values(user_active_limit=1))
        connection.execute(
            update(s.admission_counters)
            .where(s.admission_counters.c.scope_type == "USER")
            .values(active_attempts=1, outstanding=100)
        )
        connection.execute(
            update(s.admission_counters)
            .where(s.admission_counters.c.scope_type == "TENANT")
            .values(active_attempts=1, outstanding=100)
        )

    queue_updates = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("UPDATE jobs"):
            queue_updates.append(statement)

    event.listen(migrated_postgres_engine, "before_cursor_execute", capture)
    try:
        assert isinstance(service.tick(epoch), NoDecision)
    finally:
        event.remove(migrated_postgres_engine, "before_cursor_execute", capture)
    assert queue_updates == []
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(s.jobs)).scalar_one() == 100
        assert (
            connection.execute(
                select(s.jobs.c.eligible_since).where(s.jobs.c.job_id == ids[0])
            ).scalar_one()
            is not None
        )


@pytest.mark.parametrize("scope_type", ["USER", "TENANT"])
def test_block_and_resume_between_ticks_resets_reservation_age(
    migrated_postgres_engine, scope_type
):
    graph, _, ids = seed_dispatchable(migrated_postgres_engine)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.jobs)
            .where(s.jobs.c.job_id == ids[0])
            .values(eligible_since=func.clock_timestamp() - timedelta(seconds=180))
        )
        connection.execute(
            update(s.tenant_policies).values(user_active_limit=1, tenant_active_limit=1)
        )
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.admission_counters)
            .where(s.admission_counters.c.scope_type == scope_type)
            .values(active_attempts=1)
        )
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.admission_counters)
            .where(s.admission_counters.c.scope_type == scope_type)
            .values(active_attempts=0)
        )

    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    decision = service.tick(service.acquire())
    assert isinstance(decision, Dispatch)
    assert decision.job_id == str(ids[0])


def test_resource_block_and_resume_between_ticks_resets_only_affected_age(
    migrated_postgres_engine,
):
    graph, _, ids = seed_dispatchable(migrated_postgres_engine)
    with migrated_postgres_engine.begin() as connection:
        large_id = seed_job(connection, graph, cpu_millis=2000)["job_id"]
        old_age = func.clock_timestamp() - timedelta(seconds=180)
        connection.execute(update(s.jobs).values(eligible_since=old_age))
        connection.execute(update(s.admission_counters).values(outstanding=2, active_attempts=1))

    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    assert isinstance(service.tick(epoch), NoDecision)

    with migrated_postgres_engine.begin() as connection:
        connection.execute(update(s.tenant_policies).values(cpu_limit_millis=1000))
        resumed_at = connection.execute(select(func.clock_timestamp())).scalar_one()
        connection.execute(update(s.tenant_policies).values(cpu_limit_millis=6000))
        connection.execute(update(s.admission_counters).values(active_attempts=0))

    assert isinstance(service.tick(epoch), NoDecision)
    assert isinstance(service.tick(epoch), Dispatch)
    with migrated_postgres_engine.connect() as connection:
        ages = dict(
            connection.execute(
                select(s.jobs.c.job_id, s.jobs.c.eligible_since).where(
                    s.jobs.c.job_id.in_([ids[0], large_id])
                )
            ).all()
        )
    assert ages[ids[0]] < resumed_at - timedelta(seconds=120)
    assert ages[large_id] >= resumed_at


def test_capability_block_and_resume_between_ticks_resets_age(migrated_postgres_engine):
    graph, _, ids = seed_dispatchable(migrated_postgres_engine)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.jobs)
            .where(s.jobs.c.job_id == ids[0])
            .values(eligible_since=func.clock_timestamp() - timedelta(seconds=180))
        )
        connection.execute(update(s.admission_counters).values(active_attempts=1))
        original = connection.execute(
            select(s.worker_inventories.c.workload_capabilities)
        ).scalar_one()
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    assert isinstance(service.tick(epoch), NoDecision)

    with migrated_postgres_engine.begin() as connection:
        connection.execute(update(s.worker_inventories).values(workload_capabilities={}))
        resumed_at = connection.execute(select(func.clock_timestamp())).scalar_one()
        connection.execute(update(s.worker_inventories).values(workload_capabilities=original))
        connection.execute(update(s.admission_counters).values(active_attempts=0))

    assert isinstance(service.tick(epoch), NoDecision)
    assert isinstance(service.tick(epoch), Dispatch)
    with migrated_postgres_engine.connect() as connection:
        age = connection.execute(
            select(s.jobs.c.eligible_since).where(s.jobs.c.job_id == ids[0])
        ).scalar_one()
    assert age >= resumed_at


def test_eligibility_backlog_is_drained_before_dispatch(migrated_postgres_engine):
    graph, _, ids = seed_dispatchable(migrated_postgres_engine, count=0)
    with migrated_postgres_engine.begin() as connection:
        for index in range(1001):
            ids.append(seed_job(connection, graph, cpu_millis=2000, ready_sequence=index)["job_id"])
        connection.execute(update(s.jobs).values(eligible_since=func.clock_timestamp()))
        connection.execute(update(s.admission_counters).values(outstanding=1001))
        connection.execute(update(s.tenant_policies).values(cpu_limit_millis=1000))
        connection.execute(update(s.tenant_policies).values(cpu_limit_millis=6000))

    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    selected = None
    for _ in range(40):
        selected = service.tick(epoch)
        with migrated_postgres_engine.connect() as connection:
            pending = connection.execute(
                select(func.count())
                .select_from(s.queue_eligibility_events)
                .where(s.queue_eligibility_events.c.completed_at.is_(None))
            ).scalar_one()
            allocated = connection.execute(
                select(func.count()).select_from(s.allocations)
            ).scalar_one()
        if pending:
            assert isinstance(selected, NoDecision)
            assert allocated == 0
        else:
            break
    assert pending == 0
    assert isinstance(selected, Dispatch)
    assert selected.job_id == str(ids[0])
    with migrated_postgres_engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(s.queue_eligibility_events)
                .where(s.queue_eligibility_events.c.completed_at.is_(None))
            ).scalar_one()
            == 0
        )


def test_reservation_drains_fitting_arrivals_until_verified_cleanup_releases_capacity(
    migrated_postgres_engine, tmp_path
):
    timeline = []
    realtime = os.environ.get("B13_REALTIME_STARVATION") == "1"

    def capture(label):
        with migrated_postgres_engine.connect() as connection:
            allocations = (
                connection.execute(
                    select(
                        s.allocations.c.job_id,
                        s.allocations.c.cpu_millis,
                        s.allocations.c.state,
                    )
                )
                .mappings()
                .all()
            )
            reservations = (
                connection.execute(
                    select(s.reservations.c.job_id, s.reservations.c.invalidation_reason)
                )
                .mappings()
                .all()
            )
            jobs = (
                connection.execute(
                    select(s.jobs.c.job_id, s.jobs.c.state, s.jobs.c.eligible_since).order_by(
                        s.jobs.c.ready_sequence
                    )
                )
                .mappings()
                .all()
            )
            capacity = connection.execute(
                select(s.worker_inventories.c.allocatable_cpu_millis)
            ).scalar_one()
            timeline.append(
                {
                    "step": label,
                    "db_time": connection.execute(select(func.clock_timestamp())).scalar_one(),
                    "free_cpu_millis": capacity
                    - sum(row["cpu_millis"] for row in allocations if row["state"] != "RELEASED"),
                    "jobs": [dict(row) for row in jobs],
                    "allocations": [dict(row) for row in allocations],
                    "reservations": [dict(row) for row in reservations],
                }
            )

    with _client(migrated_postgres_engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b13-reservation-incarnation"
        )
        large_tenant, _, _ = seed_dispatchable(
            migrated_postgres_engine,
            count=0,
            existing_worker=(UUID(WORKER_ID), UUID(incarnation["worker_incarnation_id"])),
        )
        with migrated_postgres_engine.begin() as connection:
            large_id = seed_job(connection, large_tenant, cpu_millis=4000, ready_sequence=1)[
                "job_id"
            ]
            small_tenant = seed_tenant_graph(connection, label="b13-reservation-small")
            requirements = connection.execute(
                select(s.template_versions.c.capability_requirements).where(
                    s.template_versions.c.template_id == large_tenant["template_id"]
                )
            ).scalar_one()
            connection.execute(
                update(s.template_versions)
                .where(s.template_versions.c.template_id == small_tenant["template_id"])
                .values(capability_requirements=requirements)
            )
            connection.execute(
                s.tenant_policies.insert().values(
                    tenant_id=small_tenant["tenant_id"],
                    version=1,
                    weight=Decimal(1),
                    cpu_limit_millis=6000,
                    memory_limit_bytes=12 * 1024**3,
                    gpu_limit=0,
                    outstanding_limit=100,
                    user_outstanding_limit=100,
                    tenant_active_limit=2,
                    user_active_limit=2,
                    tenant_rate_per_second=Decimal(5),
                    tenant_rate_burst=Decimal(20),
                    user_rate_per_second=Decimal(2),
                    user_rate_burst=Decimal(10),
                    is_current=True,
                )
            )
            initial_id = seed_job(connection, small_tenant, cpu_millis=3000, ready_sequence=0)[
                "job_id"
            ]
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == initial_id)
                .values(eligible_since=func.clock_timestamp())
            )
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == large_id)
                .values(
                    eligible_since=(
                        func.clock_timestamp()
                        if realtime
                        else func.clock_timestamp() - timedelta(seconds=121)
                    ),
                )
            )
            connection.execute(
                update(s.tenant_policies)
                .where(s.tenant_policies.c.tenant_id == large_tenant["tenant_id"])
                .values(cpu_limit_millis=6000)
            )
            connection.execute(
                update(s.admission_counters)
                .where(
                    (s.admission_counters.c.scope_id == str(large_tenant["tenant_id"]))
                    | (
                        s.admission_counters.c.scope_id
                        == f"{large_tenant['tenant_id']}:{large_tenant['user_id']}"
                    )
                )
                .values(outstanding=1)
            )
            for scope, scope_id in (
                ("TENANT", str(small_tenant["tenant_id"])),
                ("USER", f"{small_tenant['tenant_id']}:{small_tenant['user_id']}"),
            ):
                connection.execute(
                    s.admission_counters.insert().values(
                        scope_type=scope, scope_id=scope_id, outstanding=1
                    )
                )
            connection.execute(
                update(s.admission_counters)
                .where(s.admission_counters.c.scope_type == "GLOBAL")
                .values(outstanding=2)
            )

        service = CoordinatorService(create_session_factory(migrated_postgres_engine))
        epoch = service.acquire()
        first = service.tick(epoch)
        assert isinstance(first, Dispatch)
        assert first.job_id == str(initial_id)
        capture("initial_held_dispatch")
        if realtime:
            from benchmarks.b13.runtime_fairness import complete_fixture_allocation

            wait_started = time.monotonic()
            for index in range(24):
                time.sleep(max(0, wait_started + index * 5 - time.monotonic()))
                with migrated_postgres_engine.begin() as connection:
                    connection.execute(
                        update(s.workers).values(last_heartbeat_at=func.clock_timestamp())
                    )
                    connection.execute(
                        update(s.attempt_leases)
                        .where(s.attempt_leases.c.job_id == initial_id)
                        .values(expires_at=func.clock_timestamp() + timedelta(seconds=45))
                    )
                    arrival_id = seed_job(
                        connection, small_tenant, cpu_millis=1000, ready_sequence=100 + index
                    )["job_id"]
                    connection.execute(
                        update(s.jobs)
                        .where(s.jobs.c.job_id == arrival_id)
                        .values(eligible_since=func.clock_timestamp())
                    )
                    connection.execute(
                        update(s.admission_counters)
                        .where(
                            (s.admission_counters.c.scope_type == "GLOBAL")
                            | (s.admission_counters.c.scope_id == str(small_tenant["tenant_id"]))
                            | (
                                s.admission_counters.c.scope_id
                                == f"{small_tenant['tenant_id']}:{small_tenant['user_id']}"
                            )
                        )
                        .values(outstanding=s.admission_counters.c.outstanding + 1)
                    )
                assert service.renew(epoch)
                arrival = service.tick(epoch)
                assert isinstance(arrival, Dispatch)
                assert arrival.job_id == str(arrival_id)
                with migrated_postgres_engine.connect() as connection:
                    allocation_id = connection.execute(
                        select(s.allocations.c.allocation_id).where(
                            s.allocations.c.job_id == arrival_id
                        )
                    ).scalar_one()
                    arrived_at = connection.execute(select(func.clock_timestamp())).scalar_one()
                complete_fixture_allocation(migrated_postgres_engine, service, allocation_id)
                timeline.append(
                    {
                        "step": "small_arrival_before_reservation",
                        "db_time": arrived_at,
                        "job_id": arrival_id,
                        "decision": type(arrival).__name__,
                    }
                )
            time.sleep(max(0, wait_started + 121 - time.monotonic()))
            with migrated_postgres_engine.begin() as connection:
                connection.execute(
                    update(s.workers).values(last_heartbeat_at=func.clock_timestamp())
                )
                connection.execute(
                    update(s.attempt_leases)
                    .where(s.attempt_leases.c.job_id == initial_id)
                    .values(expires_at=func.clock_timestamp() + timedelta(seconds=45))
                )
            assert service.renew(epoch)
        reserved = service.tick(epoch)
        assert isinstance(reserved, CreateReservation)
        assert reserved.job_id == str(large_id)
        capture("large_job_reserved")

        with migrated_postgres_engine.begin() as connection:
            small_before = seed_job(connection, small_tenant, cpu_millis=1000, ready_sequence=2)[
                "job_id"
            ]
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == small_before)
                .values(eligible_since=func.clock_timestamp())
            )
            connection.execute(
                update(s.admission_counters)
                .where(
                    (s.admission_counters.c.scope_type == "GLOBAL")
                    | (s.admission_counters.c.scope_id.like(f"{small_tenant['tenant_id']}:%"))
                    | (s.admission_counters.c.scope_id == str(small_tenant["tenant_id"]))
                )
                .values(outstanding=s.admission_counters.c.outstanding + 1)
            )
            held = (
                connection.execute(
                    select(s.allocations).where(s.allocations.c.job_id == initial_id)
                )
                .mappings()
                .one()
            )
            attempt = (
                connection.execute(select(s.attempts).where(s.attempts.c.job_id == initial_id))
                .mappings()
                .one()
            )
            lease = (
                connection.execute(
                    select(s.attempt_leases).where(
                        s.attempt_leases.c.attempt_id == attempt["attempt_id"]
                    )
                )
                .mappings()
                .one()
            )
            inventory = connection.execute(select(s.worker_inventories)).mappings().one()
            assert inventory["allocatable_cpu_millis"] - held["cpu_millis"] >= 1000

        drained = service.tick(epoch)
        assert isinstance(drained, DrainForReservation)
        assert drained.job_id == str(large_id)
        capture("fitting_small_arrival_drained")
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(s.jobs.c.state).where(s.jobs.c.job_id == small_before)
                ).scalar_one()
                == "QUEUED"
            )

        authority = {
            "worker_id": str(held["worker_id"]),
            "worker_incarnation_id": str(attempt["worker_incarnation_id"]),
            "attempt_id": str(attempt["attempt_id"]),
            "allocation_id": str(held["allocation_id"]),
            "lease_id": str(lease["lease_id"]),
            "job_fence": attempt["job_fence"],
        }
        proof = {
            "proof_type": "NO_CONTAINER",
            "startup_nonce": str(attempt["startup_nonce"]),
            "executor_operation_sequence": 1,
            "tombstone_sequence": 1,
            "observed_at": datetime.now(UTC).isoformat(),
            "inspection_checksum": "sha256:" + "a" * 64,
        }
        path = f"/v1/attempts/{attempt['attempt_id']}"
        failed = client.post(
            path + "/fail",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Callback-Id": str(new_uuid7()),
            },
            json={
                "authority": authority,
                "failure_class": "INTERNAL",
                "reason_code": "WORKLOAD_EXIT_NONZERO",
                "observation": {"observation_type": "NO_CONTAINER", "proof": proof},
            },
        )
        assert failed.status_code == 200, failed.text
        cleanup_headers = {
            "Authorization": f"Bearer {credential}",
            "X-Callback-Id": str(new_uuid7()),
        }
        cleanup_body = {
            **{key: value for key, value in authority.items() if key != "lease_id"},
            "proof": proof,
        }
        raced_decision = None
        if os.environ.get("B13_CONCURRENT_CLEANUP") == "1":
            barrier = Barrier(2)

            def cleanup_race():
                barrier.wait(timeout=5)
                return client.post(path + "/cleanup", headers=cleanup_headers, json=cleanup_body)

            def coordinator_race():
                barrier.wait(timeout=5)
                delay_ms = float(os.environ.get("B13_RACE_TICK_DELAY_MS", "0"))
                if delay_ms:
                    time.sleep(delay_ms / 1000)
                return service.tick(epoch)

            with ThreadPoolExecutor(max_workers=2) as pool:
                cleanup_future = pool.submit(cleanup_race)
                tick_future = pool.submit(coordinator_race)
                cleanup = cleanup_future.result(timeout=10)
                raced_decision = tick_future.result(timeout=10)
            assert isinstance(raced_decision, (Dispatch, DrainForReservation, NoDecision))
            timeline.append(
                {"step": "concurrent_cleanup_tick", "decision": type(raced_decision).__name__}
            )
        else:
            cleanup = client.post(path + "/cleanup", headers=cleanup_headers, json=cleanup_body)
        assert cleanup.status_code == 200, cleanup.text
        assert cleanup.json()["allocation_state"] == "RELEASED"
        capture("verified_cleanup_release")
        protected = raced_decision if isinstance(raced_decision, Dispatch) else service.tick(epoch)
        assert isinstance(protected, Dispatch)
        assert protected.job_id == str(large_id)
        capture("protected_large_dispatch")
        with migrated_postgres_engine.begin() as connection:
            small_after = seed_job(connection, small_tenant, cpu_millis=1000, ready_sequence=3)[
                "job_id"
            ]
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == small_after)
                .values(eligible_since=func.clock_timestamp())
            )
            ended = connection.execute(
                select(s.reservations.c.invalidation_reason).where(
                    s.reservations.c.job_id == large_id
                )
            ).scalar_one()
            assert ended == "dispatched"
        capture("small_arrival_after_protected_dispatch")
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(s.jobs.c.state).where(s.jobs.c.job_id == small_after)
                ).scalar_one()
                == "QUEUED"
            )
        for expected_id, label in (
            (small_before, "small_before_dispatched_after_protection"),
            (small_after, "small_after_dispatched_after_protection"),
        ):
            resumed = service.tick(epoch)
            assert isinstance(resumed, Dispatch)
            assert resumed.job_id == str(expected_id)
            capture(label)
        with migrated_postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    select(func.count()).select_from(s.jobs).where(s.jobs.c.state == "QUEUED")
                ).scalar_one()
                == 0
            )
            held_cpu = connection.execute(
                select(func.coalesce(func.sum(s.allocations.c.cpu_millis), 0)).where(
                    s.allocations.c.state != "RELEASED"
                )
            ).scalar_one()
            active = dict(
                connection.execute(
                    select(
                        s.admission_counters.c.scope_type,
                        func.sum(s.admission_counters.c.active_attempts),
                    ).group_by(s.admission_counters.c.scope_type)
                ).all()
            )
            large_allocations = connection.execute(
                select(func.count())
                .select_from(s.allocations)
                .where(s.allocations.c.job_id == large_id)
            ).scalar_one()
        assert held_cpu == 6000
        assert active == {"GLOBAL": 3, "TENANT": 3, "USER": 3}
        assert large_allocations == 1

    if output := os.environ.get("B13_RESERVATION_TIMELINE"):
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(timeline, default=str, indent=2) + "\n")
    if realtime:
        reserved_at = next(
            item["db_time"] for item in timeline if item["step"] == "large_job_reserved"
        )
        assert (reserved_at - timeline[0]["db_time"]).total_seconds() >= 120
        assert sum(item["step"] == "small_arrival_before_reservation" for item in timeline) == 24


def test_blocked_submitter_does_not_hide_another_users_job(migrated_postgres_engine):
    graph, _, ids = seed_dispatchable(migrated_postgres_engine)
    other_user = new_uuid7()
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            s.users.insert().values(
                user_id=other_user,
                username="b13-other-user",
                display_name="Other user",
                password_hash="$argon2id$" + "x" * 40,
                enabled=True,
                version=1,
            )
        )
        connection.execute(
            s.memberships.insert().values(
                tenant_id=graph["tenant_id"], user_id=other_user, role="MEMBER"
            )
        )
        second = seed_job(connection, {**graph, "user_id": other_user}, ready_sequence=1)["job_id"]
        connection.execute(update(s.jobs).where(s.jobs.c.job_id == ids[0]).values(ready_sequence=0))
        connection.execute(update(s.jobs).values(eligible_since=func.clock_timestamp()))
        connection.execute(
            update(s.tenant_policies).values(tenant_active_limit=2, user_active_limit=1)
        )
        connection.execute(
            update(s.admission_counters)
            .where(s.admission_counters.c.scope_type == "TENANT")
            .values(active_attempts=1, outstanding=2)
        )
        connection.execute(
            update(s.admission_counters)
            .where(s.admission_counters.c.scope_type == "USER")
            .values(active_attempts=1, outstanding=1)
        )
        connection.execute(
            s.admission_counters.insert().values(
                scope_type="USER",
                scope_id=f"{graph['tenant_id']}:{other_user}",
                outstanding=1,
            )
        )
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    selected = service.tick(service.acquire())
    assert isinstance(selected, Dispatch)
    assert selected.job_id == str(second)


def test_multiple_submitter_resume_preserves_age_order_and_cursor(migrated_postgres_engine):
    graph, _, _ = seed_dispatchable(migrated_postgres_engine, count=0)
    other_user = new_uuid7()
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            s.users.insert().values(
                user_id=other_user,
                username="b13-resumed-submitter",
                display_name="Resumed submitter",
                password_hash="$argon2id$" + "x" * 40,
                enabled=True,
                version=1,
            )
        )
        connection.execute(
            s.memberships.insert().values(
                tenant_id=graph["tenant_id"], user_id=other_user, role="MEMBER"
            )
        )
        first = [seed_job(connection, graph, ready_sequence=index)["job_id"] for index in range(20)]
        second = [
            seed_job(connection, {**graph, "user_id": other_user}, ready_sequence=index + 20)[
                "job_id"
            ]
            for index in range(20)
        ]
        now = connection.execute(select(func.clock_timestamp())).scalar_one()
        connection.execute(update(s.jobs).values(eligible_since=now - timedelta(seconds=180)))
        connection.execute(
            update(s.admission_counters)
            .where(s.admission_counters.c.scope_type.in_(("GLOBAL", "TENANT")))
            .values(outstanding=40)
        )
        connection.execute(
            update(s.admission_counters)
            .where(s.admission_counters.c.scope_type == "USER")
            .values(outstanding=20)
        )
        connection.execute(
            s.admission_counters.insert().values(
                scope_type="USER",
                scope_id=f"{graph['tenant_id']}:{other_user}",
                outstanding=20,
                eligible_resumed_at=now,
            )
        )

    tenant_id = str(graph["tenant_id"])
    first_window = _snapshot_windows(migrated_postgres_engine)[tenant_id]
    assert [item.job_id for item in first_window.normal] == [str(job_id) for job_id in first[:16]]
    assert first_window.oldest_eligible.job_id == str(first[0])
    assert first_window.normal[0].eligible_wait_seconds >= 120
    cursor = first_window.continuation_cursor.decode().split(":")
    next_window = _snapshot_windows(
        migrated_postgres_engine, {tenant_id: (int(cursor[0]), int(cursor[1]), cursor[2])}
    )[tenant_id]
    assert [item.job_id for item in next_window.normal] == [
        str(job_id) for job_id in (*first[16:], *second[:12])
    ]
    assert next_window.oldest_eligible.job_id == str(first[0])
    assert next_window.normal[-1].eligible_wait_seconds < 5
    wrapped = _snapshot_windows(migrated_postgres_engine, {tenant_id: (0, 9999, str(new_uuid7()))})[
        tenant_id
    ]
    assert [item.job_id for item in wrapped.normal] == [str(job_id) for job_id in first[:16]]


def test_new_high_priority_job_invalidates_dispatch_proposal(migrated_postgres_engine, monkeypatch):
    from nexa.scheduler.policy import WeightedDominantResourceTimePolicy

    graph, _, ids = seed_dispatchable(migrated_postgres_engine)
    original = WeightedDominantResourceTimePolicy.decide
    inserted = []

    def submit_between_transactions(self, snapshot, now_ms):
        proposal = original(self, snapshot, now_ms)
        if not inserted:
            with migrated_postgres_engine.begin() as connection:
                new_id = seed_job(connection, graph, ready_sequence=2**31)["job_id"]
                connection.execute(
                    update(s.jobs)
                    .where(s.jobs.c.job_id == new_id)
                    .values(base_priority=2, eligible_since=func.clock_timestamp())
                )
                connection.execute(update(s.admission_counters).values(outstanding=2))
                inserted.append(new_id)
        return proposal

    monkeypatch.setattr(WeightedDominantResourceTimePolicy, "decide", submit_between_transactions)
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    assert service.tick(epoch) == NoDecision("proposal_changed")
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(s.allocations)).scalar_one() == 0
        assert (
            connection.execute(select(s.jobs.c.state).where(s.jobs.c.job_id == ids[0])).scalar_one()
            == "QUEUED"
        )
    chosen = service.tick(epoch)
    assert isinstance(chosen, Dispatch)
    assert chosen.job_id == str(inserted[0])


def test_quota_reduction_between_proposal_and_commit_cannot_allocate(
    migrated_postgres_engine, monkeypatch
):
    from nexa.scheduler.policy import WeightedDominantResourceTimePolicy

    graph, _, ids = seed_dispatchable(migrated_postgres_engine)
    original = WeightedDominantResourceTimePolicy.decide
    changed = False

    def reduce_quota_between_transactions(self, snapshot, now_ms):
        nonlocal changed
        proposal = original(self, snapshot, now_ms)
        if not changed:
            changed = True
            with migrated_postgres_engine.begin() as connection:
                connection.execute(
                    update(s.tenant_policies)
                    .where(s.tenant_policies.c.tenant_id == graph["tenant_id"])
                    .values(cpu_limit_millis=500)
                )
        return proposal

    monkeypatch.setattr(
        WeightedDominantResourceTimePolicy, "decide", reduce_quota_between_transactions
    )
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    assert service.tick(epoch) == NoDecision("proposal_changed")
    assert isinstance(service.tick(epoch), NoDecision)
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(s.allocations)).scalar_one() == 0
        job = connection.execute(
            select(s.jobs.c.state, s.jobs.c.eligible_since).where(s.jobs.c.job_id == ids[0])
        ).one()
    assert job.state == "QUEUED"
    assert job.eligible_since is None


def test_simultaneous_ticks_cannot_oversubscribe_tenant_active_limit(migrated_postgres_engine):
    graph, _, _ = seed_dispatchable(migrated_postgres_engine, count=2)
    factory = create_session_factory(migrated_postgres_engine)
    first = CoordinatorService(factory)
    second = CoordinatorService(factory, holder_id=first.holder_id)
    epoch = first.acquire()
    barrier = Barrier(2)

    def tick(service):
        barrier.wait(timeout=5)
        return service.tick(epoch)

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(tick, (first, second)))

    assert sum(isinstance(decision, Dispatch) for decision in decisions) == 1
    assert sum(isinstance(decision, NoDecision) for decision in decisions) == 1
    with migrated_postgres_engine.connect() as connection:
        held = (
            connection.execute(
                select(s.allocations.c.cpu_millis).where(s.allocations.c.state != "RELEASED")
            )
            .scalars()
            .all()
        )
        counters = dict(
            (row.scope_type, row.active_attempts)
            for row in connection.execute(
                select(s.admission_counters.c.scope_type, s.admission_counters.c.active_attempts)
            )
        )
        tenant_limit = connection.execute(
            select(s.tenant_policies.c.tenant_active_limit).where(
                s.tenant_policies.c.tenant_id == graph["tenant_id"]
            )
        ).scalar_one()
    assert held == [1000]
    assert counters == {"GLOBAL": 1, "TENANT": 1, "USER": 1}
    assert tenant_limit == 1


def test_ledger_db_fault_rolls_back_dispatch_and_new_process_catches_up(
    migrated_postgres_engine,
):
    _, _, job_ids = seed_dispatchable(migrated_postgres_engine, count=2)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.tenant_policies).values(tenant_active_limit=2, user_active_limit=2)
        )

    def capture(step):
        with migrated_postgres_engine.connect() as connection:
            ledger = connection.execute(select(s.fairness_ledgers)).mappings().one()
            return {
                "step": step,
                "db_time": connection.execute(select(func.clock_timestamp())).scalar_one(),
                "ledger_score": ledger["virtual_score"],
                "accounted_through": ledger["accounted_through"],
                "ledger_version": ledger["version"],
                "allocations": [
                    dict(row)
                    for row in connection.execute(
                        select(
                            s.allocations.c.allocation_id,
                            s.allocations.c.job_id,
                            s.allocations.c.state,
                            s.allocations.c.cpu_millis,
                        ).order_by(s.allocations.c.allocation_id)
                    ).mappings()
                ],
                "active_counters": {
                    row["scope_type"]: row["active_attempts"]
                    for row in connection.execute(
                        select(
                            s.admission_counters.c.scope_type,
                            s.admission_counters.c.active_attempts,
                        )
                    ).mappings()
                },
                "attempt_count": connection.execute(
                    select(func.count()).select_from(s.attempts)
                ).scalar_one(),
                "event_count": connection.execute(
                    select(func.count()).select_from(s.events)
                ).scalar_one(),
            }

    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    assert isinstance(service.tick(epoch), Dispatch)
    before = capture("first_dispatch_committed")
    time.sleep(0.03)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE FUNCTION b13_fail_ledger_update() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'b13 injected ledger failure'; END $$"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER b13_fail_ledger BEFORE UPDATE ON fairness_ledgers "
                "FOR EACH ROW EXECUTE FUNCTION b13_fail_ledger_update()"
            )
        )
    try:
        with pytest.raises(DBAPIError) as failed:
            service.tick(epoch)
        during = capture("ledger_update_rejected")
        assert during["ledger_score"] == before["ledger_score"]
        assert during["accounted_through"] == before["accounted_through"]
        assert during["ledger_version"] == before["ledger_version"]
        assert during["allocations"] == before["allocations"]
        assert during["active_counters"] == before["active_counters"]
        assert during["attempt_count"] == before["attempt_count"] == 1
        assert during["event_count"] == before["event_count"] == 1
    finally:
        with migrated_postgres_engine.begin() as connection:
            connection.execute(text("DROP TRIGGER b13_fail_ledger ON fairness_ledgers"))
            connection.execute(text("DROP FUNCTION b13_fail_ledger_update()"))

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from os import environ; from uuid import UUID; "
            "from sqlalchemy import create_engine; "
            "from nexa.coordinator.service import CoordinatorService; "
            "from nexa.infrastructure.persistence.database import create_session_factory; "
            "engine = create_engine(environ['NEXA_TEST_DATABASE_URL'], pool_pre_ping=True); "
            "service = CoordinatorService(create_session_factory(engine), "
            "holder_id=UUID(environ['B13_HOLDER_ID'])); "
            "print(type(service.tick(service.acquire())).__name__); engine.dispose()",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
        env={**os.environ, "B13_HOLDER_ID": str(service.holder_id)},
    )
    assert child.stdout.strip() == "Dispatch", child.stderr
    after = capture("new_process_dispatch_committed")
    assert after["ledger_score"] > before["ledger_score"]
    assert after["accounted_through"] > before["accounted_through"]
    assert after["attempt_count"] == after["event_count"] == 2
    assert {row["job_id"] for row in after["allocations"]} == set(job_ids)
    assert after["active_counters"] == {"GLOBAL": 2, "TENANT": 2, "USER": 2}
    with migrated_postgres_engine.connect() as connection:
        charged = sum(
            connection.execute(
                select(s.allocation_ledger_segments.c.charged_amount).where(
                    s.allocation_ledger_segments.c.allocation_id
                    == before["allocations"][0]["allocation_id"]
                )
            ).scalars(),
            Decimal(0),
        )
    assert after["ledger_score"] - before["ledger_score"] == charged

    if output := os.environ.get("B13_DB_FAULT_TIMELINE"):
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "fault": type(failed.value).__name__,
                    "sqlstate": getattr(failed.value.orig, "sqlstate", None),
                    "epoch": epoch,
                    "timeline": [before, during, after],
                },
                default=str,
                indent=2,
            )
            + "\n"
        )


def _dispatched_service(engine):
    seed_dispatchable(engine, count=2)
    with engine.begin() as connection:
        connection.execute(
            update(s.tenant_policies).values(tenant_active_limit=2, user_active_limit=2)
        )
    service = CoordinatorService(create_session_factory(engine))
    epoch = service.acquire()
    assert isinstance(service.tick(epoch), Dispatch)
    return service, epoch


def _ledger_matches_segments(engine):
    from decimal import localcontext

    with engine.connect() as connection:
        ledgers = {
            row.tenant_id: row.virtual_score
            for row in connection.execute(select(s.fairness_ledgers)).mappings()
        }
        segments = list(connection.execute(select(s.allocation_ledger_segments)).mappings())
    with localcontext() as context:
        context.prec = 100
        for tenant_id, score in ledgers.items():
            charged = sum(row.charged_amount for row in segments if row.tenant_id == tenant_id)
            assert abs(score - charged) <= Decimal("1e-40"), (score, charged)


@pytest.mark.parametrize("phase", ["snapshot", "commit"])
def test_accounting_heartbeat_commits_while_decision_transaction_is_slow(
    migrated_postgres_engine, monkeypatch, phase
):
    from threading import Event, Thread

    import nexa.coordinator.snapshot as snapshot_module

    service, epoch = _dispatched_service(migrated_postgres_engine)
    original = snapshot_module.read_snapshot
    entered = Event()

    def slow_read(session, *args, **kwargs):
        slow = kwargs.get("replay_eligibility", True) is (phase == "snapshot")
        if slow and not entered.is_set():
            entered.set()
            time.sleep(1.5)
        return original(session, *args, **kwargs)

    monkeypatch.setattr(snapshot_module, "read_snapshot", slow_read)
    outcome = {}

    def tick():
        try:
            outcome["decision"] = service.tick(epoch)
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            outcome["error"] = exc

    thread = Thread(target=tick)
    thread.start()
    assert entered.wait(10)
    time.sleep(0.1)
    started = time.monotonic()
    boundary = service.account(epoch)
    elapsed = time.monotonic() - started
    with migrated_postgres_engine.connect() as connection:
        committed = tuple(
            connection.execute(select(s.fairness_ledgers.c.accounted_through)).scalars()
        )
    assert thread.is_alive()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome
    assert elapsed < 0.5
    assert boundary is not None
    assert committed and all(value == boundary for value in committed)
    _ledger_matches_segments(migrated_postgres_engine)


def test_policy_locked_accounting_refunds_heartbeat_overlap_exactly(migrated_postgres_engine):
    from decimal import localcontext

    from nexa.coordinator.accounting import epoch_ms
    from nexa.infrastructure.persistence.locking import clock_timestamp

    service, epoch = _dispatched_service(migrated_postgres_engine)
    factory = create_session_factory(migrated_postgres_engine)
    with factory.begin() as session:
        session.execute(
            select(s.policy_versions)
            .where(s.policy_versions.c.is_current.is_(True))
            .with_for_update()
        ).one()
        now = clock_timestamp(session)
        ledger = session.execute(select(s.fairness_ledgers)).mappings().one()
        segment = (
            session.execute(
                select(s.allocation_ledger_segments).where(
                    s.allocation_ledger_segments.c.ended_at.is_(None)
                )
            )
            .mappings()
            .one()
        )
        time.sleep(0.05)
        boundary = service.account(epoch)
        assert boundary > now
        account_locked(session, now)
        after = session.execute(select(s.fairness_ledgers)).mappings().one()
    start = max(ledger["accounted_through"], segment["started_at"])
    elapsed = Decimal(epoch_ms(now) - epoch_ms(start)) / Decimal(1000)
    with localcontext() as context:
        context.prec = 50
        expected = ledger["virtual_score"] + segment["dominant_share"] * elapsed / segment["weight"]
    assert after["accounted_through"] == now
    assert abs(after["virtual_score"] - expected) <= Decimal("1e-40")
    _ledger_matches_segments(migrated_postgres_engine)


def test_accounting_detects_real_db_time_regression(migrated_postgres_engine):
    from nexa.infrastructure.persistence.locking import clock_timestamp

    service, epoch = _dispatched_service(migrated_postgres_engine)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.fairness_ledgers).values(
                accounted_through=func.clock_timestamp() + timedelta(hours=1)
            )
        )
    with pytest.raises(RuntimeError, match="regressed"):
        service.account(epoch)
    with (
        pytest.raises(RuntimeError, match="regressed"),
        create_session_factory(migrated_postgres_engine).begin() as session,
    ):
        account_locked(session, clock_timestamp(session))


def test_accounting_heartbeat_requires_leadership_and_skips_write_frozen(
    migrated_postgres_engine,
):
    from nexa.coordinator.service import LeadershipLost

    service, epoch = _dispatched_service(migrated_postgres_engine)
    other = CoordinatorService(create_session_factory(migrated_postgres_engine))
    with pytest.raises(LeadershipLost):
        other.account(epoch)
    with migrated_postgres_engine.begin() as connection:
        before = connection.execute(select(s.fairness_ledgers.c.version)).scalar_one()
        connection.execute(
            update(s.policy_versions)
            .where(s.policy_versions.c.is_current.is_(True))
            .values(operational_mode="WRITE_FROZEN")
        )
    assert service.account(epoch) is None
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(s.fairness_ledgers.c.version)).scalar_one() == before


def test_accounting_heartbeat_locks_ledgers_once_without_fairness_state(
    migrated_postgres_engine,
):
    service, epoch = _dispatched_service(migrated_postgres_engine)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(migrated_postgres_engine, "before_cursor_execute", capture)
    try:
        assert service.account(epoch) is not None
    finally:
        event.remove(migrated_postgres_engine, "before_cursor_execute", capture)
    assert sum(item.startswith("INSERT INTO fairness_ledgers") for item in statements) == 1
    assert not any("fairness_state" in item for item in statements)
    assert not any("policy_versions" in item and "FOR UPDATE" in item for item in statements)


def test_accounting_heartbeat_db_fault_rolls_back_and_next_heartbeat_catches_up(
    migrated_postgres_engine,
):
    service, epoch = _dispatched_service(migrated_postgres_engine)
    assert service.account(epoch) is not None

    def ledger():
        with migrated_postgres_engine.connect() as connection:
            return connection.execute(
                select(
                    s.fairness_ledgers.c.virtual_score,
                    s.fairness_ledgers.c.accounted_through,
                    s.fairness_ledgers.c.version,
                )
            ).one()

    before = ledger()
    time.sleep(0.03)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE FUNCTION b13_fail_ledger_update() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'b13 injected ledger failure'; END $$"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER b13_fail_ledger BEFORE UPDATE ON fairness_ledgers "
                "FOR EACH ROW EXECUTE FUNCTION b13_fail_ledger_update()"
            )
        )
    try:
        with pytest.raises(DBAPIError):
            service.account(epoch)
        assert ledger() == before
    finally:
        with migrated_postgres_engine.begin() as connection:
            connection.execute(text("DROP TRIGGER b13_fail_ledger ON fairness_ledgers"))
            connection.execute(text("DROP FUNCTION b13_fail_ledger_update()"))

    boundary = service.account(epoch)
    after = ledger()
    assert after.accounted_through == boundary > before.accounted_through
    assert after.virtual_score > before.virtual_score
    _ledger_matches_segments(migrated_postgres_engine)
