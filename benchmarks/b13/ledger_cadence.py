"""Measure committed B13 ledger cadence on an isolated PostgreSQL queue."""

import argparse
import json
import os
import platform
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

from sqlalchemy import create_engine, event, exists, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from benchmarks.b13.queue_microbench import _guard_url, _provenance
from nexa.coordinator.runtime import run
from nexa.coordinator.service import CoordinatorService
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.schema_guard import (
    SchemaCompatibility,
    inspect_schema_compatibility,
)


def measure(
    engine,
    *,
    duration_seconds: float,
    warmup_seconds: float,
    mutation: bool,
    leadership_timeout_seconds: float = 30,
):
    commits = []
    decisions = []
    accounting = []
    errors = []
    mutations = []
    stop = Event()
    leading = Event()

    class MeasuredSession(Session):
        def execute(self, statement, *args, **kwargs):
            if getattr(getattr(statement, "table", None), "name", None) == "fairness_ledgers":
                self.info["ledger_written"] = True
            return super().execute(statement, *args, **kwargs)

    @event.listens_for(MeasuredSession, "after_commit")
    def committed(session):
        if session.info.pop("ledger_written", False):
            commits.append((time.perf_counter(), datetime.now(UTC).isoformat()))

    class MeasuredCoordinator(CoordinatorService):
        def tick(self, epoch):
            started = time.perf_counter()
            try:
                decision = super().tick(epoch)
                decisions.append(
                    {
                        "at_monotonic": time.perf_counter(),
                        "duration_ms": (time.perf_counter() - started) * 1000,
                        "decision": type(decision).__name__,
                    }
                )
                return decision
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {str(exc)[:240]}")
                raise

        def account(self, epoch):
            started = time.perf_counter()
            try:
                boundary = super().account(epoch)
                leading.set()
                accounting.append(
                    {
                        "at_monotonic": time.perf_counter(),
                        "duration_ms": (time.perf_counter() - started) * 1000,
                        "skipped": boundary is None,
                    }
                )
                return boundary
            except Exception as exc:
                errors.append(f"accounting: {type(exc).__name__}: {str(exc)[:240]}")
                raise

    factory = sessionmaker(
        bind=engine, class_=MeasuredSession, autoflush=False, expire_on_commit=False
    )
    service = MeasuredCoordinator(factory)

    def keep_worker_fresh():
        while not stop.is_set():
            try:
                with engine.begin() as connection:
                    connection.execute(
                        update(s.workers).values(last_heartbeat_at=func.clock_timestamp())
                    )
            except Exception as exc:
                errors.append(f"heartbeat: {type(exc).__name__}: {str(exc)[:240]}")
                stop.set()
                return
            stop.wait(1)

    heartbeat = Thread(target=keep_worker_fresh, name="b13-fixture-heartbeat")
    coordinator = Thread(target=run, args=(service, stop), name="b13-measured-coordinator")

    def mutate_resources():
        if stop.wait(1):
            return
        try:
            with engine.connect() as connection:
                tenant_id, original = connection.execute(
                    select(s.tenant_policies.c.tenant_id, s.tenant_policies.c.cpu_limit_millis)
                    .where(
                        s.tenant_policies.c.is_current.is_(True),
                        ~exists(
                            select(s.allocations.c.allocation_id).where(
                                s.allocations.c.tenant_id == s.tenant_policies.c.tenant_id,
                                s.allocations.c.state != "RELEASED",
                            )
                        ),
                    )
                    .order_by(s.tenant_policies.c.tenant_id)
                    .limit(1)
                ).one()
            for value in (500, original):
                with engine.begin() as connection:
                    connection.execute(
                        update(s.tenant_policies)
                        .where(
                            s.tenant_policies.c.tenant_id == tenant_id,
                            s.tenant_policies.c.is_current.is_(True),
                        )
                        .values(cpu_limit_millis=value)
                    )
                mutations.append(
                    {"at_monotonic": time.perf_counter(), "tenant_id": str(tenant_id), "cpu": value}
                )
        except Exception as exc:
            errors.append(f"mutation: {type(exc).__name__}: {str(exc)[:240]}")

    mutator = None
    started = time.perf_counter()
    heartbeat.start()
    coordinator.start()
    try:
        # A previous process's unexpired lease delays takeover; measure from leadership.
        if not leading.wait(leadership_timeout_seconds):
            raise RuntimeError("coordinator did not acquire leadership")
        leadership_wait_ms = (time.perf_counter() - started) * 1000
        stop.wait(warmup_seconds)
        window_start = time.perf_counter()
        mutator = (
            Thread(target=mutate_resources, name="b13-resource-mutation") if mutation else None
        )
        if mutator:
            mutator.start()
        stop.wait(duration_seconds)
        window_end = time.perf_counter()
    finally:
        stop.set()
        coordinator.join()
        heartbeat.join()
        if mutator:
            mutator.join()
    measured = [item for item in commits if window_start <= item[0] <= window_end]
    gaps = [
        (right - left) * 1000
        for left, right in zip(
            [window_start, *(item[0] for item in measured)],
            [*(item[0] for item in measured), window_end],
            strict=True,
        )
    ]
    with engine.connect() as connection:
        ledger_count, min_boundary, max_boundary = connection.execute(
            select(
                func.count(),
                func.min(s.fairness_ledgers.c.accounted_through),
                func.max(s.fairness_ledgers.c.accounted_through),
            )
        ).one()
    return {
        "started_monotonic": started,
        "leadership_wait_ms": leadership_wait_ms,
        "window_seconds": window_end - window_start,
        "warmup_seconds": warmup_seconds,
        "commits": [
            {"offset_ms": (at - window_start) * 1000, "completed_at_utc": wall}
            for at, wall in measured
        ],
        "commit_gap_ms": gaps,
        "max_commit_gap_ms": max(gaps),
        "median_commit_gap_ms": statistics.median(gaps),
        "decisions": [
            {
                "offset_ms": (item["at_monotonic"] - window_start) * 1000,
                "duration_ms": item["duration_ms"],
                "decision": item["decision"],
            }
            for item in decisions
            if window_start <= item["at_monotonic"] <= window_end
        ],
        "accounting_heartbeats": [
            {
                "offset_ms": (item["at_monotonic"] - window_start) * 1000,
                "duration_ms": item["duration_ms"],
                "skipped": item["skipped"],
            }
            for item in accounting
            if window_start <= item["at_monotonic"] <= window_end
        ],
        "errors": errors,
        "mutations": [
            {
                "tenant_id": item["tenant_id"],
                "cpu": item["cpu"],
                "offset_ms": (item["at_monotonic"] - window_start) * 1000,
            }
            for item in mutations
        ],
        "ledger_count": ledger_count,
        "accounted_through_min": min_boundary.isoformat() if min_boundary else None,
        "accounted_through_max": max_boundary.isoformat() if max_boundary else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=12)
    parser.add_argument("--warmup-seconds", type=float, default=2)
    parser.add_argument("--mutation", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.seconds < 3 or args.warmup_seconds < 0:
        parser.error("measurement must be >=3 seconds and warmup nonnegative")
    url = _guard_url(os.environ.get("NEXA_TEST_DATABASE_URL", ""))
    engine = create_engine(url, pool_pre_ping=True)
    if inspect_schema_compatibility(engine) is not SchemaCompatibility.CURRENT:
        raise RuntimeError("Migrate the isolated benchmark database to current head")
    with engine.connect() as connection:
        version = connection.exec_driver_sql("SHOW server_version_num").scalar_one()
        jobs = connection.execute(select(func.count()).select_from(s.jobs)).scalar_one()
    if int(version) // 10000 != 17 or jobs < 100_000:
        raise RuntimeError("Cadence benchmark requires PostgreSQL 17 and >=100,000 Jobs")
    measurement = measure(
        engine,
        duration_seconds=args.seconds,
        warmup_seconds=args.warmup_seconds,
        mutation=args.mutation,
    )
    report = {
        "evidence_layer": (
            "P PostgreSQL coordinator with fixture-only heartbeat; no Docker execution"
        ),
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "environment": {
            "os": platform.platform(),
            "postgres_version_num": version,
            "database_host": url.host,
            "database_port": url.port,
            "database_name": url.database,
            "jobs_before": jobs,
        },
        "provenance": _provenance(),
        "measurement": measurement,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "commit_count": len(measurement["commits"]),
                "max_commit_gap_ms": measurement["max_commit_gap_ms"],
                "errors": measurement["errors"],
            }
        )
    )


if __name__ == "__main__":
    main()
