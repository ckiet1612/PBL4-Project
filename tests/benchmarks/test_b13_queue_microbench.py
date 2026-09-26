"""Behavioral checks for the B13 PostgreSQL query-plan fixture."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select, text, update

from benchmarks.b13 import queue_microbench
from nexa.coordinator.service import CoordinatorService
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.integration._factories import seed_job
from tests.integration.test_coordinator_b11 import seed_dispatchable


@pytest.mark.postgres
def test_event_plan_fixture_records_boundary_without_rewriting_queue(migrated_postgres_engine):
    graph, _, job_ids = seed_dispatchable(migrated_postgres_engine)
    with migrated_postgres_engine.connect() as connection:
        original_age = connection.execute(
            select(s.jobs.c.eligible_since).where(s.jobs.c.job_id == job_ids[0])
        ).scalar_one()

    chosen = queue_microbench._prepare_eligibility_event(migrated_postgres_engine)

    with migrated_postgres_engine.connect() as connection:
        age = connection.execute(
            select(s.jobs.c.eligible_since).where(s.jobs.c.job_id == job_ids[0])
        ).scalar_one()
        event = connection.execute(select(s.queue_eligibility_events)).mappings().one()
    assert chosen == graph["tenant_id"]
    assert age == original_age
    assert event["tenant_id"] == graph["tenant_id"]
    assert event["old_state"]["cpu"] == 6000
    assert event["new_state"]["cpu"] == 500
    assert event["completed_at"] is None


@pytest.mark.postgres
@pytest.mark.parametrize("age_profile", ["fresh", "aged", "mixed", "multi", "after_resume"])
def test_resume_window_does_not_read_all_queued_jobs(migrated_postgres_engine, age_profile):
    graph, _, _ = seed_dispatchable(migrated_postgres_engine, count=0)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(s.tenant_policies)
            .where(s.tenant_policies.c.tenant_id == graph["tenant_id"])
            .values(outstanding_limit=2000, user_outstanding_limit=2000)
        )
        for sequence in range(1000):
            seed_job(connection, graph, ready_sequence=sequence)
        now = datetime.now(UTC)
        age = now - timedelta(seconds=180) if age_profile in {"aged", "multi"} else now
        resume = now - timedelta(seconds=120) if age_profile in {"aged", "multi"} else now
        if age_profile == "after_resume":
            # Every base age is later than the resume boundary, as after a
            # quota release under default quota: no Job is clipped to resume.
            resume = now - timedelta(seconds=30)
        connection.execute(update(s.jobs).values(eligible_since=age))
        if age_profile == "mixed":
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.ready_sequence >= 500)
                .values(eligible_since=now - timedelta(seconds=180))
            )
        other_user = None
        if age_profile == "multi":
            other_user = new_uuid7()
            connection.execute(
                s.users.insert().values(
                    user_id=other_user,
                    username="b13-multi-plan-user",
                    display_name="Multi plan user",
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
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.ready_sequence >= 500)
                .values(submitter_user_id=other_user)
            )
            connection.execute(
                s.admission_counters.insert().values(
                    scope_type="USER",
                    scope_id=f"{graph['tenant_id']}:{other_user}",
                    outstanding=500,
                    eligible_resumed_at=now,
                )
            )
        counters = update(s.admission_counters)
        if other_user is not None:
            counters = counters.where(
                s.admission_counters.c.scope_id != f"{graph['tenant_id']}:{other_user}"
            )
        connection.execute(counters.values(outstanding=1000, eligible_resumed_at=resume))

    measurement = queue_microbench._measure(migrated_postgres_engine, 1)
    job_reads = [
        node["Actual Rows"] * node.get("Actual Loops", 1)
        for item in measurement["plans"]
        for node in _plan_nodes(item["plan"][0]["Plan"])
        if node.get("Relation Name") == "jobs"
        and node["Node Type"] in {"Index Scan", "Bitmap Heap Scan", "Seq Scan"}
    ]
    assert measurement["runs"][0]["error"] is None
    assert measurement["runs"][0]["accounting_heartbeat_ms"] is not None
    assert any("fairness_ledgers" in item["sql"] for item in measurement["plans"])
    assert job_reads
    assert max(job_reads) <= 32
    filtered = [
        {
            key: node.get(key)
            for key in ("Index Name", "Index Cond", "Filter", "Rows Removed by Filter")
        }
        for item in measurement["plans"]
        for node in _plan_nodes(item["plan"][0]["Plan"])
        if node.get("Relation Name") == "jobs" and node.get("Rows Removed by Filter", 0) > 32
    ]
    assert not filtered, filtered


@pytest.mark.postgres
def test_batched_oldest_stream_skips_queue_when_no_job_ages_from_resume(
    migrated_postgres_engine,
):
    # Under default quota a release sets a resume earlier than every queued
    # base age; each resumed oldest stream then walked its whole tenant queue.
    queue_microbench._seed(migrated_postgres_engine, 4, 500)
    with migrated_postgres_engine.begin() as connection:
        now = connection.execute(select(func.clock_timestamp())).scalar_one()
        connection.execute(update(s.jobs).values(eligible_since=now))
        connection.execute(
            update(s.admission_counters)
            .where(s.admission_counters.c.scope_type == "TENANT")
            .values(eligible_resumed_at=now - timedelta(seconds=30))
        )
        connection.execute(text("ANALYZE jobs"))
        connection.execute(text("ANALYZE job_specs"))
    captured = []

    def capture(_connection, _cursor, statement, parameters, _context, _executemany):
        if "queue_parameters" in statement:
            captured.append((statement, parameters))

    event.listen(migrated_postgres_engine, "before_cursor_execute", capture)
    try:
        service = CoordinatorService(create_session_factory(migrated_postgres_engine))
        service.tick(service.acquire())
    finally:
        event.remove(migrated_postgres_engine, "before_cursor_execute", capture)
    assert captured
    with migrated_postgres_engine.connect() as connection:
        plans = [
            connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + statement, parameters
            ).scalar_one()[0]["Plan"]
            for statement, parameters in captured
        ]
    filtered = [
        (node.get("Index Name"), node["Rows Removed by Filter"], node.get("Filter"))
        for plan in plans
        for node in _plan_nodes(plan)
        if node.get("Relation Name") == "jobs" and node.get("Rows Removed by Filter", 0) > 32
    ]
    assert not filtered, filtered


def _plan_nodes(node):
    yield node
    for child in node.get("Plans", ()):
        yield from _plan_nodes(child)
