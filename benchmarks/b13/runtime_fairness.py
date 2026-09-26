"""Real-time PostgreSQL scheduler fairness trace with fixture-only completions."""

import argparse
import json
import os
import platform
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.orm import Session

from benchmarks.b13.queue_microbench import _guard_url, _provenance, _seed
from nexa.coordinator.accounting import account_locked, rebase_locked
from nexa.coordinator.service import CoordinatorService
from nexa.domain.scheduling import Dispatch, NoDecision
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.locking import clock_timestamp
from nexa.infrastructure.persistence.schema_guard import (
    SchemaCompatibility,
    inspect_schema_compatibility,
)


def weighted_jain(resource_time: dict[str, Decimal], weights: dict[str, Decimal]) -> Decimal:
    if resource_time.keys() != weights.keys() or not resource_time:
        raise ValueError("The declared cohort must have resource time and weights")
    values = [resource_time[tenant] / weights[tenant] for tenant in sorted(resource_time)]
    denominator = Decimal(len(values)) * sum((value * value for value in values), Decimal(0))
    if denominator == 0:
        raise ValueError("The measurement window has no allocated resource time")
    return sum(values, Decimal(0)) ** 2 / denominator


def window_resource_time(allocations, start, end, capacity_cpu_millis: int):
    """CPU-dominant resource-time for the fixed homogeneous CPU cohort."""
    measured = {}
    for allocation in allocations:
        left = max(start, allocation["held_at"])
        right = min(end, allocation["released_at"] or end)
        if right <= left:
            continue
        interval = right - left
        microseconds = (
            interval.days * 86_400_000_000 + interval.seconds * 1_000_000 + interval.microseconds
        )
        tenant_id = str(allocation["tenant_id"])
        measured[tenant_id] = measured.get(tenant_id, Decimal(0)) + (
            Decimal(allocation["cpu_millis"])
            * Decimal(microseconds)
            / Decimal(capacity_cpu_millis * 1_000_000)
        )
    return measured


def complete_fixture_allocation(engine, service, allocation_id):
    """Simulate a finite CPU completion; this is not a worker callback."""
    with Session(engine) as session, session.begin():
        service._locks(session)
        allocation = (
            session.execute(
                select(s.allocations)
                .where(s.allocations.c.allocation_id == allocation_id)
                .with_for_update()
            )
            .mappings()
            .one()
        )
        if allocation["state"] == "RELEASED":
            raise ValueError("Fixture allocation was already released")
        now = clock_timestamp(session)
        account_locked(session, now)
        session.execute(
            update(s.allocations)
            .where(s.allocations.c.allocation_id == allocation_id)
            .values(state="RELEASED", released_at=now, release_reason="VERIFIED_CLEANUP")
        )
        session.execute(
            update(s.attempts)
            .where(s.attempts.c.attempt_id == allocation["attempt_id"])
            .values(state="SUCCEEDED", ended_at=now, updated_at=now)
        )
        session.execute(
            update(s.attempt_leases)
            .where(s.attempt_leases.c.attempt_id == allocation["attempt_id"])
            .values(revoked_at=now, revoke_reason="benchmark_fixture_completion")
        )
        session.execute(
            update(s.attempt_authority_grants)
            .where(s.attempt_authority_grants.c.attempt_id == allocation["attempt_id"])
            .values(ended_at=now)
        )
        job = (
            session.execute(select(s.jobs).where(s.jobs.c.job_id == allocation["job_id"]))
            .mappings()
            .one()
        )
        session.execute(
            update(s.jobs)
            .where(s.jobs.c.job_id == allocation["job_id"])
            .values(state="SUCCEEDED", updated_at=now)
        )
        for scope, scope_id in (
            ("GLOBAL", "global"),
            ("TENANT", str(allocation["tenant_id"])),
            ("USER", f"{allocation['tenant_id']}:{job['submitter_user_id']}"),
        ):
            session.execute(
                update(s.admission_counters)
                .where(
                    s.admission_counters.c.scope_type == scope,
                    s.admission_counters.c.scope_id == scope_id,
                )
                .values(
                    active_attempts=s.admission_counters.c.active_attempts - 1,
                    outstanding=s.admission_counters.c.outstanding - 1,
                    version=s.admission_counters.c.version + 1,
                    updated_at=now,
                )
            )
        rebase_locked(session, now)


