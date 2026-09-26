"""B13 real-time PostgreSQL fairness measurement oracles."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from benchmarks.b13 import runtime_fairness
from nexa.coordinator.service import CoordinatorService
from nexa.domain.scheduling import Dispatch
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from tests.integration.test_coordinator_b11 import seed_dispatchable


def test_weighted_jain_uses_resource_time_divided_by_weight():
    assert runtime_fairness.weighted_jain(
        {"a": Decimal(1), "b": Decimal(2), "c": Decimal(4)},
        {"a": Decimal(1), "b": Decimal(2), "c": Decimal(4)},
    ) == Decimal(1)
    assert runtime_fairness.weighted_jain(
        {"a": Decimal(1), "b": Decimal(1), "c": Decimal(1)},
        {"a": Decimal(1), "b": Decimal(2), "c": Decimal(4)},
    ) == Decimal(49) / Decimal(63)


def test_window_resource_time_clips_held_allocations_at_both_boundaries():
    start = datetime(2026, 9, 25, tzinfo=UTC)
    end = start + timedelta(seconds=3)
    allocations = [
        {
            "tenant_id": "a",
            "cpu_millis": 1000,
            "held_at": start - timedelta(seconds=1),
            "released_at": start + timedelta(seconds=2),
        },
        {
            "tenant_id": "a",
            "cpu_millis": 1000,
            "held_at": start + timedelta(seconds=1),
            "released_at": end + timedelta(seconds=1),
        },
        {
            "tenant_id": "b",
            "cpu_millis": 1000,
            "held_at": start - timedelta(seconds=1),
            "released_at": end,
        },
    ]

    measured = runtime_fairness.window_resource_time(allocations, start, end, 3000)

    assert measured["a"] == Decimal(4) / Decimal(3)
    assert measured["b"] == Decimal(1)


@pytest.mark.postgres
def test_fixture_completion_releases_once_and_closes_charge(migrated_postgres_engine):
    _, _, job_ids = seed_dispatchable(migrated_postgres_engine)
    service = CoordinatorService(create_session_factory(migrated_postgres_engine))
    assert isinstance(service.tick(service.acquire()), Dispatch)
    with migrated_postgres_engine.connect() as connection:
        allocation_id = connection.execute(select(s.allocations.c.allocation_id)).scalar_one()

    runtime_fairness.complete_fixture_allocation(migrated_postgres_engine, service, allocation_id)

    with migrated_postgres_engine.connect() as connection:
        allocation = connection.execute(select(s.allocations)).mappings().one()
        assert allocation["state"] == "RELEASED"
        assert allocation["released_at"] is not None
        assert connection.execute(select(s.jobs.c.state)).scalar_one() == "SUCCEEDED"
        assert connection.execute(select(s.attempts.c.state)).scalar_one() == "SUCCEEDED"
        assert (
            connection.execute(
                select(func.count()).select_from(s.allocation_ledger_segments)
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(select(s.allocation_ledger_segments.c.ended_at)).scalar_one()
            is not None
        )
        assert set(
            connection.execute(select(s.admission_counters.c.active_attempts)).scalars()
        ) == {0}
        assert set(connection.execute(select(s.admission_counters.c.outstanding)).scalars()) == {0}
        assert connection.execute(select(s.jobs.c.job_id)).scalar_one() == job_ids[0]


@pytest.mark.postgres
def test_realtime_cohort_keeps_queued_demand_and_records_three_windows(migrated_postgres_engine):
    weights = runtime_fairness.prepare_cohort(migrated_postgres_engine, jobs_per_tenant=30)

    report = runtime_fairness.measure(
        migrated_postgres_engine,
        weights=weights,
        warmup_seconds=1,
        window_seconds=1,
        windows=3,
        execution_seconds=0.5,
    )

    assert sorted(weights.values()) == [Decimal(1), Decimal(2), Decimal(4)]
    assert len(report["windows"]) == 3
    assert report["errors"] == []
    assert all(count > 0 for count in report["queued_after"].values())
    assert all(window["weighted_jain"] is not None for window in report["windows"])
    assert report["max_held_cpu_millis"] <= 3000
