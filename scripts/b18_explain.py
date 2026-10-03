"""B18 query-plan evidence for adminListJobs and adminQueryFairness.

Seeds a disposable test database (≥100k jobs over 20 tenants; ≥200k ledger segments
over ≥60 days with long open segments) and prints EXPLAIN (ANALYZE, BUFFERS) for the
production statements. The database must pass the same guard as the PostgreSQL test
fixtures: postgresql+psycopg on loopback and a name starting with ``nexa_b05_test_``.

    PYTHONPATH=src:. python scripts/b18_explain.py seed
    PYTHONPATH=src:. python scripts/b18_explain.py explain-jobs > out
    PYTHONPATH=src:. python scripts/b18_explain.py explain-fairness > out
    PYTHONPATH=src:. python scripts/b18_explain.py write-cost

The URL is read from NEXA_TEST_DATABASE_URL and never printed.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select, text

from nexa.application.admin_queries import (
    _FAIRNESS_ALL,
    _FAIRNESS_TENANT,
    FAIRNESS_SETTINGS,
    admin_job_statement,
    fairness_parameters,
)
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.integration._factories import (
    CHECKSUM,
    seed_authority,
    seed_job,
    seed_tenant_graph,
    seed_worker,
)

TENANTS = 20
JOBS = 100_000
ALLOCATIONS_PER_TENANT = 10
SEGMENTS_PER_ALLOCATION = 1_050
LONG_OPEN_TENANTS = 5
BATCH = 5_000
STATES = [("SUCCEEDED", 80), ("FAILED", 6), ("CANCELLED", 5), ("QUEUED", 7), ("RUNNING", 2)]
REASONS = ["waiting_for_capacity", "waiting_for_quota", "waiting_for_worker"]


def _engine() -> Engine:
    url = os.environ.get("NEXA_TEST_DATABASE_URL", "")
    parsed = urlsplit(url)
    if parsed.scheme != "postgresql+psycopg" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        sys.exit("NEXA_TEST_DATABASE_URL must be postgresql+psycopg on loopback")
    if not parsed.path.removeprefix("/").startswith("nexa_b05_test_"):
        sys.exit("database name must start with nexa_b05_test_")
    if url == os.environ.get("NEXA_DATABASE_URL"):
        sys.exit("refusing to use NEXA_DATABASE_URL")
    engine = create_engine(url)
    with engine.connect() as connection:
        if int(connection.execute(text("SHOW server_version_num")).scalar_one()) // 10_000 != 17:
            sys.exit("PostgreSQL 17 is required")
    return engine


def _reset(engine: Engine) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%")
    )
    command.upgrade(config, "head")


def _job_rows(graph, rng: random.Random, now: datetime, count: int):
    states = [state for state, weight in STATES for _ in range(weight)]
    for _ in range(count):
        job_id = new_uuid7()
        state = rng.choice(states)
        created_at = now - timedelta(seconds=rng.uniform(0, 60 * 86_400))
        yield (
            {
                "job_id": job_id,
                "tenant_id": graph["tenant_id"],
                "submitter_user_id": graph["user_id"],
                "state": state,
                "desired_state": "RUNNING",
                "waiting_reason": rng.choice(REASONS) if state == "QUEUED" else None,
                "version": 1,
                "job_fence": 0,
                "event_sequence": 1,
                "checkpoint_sequence": 0,
                "retry_count": 0,
                "max_retries": 2,
                "base_priority": 1,
                "ready_sequence": int(job_id.int & 0x7FFFFFFF),
                "created_at": created_at,
                "updated_at": created_at,
            },
            {
                "job_id": job_id,
                "tenant_id": graph["tenant_id"],
                "canonical_spec": {"seed": 7},
                "spec_checksum": CHECKSUM,
                "template_id": graph["template_id"],
                "template_version": 1,
                "input_artifact_id": graph["artifact_id"],
                "cpu_millis": 1000,
                "memory_bytes": 1_073_741_824,
                "gpu_count": 0,
                "runtime_limit_seconds": 300,
                "checkpoint_interval_seconds": 30,
            },
            {"session_id": new_uuid7(), "tenant_id": graph["tenant_id"], "job_id": job_id},
        )


def _insert_jobs(connection, graph, rng, now, count) -> None:
    rows = list(_job_rows(graph, rng, now, count))
    for start in range(0, len(rows), BATCH):
        chunk = rows[start : start + BATCH]
        connection.execute(s.jobs.insert(), [row[0] for row in chunk])
        connection.execute(s.job_specs.insert(), [row[1] for row in chunk])
        connection.execute(s.logical_sessions.insert(), [row[2] for row in chunk])


def seed(engine: Engine) -> None:
    rng = random.Random(18)
    _reset(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        worker = seed_worker(connection, label="b18-perf")
        policy_version = connection.execute(
            select(s.policy_versions.c.policy_version).where(
                s.policy_versions.c.is_current.is_(True)
            )
        ).scalar_one()
        graphs = [seed_tenant_graph(connection, label=f"b18-perf-{i:02d}") for i in range(TENANTS)]
    for graph in graphs:
        with engine.begin() as connection:
            _insert_jobs(connection, graph, rng, now, JOBS // TENANTS)
    segments = 0
    for tenant_index, graph in enumerate(graphs):
        with engine.begin() as connection:
            rows = []
            for allocation_index in range(ALLOCATIONS_PER_TENANT):
                job = seed_job(connection, graph, state="RUNNING")
                allocation_id = seed_authority(connection, graph, job, worker)["allocation_id"]
                long_open = tenant_index < LONG_OPEN_TENANTS and allocation_index == 0
                stop = now - timedelta(days=rng.uniform(30, 45)) if long_open else now
                cursor = now - timedelta(days=61, seconds=rng.uniform(0, 3600))
                share = Decimal(rng.choice(["0.0625", "0.125", "0.25", "0.5"]))
                weight = Decimal(rng.choice(["1", "2", "0.5"]))
                for _ in range(SEGMENTS_PER_ALLOCATION):
                    end = cursor + timedelta(seconds=rng.uniform(60, 9_000))
                    if end >= stop:
                        break
                    rows.append(
                        {
                            "segment_id": new_uuid7(),
                            "allocation_id": allocation_id,
                            "tenant_id": graph["tenant_id"],
                            "started_at": cursor,
                            "ended_at": end,
                            "dominant_share": share,
                            "weight": weight,
                            "charged_amount": Decimal(0),
                            "policy_version": policy_version,
                        }
                    )
                    cursor = end + timedelta(seconds=rng.uniform(0, 1_200))
                if allocation_index < 2 or long_open:
                    rows.append(
                        {
                            "segment_id": new_uuid7(),
                            "allocation_id": allocation_id,
                            "tenant_id": graph["tenant_id"],
                            "started_at": min(cursor, now - timedelta(seconds=30)),
                            "ended_at": None,
                            "dominant_share": share,
                            "weight": weight,
                            "charged_amount": Decimal(0),
                            "policy_version": policy_version,
                        }
                    )
            for start in range(0, len(rows), BATCH):
                connection.execute(
                    s.allocation_ledger_segments.insert(), rows[start : start + BATCH]
                )
            segments += len(rows)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("VACUUM ANALYZE"))
    print(f"seeded tenants={TENANTS} jobs={JOBS + TENANTS * ALLOCATIONS_PER_TENANT}")
    print(f"seeded ledger_segments={segments}")
    _summary(engine)


def _summary(engine: Engine) -> None:
    ledger = s.allocation_ledger_segments
    with engine.connect() as connection:
        jobs = connection.execute(select(func.count()).select_from(s.jobs)).scalar_one()
        tenants = connection.execute(
            select(func.count(func.distinct(s.jobs.c.tenant_id)))
        ).scalar_one()
        first, last, total, open_count = connection.execute(
            select(
                func.min(ledger.c.started_at),
                func.max(func.coalesce(ledger.c.ended_at, func.now())),
                func.count(),
                func.count().filter(ledger.c.ended_at.is_(None)),
            )
        ).one()
        oldest_open = connection.execute(
            select(func.min(ledger.c.started_at)).where(ledger.c.ended_at.is_(None))
        ).scalar_one()
        version = connection.execute(text("SELECT version()")).scalar_one()
        indexes = connection.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE tablename IN"
                " ('jobs', 'allocation_ledger_segments') ORDER BY tablename, indexname"
            )
        ).scalars()
        print(f"server {version}")
        print(f"jobs={jobs} tenants_with_jobs={tenants}")
        print(f"ledger_segments={total} open={open_count} span_days={(last - first).days}")
        print(f"oldest_open_segment_age_days={(datetime.now(UTC) - oldest_open).days}")
        print("indexes: " + ", ".join(indexes))


def _explain(connection, title: str, sql: str, params: dict) -> None:
    rows = connection.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS) " + sql, params).scalars()
    print(f"=== {title}")
    for line in rows:
        print(line)
    print()


def _compiled(engine: Engine, statement) -> tuple[str, dict]:
    compiled = statement.compile(dialect=engine.dialect)
    return str(compiled), dict(compiled.params)


def explain_jobs(engine: Engine) -> None:
    _summary(engine)
    print()
    with engine.connect() as connection:
        tenant_id, user_id = connection.execute(
            select(s.jobs.c.tenant_id, s.jobs.c.submitter_user_id).limit(1)
        ).one()
        first_page = connection.execute(
            admin_job_statement(
                tenant_id=None,
                user_id=None,
                state=None,
                waiting_reason=None,
                created_after=None,
                after=None,
                limit=51,
            )
        ).all()
        after = (first_page[49].created_at, first_page[49].job_id)
        week = datetime.now(UTC) - timedelta(days=7)
        cases = [
            ("no filter", {}),
            ("tenant_id", {"tenant_id": tenant_id}),
            ("state=QUEUED", {"state": "QUEUED"}),
            ("waiting_reason=waiting_for_quota", {"waiting_reason": "waiting_for_quota"}),
            ("user_id", {"user_id": user_id}),
            ("created_after=now-7d", {"created_after": week}),
            ("page 2 (no filter, keyset after row 50)", {"after": after}),
            ("page 2 with tenant_id", {"tenant_id": tenant_id, "after": after}),
        ]
        for title, overrides in cases:
            arguments = {
                "tenant_id": None,
                "user_id": None,
                "state": None,
                "waiting_reason": None,
                "created_after": None,
                "after": None,
                "limit": 51,
                **overrides,
            }
            sql, params = _compiled(engine, admin_job_statement(**arguments))
            _explain(connection, f"adminListJobs {title}", sql, params)
        connection.rollback()


def explain_fairness(engine: Engine) -> None:
    _summary(engine)
    print()
    with engine.connect() as connection:
        tenant_id = connection.execute(
            select(s.allocation_ledger_segments.c.tenant_id)
            .where(s.allocation_ledger_segments.c.ended_at.is_(None))
            .order_by(s.allocation_ledger_segments.c.started_at)
            .limit(1)
        ).scalar_one()
        connection.execute(FAIRNESS_SETTINGS)
        now = datetime.now(UTC).replace(microsecond=0)
        windows = [("1h", timedelta(hours=1), 300), ("24h", timedelta(hours=24), 3600)]
        windows.append(("31d", timedelta(days=31), 86_400))
        windows.append(("31d ending 29d ago", timedelta(days=31), 86_400))
        for title, span, bucket in windows:
            to_at = now - timedelta(days=29) if "ago" in title else now
            from_at = to_at - span
            base = fairness_parameters(from_at=from_at, to_at=to_at, bucket_seconds=bucket)
            for scope, statement, params in (
                ("all tenants", _FAIRNESS_ALL, base),
                ("one tenant", _FAIRNESS_TENANT, {**base, "tenant_id": tenant_id}),
            ):
                sql, compiled = _compiled(engine, statement)
                for key, value in params.items():
                    compiled[key] = value
                started = time.perf_counter()
                count = len(connection.exec_driver_sql(sql, compiled).all())
                elapsed = (time.perf_counter() - started) * 1000
                print(f"# {title} bucket={bucket}s {scope}: rows={count} wall_ms={elapsed:.1f}")
                _explain(connection, f"adminQueryFairness {title} {scope}", sql, compiled)
        # Comparison only: the 31-day all-tenant window with sequential scans disabled,
        # to show the planner's seq scan choice there is the cheaper one.
        sql, compiled = _compiled(engine, _FAIRNESS_ALL)
        span = timedelta(days=31)
        compiled.update(fairness_parameters(from_at=now - span, to_at=now, bucket_seconds=86_400))
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        started = time.perf_counter()
        count = len(connection.exec_driver_sql(sql, compiled).all())
        elapsed = (time.perf_counter() - started) * 1000
        print(f"# 31d all tenants enable_seqscan=off: rows={count} wall_ms={elapsed:.1f}")
        _explain(
            connection, "adminQueryFairness 31d all tenants, enable_seqscan=off", sql, compiled
        )
        connection.rollback()


def write_cost(engine: Engine) -> None:
    """ACC-29 write cost: insert 5000 jobs (+specs, sessions) and roll back, 5 runs."""
    rng = random.Random(29)
    with engine.connect() as connection:
        graph_row = connection.execute(
            select(
                s.job_specs.c.tenant_id,
                s.jobs.c.submitter_user_id,
                s.job_specs.c.template_id,
                s.job_specs.c.input_artifact_id,
            )
            .join(s.jobs, s.jobs.c.job_id == s.job_specs.c.job_id)
            .limit(1)
        ).one()
    graph = {
        "tenant_id": graph_row[0],
        "user_id": graph_row[1],
        "template_id": graph_row[2],
        "artifact_id": graph_row[3],
    }
    timings = []
    for _ in range(5):
        with engine.connect() as connection:
            transaction = connection.begin()
            started = time.perf_counter()
            _insert_jobs(connection, graph, rng, datetime.now(UTC), 5_000)
            timings.append((time.perf_counter() - started) * 1000)
            transaction.rollback()
    timings.sort()
    print(f"insert 5000 jobs+specs+sessions ms: runs={[round(t, 1) for t in timings]}")
    print(f"median_ms={timings[2]:.1f}")
    _ledger_write_cost(engine)


def _ledger_write_cost(engine: Engine) -> None:
    """Ledger writes the period index adds to: the per-tick charge and a boundary rebuild."""
    segments = s.allocation_ledger_segments
    ticks, boundaries = [], []
    for _ in range(5):
        with engine.connect() as connection:
            transaction = connection.begin()
            open_rows = (
                connection.execute(select(segments).where(segments.c.ended_at.is_(None)))
                .mappings()
                .all()
            )
            started = time.perf_counter()
            for _tick in range(200):
                connection.execute(
                    segments.update()
                    .where(segments.c.ended_at.is_(None))
                    .values(charged_amount=Decimal(_tick) / 1000)
                )
            ticks.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            boundary = datetime.now(UTC)
            for step in range(200):
                at = boundary + timedelta(seconds=step + 1)
                connection.execute(
                    segments.update().where(segments.c.ended_at.is_(None)).values(ended_at=at)
                )
                connection.execute(
                    segments.insert(),
                    [
                        {
                            **{key: row[key] for key in row if key != "segment_id"},
                            "segment_id": new_uuid7(),
                            "started_at": at,
                            "ended_at": None,
                            "charged_amount": Decimal(0),
                        }
                        for row in open_rows
                    ],
                )
            boundaries.append((time.perf_counter() - started) * 1000)
            transaction.rollback()
    ticks.sort()
    boundaries.sort()
    print(f"open_segments={len(open_rows)}")
    print(f"200 charge ticks ms: runs={[round(t, 1) for t in ticks]} median_ms={ticks[2]:.1f}")
    print(
        f"200 boundary rebuilds (close+insert) ms: runs={[round(t, 1) for t in boundaries]} "
        f"median_ms={boundaries[2]:.1f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["seed", "summary", "explain-jobs", "explain-fairness", "write-cost"]
    )
    action = parser.parse_args().action
    engine = _engine()
    try:
        {
            "seed": seed,
            "summary": _summary,
            "explain-jobs": explain_jobs,
            "explain-fairness": explain_fairness,
            "write-cost": write_cost,
        }[action](engine)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