def prepare_cohort(
    engine, *, jobs_per_tenant: int, cpu_quota_millis: int = 6000, tenant_weights=(1, 2, 4)
) -> dict[str, Decimal]:
    """Seed three equal CPU profiles with the given weights, quota and continuous demand."""
    if jobs_per_tenant < 30:
        raise ValueError("At least 30 jobs per tenant are required for the smoke window")
    _seed(engine, 3, jobs_per_tenant)
    weights = {}
    with engine.begin() as connection:
        tenant_ids = (
            connection.execute(
                select(s.tenant_policies.c.tenant_id)
                .where(s.tenant_policies.c.is_current.is_(True))
                .order_by(s.tenant_policies.c.tenant_id)
            )
            .scalars()
            .all()
        )
        if len(tenant_ids) != 3:
            raise RuntimeError("The isolated fairness fixture must contain three tenants")
        for tenant_id, weight in zip(tenant_ids, tenant_weights, strict=True):
            connection.execute(
                update(s.tenant_policies)
                .where(s.tenant_policies.c.tenant_id == tenant_id)
                .values(
                    weight=Decimal(weight),
                    cpu_limit_millis=cpu_quota_millis,
                    tenant_active_limit=2,
                    user_active_limit=2,
                )
            )
            weights[str(tenant_id)] = Decimal(weight)
        connection.execute(
            update(s.worker_inventories).values(
                allocatable_cpu_millis=3000, allocatable_gpu_count=0
            )
        )
        connection.execute(text("ANALYZE jobs"))
    return weights


def _queued_by_tenant(engine, weights):
    with engine.connect() as connection:
        counts = {
            str(tenant_id): count
            for tenant_id, count in connection.execute(
                select(s.jobs.c.tenant_id, func.count())
                .where(s.jobs.c.state == "QUEUED")
                .group_by(s.jobs.c.tenant_id)
            ).all()
        }
    return {tenant: counts.get(tenant, 0) for tenant in weights}


_TICK_PROBE = text(
    """
    SELECT
        (SELECT count(*) FROM queue_eligibility_events WHERE completed_at IS NULL)
            AS pending_eligibility_events,
        (SELECT coalesce(sum(cpu_millis), 0) FROM allocations WHERE state <> 'RELEASED')
            AS held_cpu_millis,
        (
            SELECT count(*) FROM tenant_policies p
            CROSS JOIN LATERAL (
                SELECT coalesce(sum(a.cpu_millis), 0) AS cpu, count(*) AS active
                FROM allocations a WHERE a.tenant_id = p.tenant_id AND a.state <> 'RELEASED'
            ) held
            WHERE p.is_current AND p.cpu_limit_millis - held.cpu >= 1000
              AND held.active < p.tenant_active_limit
              AND EXISTS (SELECT 1 FROM jobs j WHERE j.tenant_id = p.tenant_id
                          AND j.state = 'QUEUED' AND j.desired_state = 'RUNNING'
                          AND j.recovery_intent IS NULL)
        ) AS tenants_with_fitting_demand
    """
)
_JOB_WRITES = text("SELECT n_tup_upd, n_tup_ins FROM pg_stat_user_tables WHERE relname = 'jobs'")
_EVENT_COUNT = text("SELECT count(*) FROM queue_eligibility_events")


def _job_write_counters(engine):
    time.sleep(1.5)  # Cumulative statistics are flushed by backends at most once per second.
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_stat_clear_snapshot()"))
        updates, inserts = connection.execute(_JOB_WRITES).one()
        events = connection.execute(_EVENT_COUNT).scalar_one()
    return {"jobs_n_tup_upd": updates, "jobs_n_tup_ins": inserts, "eligibility_events": events}


