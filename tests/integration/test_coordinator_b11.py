"""Database integration, not Docker or product end-to-end evidence."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema import coordinator_leadership

pytestmark = pytest.mark.postgres


def test_two_holders_takeover_and_stale_epoch(migrated_postgres_engine):
    from nexa.coordinator.service import CoordinatorService

    factory = create_session_factory(migrated_postgres_engine)
    first = CoordinatorService(factory, holder_id=new_uuid7())
    second = CoordinatorService(factory, holder_id=new_uuid7())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda service: service.acquire(), [first, second]))
    assert sum(epoch is not None for epoch in results) == 1
    winner, loser = (first, second) if results[0] else (second, first)
    epoch = next(value for value in results if value is not None)
    assert winner.renew(epoch)
    assert not loser.renew(epoch)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(coordinator_leadership).values(
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1)
            )
        )
    newer = loser.acquire()
    assert newer == epoch + 1
    assert not winner.renew(epoch)
    with pytest.raises(RuntimeError, match="leadership"):
        winner.tick(epoch)
    with migrated_postgres_engine.connect() as connection:
        row = connection.execute(select(coordinator_leadership)).mappings().one()
    assert row["holder_id"] == loser.holder_id


def seed_dispatchable(engine, *, count=1, existing_worker=None, template_id=None):
    from decimal import Decimal

    from nexa.infrastructure.persistence import schema as s
    from tests.integration._factories import IMAGE_DIGEST, seed_job, seed_tenant_graph, seed_worker

    with engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="coordinator", template_id=template_id)
        worker = seed_worker(connection, label="coordinator", existing=existing_worker)
        connection.execute(
            update(s.policy_versions).values(
                global_outstanding_limit=1000, operational_mode="NORMAL", is_current=True
            )
        )
        connection.execute(
            s.tenant_policies.insert().values(
                tenant_id=graph["tenant_id"],
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
        for scope_type, scope_id in [
            ("GLOBAL", "global"),
            ("TENANT", str(graph["tenant_id"])),
            ("USER", f"{graph['tenant_id']}:{graph['user_id']}"),
        ]:
            connection.execute(
                __import__("sqlalchemy.dialects.postgresql", fromlist=["insert"])
                .insert(s.admission_counters)
                .values(scope_type=scope_type, scope_id=scope_id, outstanding=count)
                .on_conflict_do_update(
                    index_elements=["scope_type", "scope_id"], set_={"outstanding": count}
                )
            )
        connection.execute(update(s.workers).values(last_heartbeat_at=func.clock_timestamp()))
        connection.execute(update(s.jobs).values(eligible_since=func.clock_timestamp()))
        capabilities = {
            "architectures": ["linux/arm64"],
            "adapter_id": "cpu.iterative",
            "adapter_version": "1.0.0",
            "image_digest": IMAGE_DIGEST,
            "device": "CPU",
            "framework": "NEXA_CPU",
            "framework_version": "1.0.0",
            "cuda_runtime_min": None,
            "driver_min": None,
            "compute_capability_min": None,
        }
        connection.execute(update(s.template_versions).values(capability_requirements=capabilities))
        connection.execute(
            update(s.worker_inventories).values(
                workload_capabilities={
                    "adapters": [{"adapter_id": "cpu.iterative", "adapter_version": "1.0.0"}],
                    "images": [
                        {
                            "image_digest": IMAGE_DIGEST,
                            "architecture": "linux/arm64",
                            "verified": True,
                        }
                    ],
                    "frameworks": [
                        {"framework": "NEXA_CPU", "framework_version": "1.0.0", "device": "CPU"}
                    ],
                }
            )
        )
        job_ids = [seed_job(connection, graph)["job_id"] for _ in range(count)]
        connection.execute(update(s.jobs).values(eligible_since=func.clock_timestamp()))
    return graph, worker, job_ids


def test_dispatch_is_atomic_policy_constrained_and_survives_restart(migrated_postgres_engine):
    from nexa.coordinator.service import CoordinatorService
    from nexa.domain.scheduling import Dispatch, NoDecision
    from nexa.infrastructure.persistence import schema as s

    graph, worker, job_ids = seed_dispatchable(migrated_postgres_engine, count=2)
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    result = service.tick(epoch)
    assert isinstance(result, Dispatch)
    assert isinstance(service.tick(epoch), NoDecision)
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(s.attempts)).scalar_one() == 1
        allocation = connection.execute(select(s.allocations)).mappings().one()
        attempt = connection.execute(select(s.attempts)).mappings().one()
        job = (
            connection.execute(select(s.jobs).where(s.jobs.c.job_id == allocation["job_id"]))
            .mappings()
            .one()
        )
        assert allocation["state"] == "HELD"
        assert attempt["state"] == "CREATED"
        assert job["state"] == "DISPATCHING"
        assert job["job_fence"] == attempt["job_fence"] == 1
        assert (
            connection.execute(select(func.count()).select_from(s.attempt_leases)).scalar_one() == 1
        )
        assert (
            connection.execute(
                select(func.count()).select_from(s.attempt_authority_grants)
            ).scalar_one()
            == 1
        )
        assert connection.execute(
            select(s.admission_counters.c.active_attempts)
        ).scalars().all() == [1, 1, 1]
        assert (
            connection.execute(
                select(func.count()).select_from(s.allocation_ledger_segments)
            ).scalar_one()
            == 1
        )


def test_leader_replaced_after_policy_proposal_cannot_dispatch(
    migrated_postgres_engine, monkeypatch
):
    from nexa.coordinator.service import CoordinatorService, LeadershipLost
    from nexa.infrastructure.persistence import schema as s
    from nexa.scheduler.policy import WeightedDominantResourceTimePolicy

    seed_dispatchable(migrated_postgres_engine)
    factory = create_session_factory(migrated_postgres_engine)
    first, second = CoordinatorService(factory), CoordinatorService(factory)
    epoch = first.acquire()
    original = WeightedDominantResourceTimePolicy.decide
    called = False

    def replace_leader(self, snapshot, now_ms):
        nonlocal called
        result = original(self, snapshot, now_ms)
        if not called:
            called = True
            with migrated_postgres_engine.begin() as connection:
                connection.execute(
                    update(s.coordinator_leadership).values(
                        lease_expires_at=func.clock_timestamp() - timedelta(seconds=1)
                    )
                )
            assert second.acquire() == epoch + 1
        return result

    monkeypatch.setattr(WeightedDominantResourceTimePolicy, "decide", replace_leader)
    with pytest.raises(LeadershipLost):
        first.tick(epoch)
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(s.allocations)).scalar_one() == 0


def test_reservation_drains_then_dispatches_and_accounts_quarantine(migrated_postgres_engine):
    from decimal import Decimal, localcontext

    from nexa.coordinator.accounting import account_locked, rebase_locked
    from nexa.coordinator.service import CoordinatorService
    from nexa.domain.scheduling import CreateReservation, Dispatch
    from nexa.infrastructure.persistence import schema as s

    seed_dispatchable(migrated_postgres_engine)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.jobs).values(eligible_since=func.clock_timestamp() - timedelta(seconds=121))
        )
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    assert isinstance(service.tick(epoch), CreateReservation)
    assert isinstance(service.tick(epoch), Dispatch)
    with service.session_factory.begin() as session:
        baseline = session.execute(select(func.clock_timestamp())).scalar_one()
        account_locked(session, baseline)
        before = session.execute(select(s.fairness_ledgers.c.virtual_score)).scalar_one()
        segment = (
            session.execute(
                select(s.allocation_ledger_segments).where(
                    s.allocation_ledger_segments.c.ended_at.is_(None)
                )
            )
            .mappings()
            .one()
        )
        session.execute(update(s.allocations).values(state="QUARANTINED", quarantined_at=baseline))
        later = baseline + timedelta(seconds=10)
        account_locked(session, later)
        account_locked(session, later)
        after = session.execute(select(s.fairness_ledgers.c.virtual_score)).scalar_one()
        with localcontext() as context:
            context.prec = 50
            assert after == before + segment["dominant_share"] * Decimal(10) / segment["weight"]
        session.execute(update(s.allocations).values(state="RELEASED", released_at=later))
        rebase_locked(session, later)
        account_locked(session, later + timedelta(seconds=10))
        assert session.execute(select(s.fairness_ledgers.c.virtual_score)).scalar_one() == after
        assert (
            session.execute(
                select(func.count())
                .select_from(s.allocation_ledger_segments)
                .where(s.allocation_ledger_segments.c.ended_at.is_(None))
            ).scalar_one()
            == 0
        )


def test_policy_weight_change_closes_old_segment(migrated_postgres_engine, tmp_path):
    from decimal import Decimal

    from nexa.coordinator.service import CoordinatorService
    from nexa.infrastructure.persistence import schema as s
    from tests.integration.test_policy_service import _context

    identity, admin, policy, principal = _context(migrated_postgres_engine, tmp_path)
    graph, _, _ = seed_dispatchable(migrated_postgres_engine)
    service = CoordinatorService(identity.session_factory)
    epoch = service.acquire()
    service.tick(epoch)
    policy.update_tenant_policy(
        principal,
        tenant_id=graph["tenant_id"],
        expected_version=1,
        changes={"weight": Decimal(2)},
        idempotency_key="b11-weight-change-0001",
        request_hash="sha256:" + "f" * 64,
    )
    with migrated_postgres_engine.connect() as connection:
        rows = (
            connection.execute(
                select(s.allocation_ledger_segments).order_by(
                    s.allocation_ledger_segments.c.started_at
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 2
        assert rows[0]["ended_at"] == rows[1]["started_at"]
        assert rows[0]["weight"] == Decimal(1)
        assert rows[1]["weight"] == Decimal(2)


def test_aggregate_dominant_share_matches_b04_without_rounding_each_job(migrated_postgres_engine):
    from decimal import Decimal, localcontext

    from nexa.coordinator.accounting import account_locked
    from nexa.coordinator.service import CoordinatorService
    from nexa.infrastructure.persistence import schema as s

    seed_dispatchable(migrated_postgres_engine, count=3)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.tenant_policies).values(tenant_active_limit=4, user_active_limit=4)
        )
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    for _ in range(3):
        service.tick(epoch)
    with service.session_factory.begin() as session:
        now = session.execute(select(func.clock_timestamp())).scalar_one()
        account_locked(session, now)
        before = session.execute(select(s.fairness_ledgers.c.virtual_score)).scalar_one()
        account_locked(session, now + timedelta(seconds=10))
        after = session.execute(select(s.fairness_ledgers.c.virtual_score)).scalar_one()
        with localcontext() as context:
            context.prec = 50
            assert after == before + Decimal(5)


def test_seventeenth_high_priority_job_is_in_bounded_window(migrated_postgres_engine):
    from nexa.coordinator.service import CoordinatorService
    from nexa.domain.scheduling import Dispatch
    from nexa.infrastructure.persistence import schema as s

    _, _, ids = seed_dispatchable(migrated_postgres_engine, count=17)
    with migrated_postgres_engine.begin() as connection:
        for index, job_id in enumerate(ids):
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == job_id)
                .values(ready_sequence=index, base_priority=2 if index == 16 else 0)
            )
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    result = service.tick(service.acquire())
    assert isinstance(result, Dispatch)
    assert result.job_id == str(ids[16])


def test_dispatch_stops_age_for_concurrency_blocked_jobs(migrated_postgres_engine):
    from nexa.coordinator.service import CoordinatorService
    from nexa.domain.scheduling import NoDecision
    from nexa.infrastructure.persistence import schema as s

    _, _, ids = seed_dispatchable(migrated_postgres_engine, count=2)
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    service.tick(epoch)
    with migrated_postgres_engine.connect() as connection:
        waiting = (
            connection.execute(select(s.jobs).where(s.jobs.c.state == "QUEUED")).mappings().one()
        )
        counter = (
            connection.execute(
                select(s.admission_counters).where(s.admission_counters.c.scope_type == "USER")
            )
            .mappings()
            .one()
        )
    assert waiting["eligible_since"] is not None
    assert counter["active_attempts"] == 1
    assert isinstance(service.tick(epoch), NoDecision)
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(s.reservations)).all() == []


def test_dispatch_events_use_contract_names(migrated_postgres_engine):
    from nexa.coordinator.service import CoordinatorService
    from nexa.infrastructure.persistence import schema as s

    seed_dispatchable(migrated_postgres_engine)
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    service.tick(service.acquire())
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(s.events.c.event_type)).scalar_one() == "JOB_DISPATCHING"


def test_heartbeat_capacity_change_rebases_held_charge_and_restart(
    migrated_postgres_engine, tmp_path
):
    """The old denominator is charged through the inventory commit boundary."""
    from decimal import Decimal
    from uuid import UUID

    from nexa.coordinator.service import CoordinatorService
    from nexa.infrastructure.persistence import schema as s
    from tests.api.test_http_contract import _client
    from tests.integration.test_worker_api_b10 import (
        WORKER_ID,
        _bootstrap_worker,
        _create_incarnation,
        _inventory,
    )

    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-capacity-incarnation"
        )
        seed_dispatchable(
            engine,
            existing_worker=(UUID(WORKER_ID), UUID(incarnation["worker_incarnation_id"])),
        )
        service = CoordinatorService(create_session_factory(engine))
        epoch = service.acquire()
        service.tick(epoch)
        with engine.connect() as connection:
            old = connection.execute(select(s.allocation_ledger_segments)).mappings().one()
            score_before = connection.execute(
                select(s.fairness_ledgers.c.virtual_score)
            ).scalar_one()
        assert abs(old["dominant_share"] - Decimal(1) / 6) < Decimal("1e-12")
        inventory = _inventory()
        inventory["host_cpu_millis"] = 10_000
        inventory["allocatable"]["cpu_millis"] = 8_000
        changed = client.post(
            f"/v1/workers/{WORKER_ID}/heartbeat",
            headers={"Authorization": f"Bearer {credential}", "X-Callback-Id": str(new_uuid7())},
            json={
                "worker_incarnation_id": incarnation["worker_incarnation_id"],
                "observed_health": "STARTING",
                "reconcile_complete": False,
                "inventory": inventory,
                "observed_containers": [],
            },
        )
        assert changed.status_code == 200, changed.text
        with engine.connect() as connection:
            segments = (
                connection.execute(
                    select(s.allocation_ledger_segments).order_by(
                        s.allocation_ledger_segments.c.started_at
                    )
                )
                .mappings()
                .all()
            )
            score_after = connection.execute(
                select(s.fairness_ledgers.c.virtual_score)
            ).scalar_one()
            current_inventory = connection.execute(
                select(s.workers.c.current_inventory_version)
            ).scalar_one()
        assert current_inventory == 2
        assert len(segments) == 2
        assert segments[0]["ended_at"] == segments[1]["started_at"]
        assert segments[0]["dominant_share"] == old["dominant_share"]
        assert segments[1]["dominant_share"] == Decimal("0.125")
        assert score_after >= score_before
        restarted = CoordinatorService(create_session_factory(engine), holder_id=service.holder_id)
        assert restarted.acquire() == epoch
        restarted.tick(epoch)
        with engine.connect() as connection:
            assert (
                connection.execute(select(s.fairness_ledgers.c.virtual_score)).scalar_one()
                >= score_after
            )
            assert connection.execute(
                select(s.allocation_ledger_segments.c.dominant_share).where(
                    s.allocation_ledger_segments.c.ended_at.is_(None)
                )
            ).scalar_one() == Decimal("0.125")


def test_quota_blocked_time_does_not_age_into_reservation(migrated_postgres_engine):
    from nexa.coordinator.service import CoordinatorService
    from nexa.domain.scheduling import Dispatch
    from nexa.infrastructure.persistence import schema as s

    _, _, ids = seed_dispatchable(migrated_postgres_engine)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.jobs)
            .where(s.jobs.c.job_id == ids[0])
            .values(eligible_since=func.clock_timestamp() - timedelta(seconds=180))
        )
        connection.execute(update(s.tenant_policies).values(cpu_limit_millis=500))
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    service.tick(epoch)
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(s.jobs.c.eligible_since)).scalar_one() is None
        assert connection.execute(select(s.reservations)).all() == []
    with migrated_postgres_engine.begin() as connection:
        connection.execute(update(s.tenant_policies).values(cpu_limit_millis=6000))
    assert isinstance(service.tick(epoch), Dispatch)
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(s.reservations)).all() == []


def test_occupied_capacity_keeps_eligible_age(migrated_postgres_engine):
    from nexa.coordinator.service import CoordinatorService
    from nexa.infrastructure.persistence import schema as s

    _, _, ids = seed_dispatchable(migrated_postgres_engine, count=2)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.tenant_policies).values(tenant_active_limit=2, user_active_limit=2)
        )
        connection.execute(update(s.worker_inventories).values(allocatable_cpu_millis=1000))
        connection.execute(
            update(s.jobs).values(eligible_since=func.clock_timestamp() - timedelta(seconds=30))
        )
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    service.tick(service.acquire())
    with migrated_postgres_engine.connect() as connection:
        waiting = (
            connection.execute(select(s.jobs).where(s.jobs.c.state == "QUEUED")).mappings().one()
        )
    assert waiting["job_id"] in ids
    assert waiting["eligible_since"] is not None
    assert (waiting["created_at"] - waiting["eligible_since"]).total_seconds() > 20


def test_capability_block_resets_age_before_redispatch(migrated_postgres_engine):
    from datetime import UTC, datetime

    from nexa.coordinator.service import CoordinatorService
    from nexa.domain.scheduling import Dispatch
    from nexa.infrastructure.persistence import schema as s

    _, _, ids = seed_dispatchable(migrated_postgres_engine)
    with migrated_postgres_engine.connect() as connection:
        original = connection.execute(select(s.worker_inventories)).mappings().one()

    def publish_inventory(version, *, compatible):
        capabilities = dict(original["workload_capabilities"])
        if not compatible:
            capabilities["images"] = []
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                s.worker_inventories.insert().values(
                    inventory_id=new_uuid7(),
                    worker_id=original["worker_id"],
                    worker_incarnation_id=original["worker_incarnation_id"],
                    inventory_version=version,
                    architecture=original["architecture"],
                    host_cpu_millis=original["host_cpu_millis"],
                    host_memory_bytes=original["host_memory_bytes"],
                    allocatable_cpu_millis=original["allocatable_cpu_millis"],
                    allocatable_memory_bytes=original["allocatable_memory_bytes"],
                    allocatable_gpu_count=0,
                    runtime_capabilities=original["runtime_capabilities"],
                    workload_capabilities=capabilities,
                    checksum="sha256:" + f"{version:064x}",
                    observed_at=datetime.now(UTC),
                )
            )
            connection.execute(update(s.workers).values(current_inventory_version=version))

    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.jobs)
            .where(s.jobs.c.job_id == ids[0])
            .values(eligible_since=func.clock_timestamp() - timedelta(seconds=180))
        )
    publish_inventory(2, compatible=False)
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    epoch = service.acquire()
    service.tick(epoch)
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(s.jobs.c.eligible_since)).scalar_one() is None
        assert connection.execute(select(s.reservations)).all() == []
    publish_inventory(3, compatible=True)
    assert isinstance(service.tick(epoch), Dispatch)
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(s.reservations)).all() == []