def measure(
    engine,
    *,
    weights: dict[str, Decimal],
    warmup_seconds: float,
    window_seconds: float,
    windows: int,
    execution_seconds: float,
):
    if warmup_seconds < 0 or window_seconds <= 0 or windows < 1 or execution_seconds <= 0:
        raise ValueError("Invalid real-time measurement window")
    service = CoordinatorService(create_session_factory(engine))
    epoch = service.acquire()
    if epoch is None:
        raise RuntimeError("The isolated fairness fixture has another live coordinator")
    queued_before = _queued_by_tenant(engine, weights)
    writes_before = _job_write_counters(engine)
    idle_fit_ticks = []
    max_pending_events = 0
    completions = 0
    active = {}
    trace = []
    errors = []
    decision_counts = {}
    no_decision_reasons = {}
    max_held_cpu = 0
    started_db = None
    started = time.perf_counter()
    total_seconds = warmup_seconds + window_seconds * windows
    deadline = started + total_seconds
    last_heartbeat = float("-inf")
    last_renew = float("-inf")
    while time.perf_counter() < deadline:
        loop_started = time.perf_counter()
        try:
            if started_db is None:
                with engine.connect() as connection:
                    started_db = connection.execute(select(func.clock_timestamp())).scalar_one()
            for allocation_id, due in list(active.items()):
                if due <= loop_started:
                    complete_fixture_allocation(engine, service, allocation_id)
                    del active[allocation_id]
                    completions += 1
                    trace.append(
                        {
                            "event": "fixture_completion",
                            "allocation_id": str(allocation_id),
                            "offset_ms": (time.perf_counter() - started) * 1000,
                        }
                    )
            if loop_started - last_heartbeat >= 1:
                with engine.begin() as connection:
                    connection.execute(
                        update(s.workers).values(last_heartbeat_at=func.clock_timestamp())
                    )
                last_heartbeat = loop_started
            if loop_started - last_renew >= 5:
                if not service.renew(epoch):
                    raise RuntimeError("Coordinator leadership was lost")
                last_renew = loop_started
            decision = service.tick(epoch)
            name = type(decision).__name__
            decision_counts[name] = decision_counts.get(name, 0) + 1
            if isinstance(decision, NoDecision):
                no_decision_reasons[decision.reason] = (
                    no_decision_reasons.get(decision.reason, 0) + 1
                )
            with engine.connect() as connection:
                probe = connection.execute(_TICK_PROBE).mappings().one()
            max_pending_events = max(max_pending_events, probe["pending_eligibility_events"])
            # Capacity left idle although a tenant has a job that fits its headroom.
            if (
                name == "NoDecision"
                and 3000 - probe["held_cpu_millis"] >= 1000
                and probe["tenants_with_fitting_demand"]
            ):
                idle_fit_ticks.append(
                    {
                        "offset_ms": (time.perf_counter() - started) * 1000,
                        "reason": decision.reason,
                        **{key: int(value) for key, value in probe.items()},
                    }
                )
            if isinstance(decision, Dispatch):
                with engine.connect() as connection:
                    allocation_id = connection.execute(
                        select(s.allocations.c.allocation_id).where(
                            s.allocations.c.job_id == decision.job_id
                        )
                    ).scalar_one()
                active[allocation_id] = time.perf_counter() + execution_seconds
                max_held_cpu = max(max_held_cpu, len(active) * 1000)
                trace.append(
                    {
                        "event": "dispatch",
                        "job_id": decision.job_id,
                        "allocation_id": str(allocation_id),
                        "offset_ms": (time.perf_counter() - started) * 1000,
                    }
                )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:240]}")
            break
        time.sleep(
            max(0, min(0.25 - (time.perf_counter() - loop_started), deadline - time.perf_counter()))
        )
    if started_db is None:
        raise RuntimeError("No measurement tick was executed")
    with engine.connect() as connection:
        allocations = (
            connection.execute(
                select(
                    s.allocations.c.allocation_id,
                    s.allocations.c.tenant_id,
                    s.allocations.c.cpu_millis,
                    s.allocations.c.memory_bytes,
                    s.allocations.c.held_at,
                    s.allocations.c.released_at,
                )
            )
            .mappings()
            .all()
        )
    queued_after = _queued_by_tenant(engine, weights)
    writes_after = _job_write_counters(engine)
    decisions = sum(decision_counts.values())
    job_updates = writes_after["jobs_n_tup_upd"] - writes_before["jobs_n_tup_upd"]
    measured_windows = []
    for index in range(windows):
        left = started_db + timedelta(seconds=warmup_seconds + index * window_seconds)
        right = left + timedelta(seconds=window_seconds)
        resource_time = window_resource_time(allocations, left, right, 3000)
        cohort = {tenant: resource_time.get(tenant, Decimal(0)) for tenant in weights}
        measured_windows.append(
            {
                "index": index + 1,
                "start_db_utc": left.isoformat(),
                "end_db_utc": right.isoformat(),
                "dominant_resource_time": {tenant: str(value) for tenant, value in cohort.items()},
                "weighted_jain": str(weighted_jain(cohort, weights))
                if any(cohort.values())
                else None,
            }
        )
    return {
        "started_db_utc": started_db.isoformat(),
        "warmup_seconds": warmup_seconds,
        "window_seconds": window_seconds,
        "execution_seconds": execution_seconds,
        "weights": {tenant: str(value) for tenant, value in weights.items()},
        "queued_before": queued_before,
        "queued_after": queued_after,
        "decision_counts": decision_counts,
        "max_held_cpu_millis": max_held_cpu,
        "fixture_completions": completions,
        "no_decision_reasons": no_decision_reasons,
        "idle_fit_ticks": idle_fit_ticks,
        "max_pending_eligibility_events": max_pending_events,
        "eligibility_events_created": writes_after["eligibility_events"]
        - writes_before["eligibility_events"],
        "jobs_row_updates": job_updates,
        "jobs_row_updates_per_tick": job_updates / decisions if decisions else None,
        "jobs_row_updates_per_dispatch_or_completion": (
            job_updates / (decision_counts.get("Dispatch", 0) + completions)
            if decision_counts.get("Dispatch", 0) + completions
            else None
        ),
        "allocations": [dict(row) for row in allocations],
        "trace": trace,
        "windows": measured_windows,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-per-tenant", type=int, default=1000)
    parser.add_argument("--warmup-seconds", type=float, default=10)
    parser.add_argument("--window-seconds", type=float, default=30)
    parser.add_argument("--windows", type=int, default=3)
    parser.add_argument("--execution-seconds", type=float, default=1)
    parser.add_argument("--tenant-cpu-quota-millis", type=int, default=6000)
    parser.add_argument("--weights", default="1,2,4", help="Three comma-separated weights")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    url = _guard_url(os.environ.get("NEXA_TEST_DATABASE_URL", ""))
    engine = create_engine(url, pool_pre_ping=True)
    if inspect_schema_compatibility(engine) is not SchemaCompatibility.CURRENT:
        raise RuntimeError("Migrate the isolated benchmark database to current head")
    with engine.connect() as connection:
        version = connection.exec_driver_sql("SHOW server_version_num").scalar_one()
    if int(version) // 10000 != 17:
        raise RuntimeError("This benchmark requires PostgreSQL 17")
    weights = prepare_cohort(
        engine,
        jobs_per_tenant=args.jobs_per_tenant,
        cpu_quota_millis=args.tenant_cpu_quota_millis,
        tenant_weights=tuple(int(value) for value in args.weights.split(",")),
    )
    measurement = measure(
        engine,
        weights=weights,
        warmup_seconds=args.warmup_seconds,
        window_seconds=args.window_seconds,
        windows=args.windows,
        execution_seconds=args.execution_seconds,
    )
    report = {
        "evidence_layer": (
            "P PostgreSQL production coordinator, direct-DB fixture completions; "
            "no API or Docker execution claim"
        ),
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "environment": {
            "os": platform.platform(),
            "postgres_version_num": version,
            "database_host": url.host,
            "database_port": url.port,
            "database_name": url.database,
        },
        "fixture": {
            "allocatable_cpu_millis": 3000,
            "allocatable_memory_bytes": 12 * 1024**3,
            "tenant_cpu_quota_millis": args.tenant_cpu_quota_millis,
            "weights": args.weights,
            "tenant_active_limit": 2,
            "user_active_limit": 2,
            "job_cpu_millis": 1000,
            "job_memory_bytes": 1024**3,
            "jobs_per_tenant": args.jobs_per_tenant,
        },
        "provenance": _provenance(),
        "measurement": measurement,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {
                "weighted_jain": [row["weighted_jain"] for row in measurement["windows"]],
                "queued_after": measurement["queued_after"],
                "decision_counts": measurement["decision_counts"],
                "no_decision_reasons": measurement["no_decision_reasons"],
                "idle_fit_reasons": sorted(
                    {tick["reason"] for tick in measurement["idle_fit_ticks"]}
                ),
                "idle_fit_ticks": len(measurement["idle_fit_ticks"]),
                "eligibility_events_created": measurement["eligibility_events_created"],
                "jobs_row_updates_per_dispatch_or_completion": measurement[
                    "jobs_row_updates_per_dispatch_or_completion"
                ],
                "errors": measurement["errors"],
            }
        )
    )
    if measurement["errors"]:
        raise RuntimeError("Real-time fairness benchmark recorded an error")


if __name__ == "__main__":
    main()
