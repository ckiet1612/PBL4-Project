"""Isolated direct-DB queue microbenchmark, never API acceptance evidence."""

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, event, exists, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from nexa.coordinator.accounting import account_locked, rebase_locked
from nexa.coordinator.service import CoordinatorService
from nexa.domain.scheduling import Dispatch
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema_guard import (
    SchemaCompatibility,
    inspect_schema_compatibility,
)
from tests.integration._factories import CHECKSUM, seed_tenant_graph, seed_worker
from tests.integration.test_coordinator_b11 import seed_dispatchable


def _guard_url(raw: str):
    url = make_url(raw)
    if (
        url.drivername != "postgresql+psycopg"
        or url.host not in {"127.0.0.1", "localhost", "::1"}
        or not (url.database or "").startswith("nexa_b05_test_")
        or raw == os.environ.get("NEXA_DATABASE_URL")
    ):
        raise ValueError("Use an isolated loopback PostgreSQL nexa_b05_test_ database")
    return url


def _source_tree_sha256(source_paths):
    """Hash every source file path and content, so a tree copy without .git is identified."""
    digest = hashlib.sha256()
    files = sorted(
        path
        for root in source_paths
        for path in Path(root).rglob("*.py")
        if "__pycache__" not in path.parts
    )
    for path in files:
        digest.update(f"{path}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
    return digest.hexdigest()


def _provenance():
    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    source_paths = ("src/", "migrations/", "tests/", "benchmarks/b13/")
    tree = _source_tree_sha256(source_paths)
    if not Path(".git").exists():
        return {"head": None, "git": "unavailable: tree copy", "source_tree_sha256": tree}
    diff = subprocess.check_output(
        ["git", "diff", "--binary", "--", *source_paths, "pyproject.toml", "uv.lock"]
    )
    untracked = git("ls-files", "--others", "--exclude-standard").splitlines()
    hashes = {
        name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
        for name in untracked
        if name.startswith(source_paths) and name.endswith(".py") and Path(name).is_file()
    }
    return {
        "head": git("rev-parse", "HEAD"),
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "untracked_sha256": hashes,
        "source_tree_sha256": tree,
    }


def _seed(engine, tenant_count: int, jobs_per_tenant: int):
    with engine.connect() as connection:
        if connection.execute(select(func.count()).select_from(s.jobs)).scalar_one():
            raise ValueError("Refusing to seed a nonempty jobs table")

    first, _, _ = seed_dispatchable(engine, count=0)
    graphs = [first]
    with engine.begin() as connection:
        for index in range(1, tenant_count):
            graph = seed_tenant_graph(connection, label=f"b13-micro-{index}")
            graphs.append(graph)
            connection.execute(
                s.tenant_policies.insert().values(
                    tenant_id=graph["tenant_id"],
                    version=1,
                    weight=Decimal(1),
                    cpu_limit_millis=6000,
                    memory_limit_bytes=12 * 1024**3,
                    gpu_limit=0,
                    outstanding_limit=max(2000, jobs_per_tenant),
                    user_outstanding_limit=max(2000, jobs_per_tenant),
                    tenant_active_limit=2,
                    user_active_limit=1,
                    tenant_rate_per_second=Decimal(5),
                    tenant_rate_burst=Decimal(20),
                    user_rate_per_second=Decimal(2),
                    user_rate_burst=Decimal(10),
                    is_current=True,
                )
            )
            for scope, scope_id in (
                ("TENANT", str(graph["tenant_id"])),
                ("USER", f"{graph['tenant_id']}:{graph['user_id']}"),
            ):
                connection.execute(
                    s.admission_counters.insert().values(
                        scope_type=scope, scope_id=scope_id, outstanding=jobs_per_tenant
                    )
                )
        requirements = connection.execute(
            select(s.template_versions.c.capability_requirements).where(
                s.template_versions.c.template_id == first["template_id"]
            )
        ).scalar_one()
        connection.execute(update(s.template_versions).values(capability_requirements=requirements))
        connection.execute(
            update(s.tenant_policies)
            .where(s.tenant_policies.c.tenant_id == first["tenant_id"])
            .values(
                outstanding_limit=max(2000, jobs_per_tenant),
                user_outstanding_limit=max(2000, jobs_per_tenant),
                tenant_active_limit=2,
                user_active_limit=1,
            )
        )
        connection.execute(
            update(s.policy_versions).values(
                global_outstanding_limit=tenant_count * jobs_per_tenant
            )
        )

    for tenant_index, graph in enumerate(graphs):
        with engine.begin() as connection:
            now = connection.execute(select(func.clock_timestamp())).scalar_one()
            ids = [new_uuid7() for _ in range(jobs_per_tenant)]
            connection.execute(
                s.jobs.insert(),
                [
                    {
                        "job_id": job_id,
                        "tenant_id": graph["tenant_id"],
                        "submitter_user_id": graph["user_id"],
                        "state": "QUEUED",
                        "desired_state": "RUNNING",
                        "ready_sequence": tenant_index * jobs_per_tenant + index,
                        "eligible_since": now,
                    }
                    for index, job_id in enumerate(ids)
                ],
            )
            connection.execute(
                s.job_specs.insert(),
                [
                    {
                        "job_id": job_id,
                        "tenant_id": graph["tenant_id"],
                        "canonical_spec": {"benchmark": "direct-db"},
                        "spec_checksum": CHECKSUM,
                        "template_id": graph["template_id"],
                        "template_version": 1,
                        "input_artifact_id": graph["artifact_id"],
                        "cpu_millis": 1000,
                        "memory_bytes": 1024**3,
                        "gpu_count": 0,
                        "runtime_limit_seconds": 300,
                        "checkpoint_interval_seconds": 30,
                    }
                    for job_id in ids
                ],
            )
            connection.execute(
                s.logical_sessions.insert(),
                [
                    {
                        "session_id": new_uuid7(),
                        "tenant_id": graph["tenant_id"],
                        "job_id": job_id,
                    }
                    for job_id in ids
                ],
            )
            if tenant_index == 0:
                connection.execute(
                    update(s.admission_counters)
                    .where(s.admission_counters.c.scope_type.in_(["TENANT", "USER"]))
                    .values(outstanding=jobs_per_tenant)
                )
        if (tenant_index + 1) % 10 == 0:
            print(f"seeded {tenant_index + 1}/{tenant_count} tenants", flush=True)
    with engine.begin() as connection:
        connection.execute(
            update(s.admission_counters)
            .where(s.admission_counters.c.scope_type == "GLOBAL")
            .values(outstanding=tenant_count * jobs_per_tenant)
        )
        # Reset the measurement cohort's age after the direct seed completes.
        connection.execute(update(s.jobs).values(eligible_since=func.clock_timestamp()))
        connection.execute(update(s.workers).values(last_heartbeat_at=func.clock_timestamp()))
        connection.execute(text("ANALYZE jobs"))
        connection.execute(text("ANALYZE job_specs"))


def _prepare_api_worker(engine):
    with engine.begin() as connection:
        if connection.execute(select(func.count()).select_from(s.workers)).scalar_one():
            raise ValueError("Refusing to replace an existing worker")
        template = (
            connection.execute(
                select(s.template_versions).where(
                    s.template_versions.c.template_id == "cpu-iterative",
                    s.template_versions.c.version == 1,
                )
            )
            .mappings()
            .one()
        )
        worker = seed_worker(connection, label="b13-api-fixture")
        connection.execute(
            update(s.worker_inventories)
            .where(s.worker_inventories.c.worker_id == worker["worker_id"])
            .values(
                architecture="linux/amd64",
                allocatable_gpu_count=0,
                workload_capabilities={
                    "adapters": [{"adapter_id": "cpu.iterative", "adapter_version": "1.0.0"}],
                    "images": [
                        {
                            "image_digest": template["image_digest"],
                            "architecture": "linux/amd64",
                            "verified": True,
                        }
                    ],
                    "frameworks": [
                        {"framework": "NEXA_CPU", "framework_version": "1.0.0", "device": "CPU"}
                    ],
                },
            )
        )
        connection.execute(update(s.workers).values(last_heartbeat_at=func.clock_timestamp()))
        connection.execute(text("ANALYZE jobs"))
        connection.execute(text("ANALYZE job_specs"))


def _prepare_dispatch(engine):
    """Isolate the dispatch path on a direct-DB benchmark clone."""
    with Session(engine) as session, session.begin():
        inventory = session.execute(select(s.worker_inventories)).mappings().one()
        capacity = (inventory["host_cpu_millis"], inventory["allocatable_cpu_millis"])
        if capacity not in {(8000, 6000), (16000, 12000)}:
            raise ValueError("Dispatch preparation expects the 8-core API fixture")
        now = session.execute(select(func.clock_timestamp())).scalar_one()
        account_locked(session, now)
        if capacity == (8000, 6000):
            session.execute(
                update(s.worker_inventories).values(
                    host_cpu_millis=16000,
                    allocatable_cpu_millis=12000,
                )
            )
            rebase_locked(session, now)
        session.execute(
            update(s.reservations)
            .where(s.reservations.c.invalidated_at.is_(None))
            .values(invalidated_at=now, invalidation_reason="benchmark_fixture_reset")
        )
        session.execute(update(s.workers).values(last_heartbeat_at=now))
    _reset_queued_age(engine)


def _reset_queued_age(engine):
    # Fixture-only bulk reset: rebuild the derived heads once after the batch.
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE jobs DISABLE TRIGGER b13_job_head_update"))
    try:
        with engine.begin() as connection:
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.state == "QUEUED")
                .values(eligible_since=func.clock_timestamp())
            )
    finally:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE jobs ENABLE TRIGGER b13_job_head_update"))
    with engine.begin() as connection:
        connection.execute(
            text("SELECT nexa_b13_refresh_queue_head(tenant_id, priority) FROM queue_heads")
        ).all()
    with engine.begin() as connection:
        connection.execute(text("ANALYZE jobs"))


def _drain_eligibility(engine, service):
    """Replay every pending eligibility page outside the measurement; return the pages."""
    from nexa.coordinator.eligibility import pending_eligibility_tenants, process_eligibility_batch

    pages = 0
    while True:
        with Session(engine) as session, session.begin():
            service._locks(session)
            if not pending_eligibility_tenants(session):
                return pages
            process_eligibility_batch(
                session, session.execute(select(func.clock_timestamp())).scalar_one()
            )
        pages += 1


def _prepare_default_quota(engine):
    """Give every tenant the default 50% quota of a 2-core pool, so each 1-core
    dispatch exhausts its tenant's CPU headroom and each release restores it."""
    from benchmarks.b13.runtime_fairness import complete_fixture_allocation

    service = CoordinatorService(create_session_factory(engine))
    with engine.connect() as connection:
        held = (
            connection.execute(
                select(s.allocations.c.allocation_id).where(s.allocations.c.state != "RELEASED")
            )
            .scalars()
            .all()
        )
    for allocation_id in held:
        complete_fixture_allocation(engine, service, allocation_id)
    with Session(engine) as session, session.begin():
        service._locks(session)
        inventory = session.execute(select(s.worker_inventories)).mappings().one()
        if (inventory["host_cpu_millis"], inventory["allocatable_cpu_millis"]) != (8000, 6000):
            raise ValueError("Default quota preparation expects the 8-core API fixture")
        now = session.execute(select(func.clock_timestamp())).scalar_one()
        account_locked(session, now)
        session.execute(update(s.worker_inventories).values(allocatable_cpu_millis=2000))
        rebase_locked(session, now)
        session.execute(
            update(s.tenant_policies)
            .where(s.tenant_policies.c.is_current.is_(True))
            .values(
                cpu_limit_millis=1000,
                memory_limit_bytes=inventory["allocatable_memory_bytes"] // 2,
            )
        )
        session.execute(
            update(s.reservations)
            .where(s.reservations.c.invalidated_at.is_(None))
            .values(invalidated_at=now, invalidation_reason="benchmark_fixture_reset")
        )
    # Admin-rate capacity events from the fixture change are drained before measuring.
    pages = _drain_eligibility(engine, service)
    with engine.begin() as connection:
        connection.execute(update(s.workers).values(last_heartbeat_at=func.clock_timestamp()))
    _reset_queued_age(engine)
    with engine.begin() as connection:
        connection.execute(text("ANALYZE queue_request_sizes"))
        connection.execute(text("ANALYZE quota_headroom_steps"))
    return {"released_fixture_allocations": len(held), "eligibility_pages_drained": pages}


_WRITE_MARK = text("SELECT pg_snapshot_xmax(pg_current_snapshot())::text::bigint % 4294967296")
_ROWS_WRITTEN = text(
    """
    /* b13 fixture write probe */
    SELECT
        (SELECT count(*) FROM jobs WHERE xmin::text::bigint >= :mark) AS jobs,
        (SELECT count(*) FROM queue_eligibility_events WHERE xmin::text::bigint >= :mark)
            AS eligibility_events,
        (SELECT count(*) FROM queue_eligibility_events WHERE completed_at IS NULL)
            AS pending_eligibility_events
    """
)


def _write_mark(engine):
    with engine.connect() as connection:
        return connection.execute(_WRITE_MARK).scalar_one()


def _rows_written(engine, mark):
    """Rows whose current version was written since the mark (xid wraparound-free fixture)."""
    with engine.connect() as connection:
        return dict(connection.execute(_ROWS_WRITTEN, {"mark": mark}).mappings().one())


def _expand_dispatch_capacity(engine):
    """Give an existing dispatch clone room for repeated commit measurements."""
    with Session(engine) as session, session.begin():
        inventory = session.execute(select(s.worker_inventories)).mappings().one()
        if (inventory["host_cpu_millis"], inventory["allocatable_cpu_millis"]) != (
            16000,
            12000,
        ):
            raise ValueError("Capacity expansion expects the prepared dispatch clone")
        now = session.execute(select(func.clock_timestamp())).scalar_one()
        account_locked(session, now)
        session.execute(
            update(s.worker_inventories).values(
                host_cpu_millis=24000,
                allocatable_cpu_millis=18000,
            )
        )
        rebase_locked(session, now)
        session.execute(update(s.workers).values(last_heartbeat_at=now))


def _prepare_eligibility_event(engine):
    """Create one quota boundary for an isolated queue-plan fixture."""
    with engine.begin() as connection:
        tenant_id = connection.execute(
            select(s.tenant_policies.c.tenant_id)
            .where(
                s.tenant_policies.c.is_current.is_(True),
                s.tenant_policies.c.cpu_limit_millis > 500,
                exists(
                    select(s.jobs.c.job_id)
                    .join(s.job_specs, s.job_specs.c.job_id == s.jobs.c.job_id)
                    .where(
                        s.jobs.c.tenant_id == s.tenant_policies.c.tenant_id,
                        s.jobs.c.state == "QUEUED",
                        s.job_specs.c.cpu_millis > 500,
                        s.job_specs.c.cpu_millis <= s.tenant_policies.c.cpu_limit_millis,
                    )
                ),
                ~exists(
                    select(s.allocations.c.allocation_id).where(
                        s.allocations.c.tenant_id == s.tenant_policies.c.tenant_id,
                        s.allocations.c.state != "RELEASED",
                    )
                ),
            )
            .order_by(s.tenant_policies.c.tenant_id)
            .limit(1)
        ).scalar_one_or_none()
        if tenant_id is None:
            raise ValueError("No queued tenant can generate an isolated quota eligibility event")
        connection.execute(
            update(s.tenant_policies)
            .where(s.tenant_policies.c.tenant_id == tenant_id, s.tenant_policies.c.is_current)
            .values(cpu_limit_millis=500)
        )
    return tenant_id


def _measure(engine, repetitions: int, release_after_dispatch: bool = False):
    service = CoordinatorService(create_session_factory(engine))
    epoch = service.acquire()
    timings = []
    captured = []
    statements_per_run = []
    current_statements = None

    def record(_connection, _cursor, statement, parameters, _context, executemany):
        if "b13 fixture write probe" in statement:
            return
        if current_statements is not None:
            current_statements.append(statement)
        if not executemany and any(
            term in statement
            for term in (
                "FROM jobs",
                "UPDATE jobs",
                "queue_heads",
                "queue_eligibility_events",
                "fairness_ledgers",
                "allocation_ledger_segments",
            )
        ):
            captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        for index in range(repetitions + 1):
            if not service.renew(epoch):
                raise RuntimeError("Benchmark coordinator lost leadership before a measured tick")
            # The direct-DB fixture has no worker heartbeat process.
            with engine.begin() as connection:
                connection.execute(
                    update(s.workers).values(last_heartbeat_at=func.clock_timestamp())
                )
            with engine.connect() as connection:
                before = connection.execute(
                    select(func.min(s.fairness_ledgers.c.accounted_through))
                ).scalar_one()
            mark = _write_mark(engine)
            current_statements = []
            started = time.perf_counter()
            try:
                decision = service.tick(epoch)
                error = None
            except Exception as exc:
                decision = None
                error = f"{type(exc).__name__}: {str(exc)[:240]}"
            duration_ms = (time.perf_counter() - started) * 1000
            statements_per_run.append(current_statements)
            current_statements = None
            accounting_ms = None
            if error is None:
                accounting_started = time.perf_counter()
                try:
                    service.account(epoch)
                    accounting_ms = (time.perf_counter() - accounting_started) * 1000
                except Exception as exc:
                    error = f"accounting: {type(exc).__name__}: {str(exc)[:240]}"
            with engine.connect() as connection:
                after = connection.execute(
                    select(func.min(s.fairness_ledgers.c.accounted_through))
                ).scalar_one()
            written = _rows_written(engine, mark)
            release = None
            if release_after_dispatch and error is None and isinstance(decision, Dispatch):
                release = _release_dispatch(engine, service, decision.job_id)
            timings.append(
                {
                    "run": index,
                    "warmup": index == 0,
                    "duration_ms": duration_ms,
                    "accounting_heartbeat_ms": accounting_ms,
                    "decision": type(decision).__name__ if decision else None,
                    "error": error,
                    "accounted_through_before": before.isoformat() if before else None,
                    "accounted_through_after": after.isoformat() if after else None,
                    "sql_statement_count": len(statements_per_run[-1]),
                    "queue_statement_count": sum(
                        "FROM jobs" in statement or "UPDATE jobs" in statement
                        for statement in statements_per_run[-1]
                    ),
                    "rows_written_by_tick_and_accounting": written,
                    "fixture_release": release,
                }
            )
            print(
                f"run {index}/{repetitions}: {type(decision).__name__ if decision else 'error'} "
                f"{duration_ms:.1f} ms",
                flush=True,
            )
            if error:
                break
    finally:
        event.remove(engine, "before_cursor_execute", record)
    if not captured:
        raise RuntimeError("No queue query was observed; benchmark fixture was not schedulable")

    plans = []
    seen = set()
    for statement, parameters in captured:
        if statement in seen:
            continue
        seen.add(statement)
        if len(plans) >= 32:
            break
        with engine.connect() as connection:
            transaction = connection.begin()
            # Mirror the coordinator's per-transaction setting.
            connection.exec_driver_sql("SET LOCAL jit = off")
            try:
                plan = connection.exec_driver_sql(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
                ).scalar_one()
                plans.append({"sql": statement, "analyzed": True, "plan": plan})
            except DBAPIError as exc:
                # A replayed write can violate constraints against later state.
                transaction.rollback()
                transaction = connection.begin()
                connection.exec_driver_sql("SET LOCAL jit = off")
                plan = connection.exec_driver_sql(
                    "EXPLAIN (FORMAT JSON) " + statement, parameters
                ).scalar_one()
                plans.append(
                    {
                        "sql": statement,
                        "analyzed": False,
                        "analyze_error": str(exc.orig).splitlines()[0][:240],
                        "plan": plan,
                    }
                )
            finally:
                transaction.rollback()
    measured = [row["duration_ms"] for row in timings if not row["warmup"] and not row["error"]]
    return {
        "runs": timings,
        "median_ms": statistics.median(measured) if measured else None,
        "range_ms": [min(measured), max(measured)] if measured else None,
        "captured_statement_count": len(captured),
        "plan_context": (
            "EXPLAIN rerun after measured ticks with jit off, as in coordinator "
            "transactions; DB state may have changed"
        ),
        "plans": plans,
    }


def _release_dispatch(engine, service, job_id):
    from benchmarks.b13.runtime_fairness import complete_fixture_allocation

    with engine.connect() as connection:
        allocation = (
            connection.execute(
                select(s.allocations.c.allocation_id, s.allocations.c.tenant_id).where(
                    s.allocations.c.job_id == job_id
                )
            )
            .mappings()
            .one()
        )
    mark = _write_mark(engine)
    started = time.perf_counter()
    complete_fixture_allocation(engine, service, allocation["allocation_id"])
    duration_ms = (time.perf_counter() - started) * 1000
    with engine.connect() as connection:
        steps = connection.execute(
            text(
                "SELECT resource, upper_bound FROM quota_headroom_steps "
                "WHERE tenant_id = :tenant ORDER BY resource, upper_bound"
            ),
            {"tenant": allocation["tenant_id"]},
        ).all()
    return {
        "tenant_id": str(allocation["tenant_id"]),
        "duration_ms": duration_ms,
        "rows_written": _rows_written(engine, mark),
        "quota_steps_after": [list(step) for step in steps],
    }


# The production loop starts a tick at most every 250 ms (coordinator/runtime.py).
_TICK_FLOOR_MS = 250.0
STALL_MUTATIONS = ("capability", "tenant-toggle")


def _pending_events(engine, tenant_id=None):
    query = (
        select(func.count())
        .select_from(s.queue_eligibility_events)
        .where(s.queue_eligibility_events.c.completed_at.is_(None))
    )
    if tenant_id is not None:
        query = query.where(s.queue_eligibility_events.c.tenant_id == tenant_id)
    with engine.connect() as connection:
        return connection.execute(query).scalar_one()


def _stall_mutation(engine, mutation):
    """Apply one admin-rate change; its PostgreSQL trigger appends the eligibility events.

    ``capability``: the worker inventory advertises one more adapter, so every tenant
    with queued Jobs is replayed although every Job stays possible. ``tenant-toggle``:
    an admin disables and re-enables one tenant (two events for that tenant only).
    """
    tenant_id = None
    with engine.begin() as connection:
        if mutation == "capability":
            inventory = connection.execute(select(s.worker_inventories)).mappings().one()
            capabilities = dict(inventory["workload_capabilities"])
            capabilities["adapters"] = [
                *capabilities["adapters"],
                {"adapter_id": "batch.inference", "adapter_version": "1.0.0"},
            ]
            connection.execute(
                update(s.worker_inventories)
                .where(s.worker_inventories.c.worker_id == inventory["worker_id"])
                .values(workload_capabilities=capabilities)
            )
        else:
            tenant_id = connection.execute(
                select(s.jobs.c.tenant_id)
                .where(s.jobs.c.state == "QUEUED")
                .order_by(s.jobs.c.tenant_id)
                .limit(1)
            ).scalar_one()
            connection.execute(
                update(s.tenants).where(s.tenants.c.tenant_id == tenant_id).values(enabled=False)
            )
    if mutation == "tenant-toggle":
        with engine.begin() as connection:
            connection.execute(
                update(s.tenants).where(s.tenants.c.tenant_id == tenant_id).values(enabled=True)
            )
    return tenant_id


def _job_tenant(engine, job_id):
    with engine.connect() as connection:
        return connection.execute(
            select(s.jobs.c.tenant_id).where(s.jobs.c.job_id == job_id)
        ).scalar_one()


def _measure_eligibility_stall(engine, mutation, max_ticks):
    """Ticks and time from one admin-rate mutation until dispatch resumes (B13-OBS-01).

    Ticks run back to back with the fixture heartbeat, accounting and release of each
    Dispatch, as in ``_measure``. The production loop starts ticks at most every 250 ms,
    so the production estimate charges each tick ``max(250 ms, measured tick)``.
    """
    service = CoordinatorService(create_session_factory(engine))
    epoch = service.acquire()
    drained_before = _drain_eligibility(engine, service)

    def tick():
        if not service.renew(epoch):
            raise RuntimeError("Benchmark coordinator lost leadership during the stall")
        with engine.begin() as connection:
            connection.execute(update(s.workers).values(last_heartbeat_at=func.clock_timestamp()))
        started = time.perf_counter()
        decision = service.tick(epoch)
        duration_ms = (time.perf_counter() - started) * 1000
        service.account(epoch)
        tenant = None
        if isinstance(decision, Dispatch):
            tenant = _job_tenant(engine, decision.job_id)
            _release_dispatch(engine, service, decision.job_id)
        return decision, duration_ms, tenant

    # Control: the drained fixture dispatches before the mutation.
    control = []
    for _ in range(max_ticks):
        decision, duration_ms, _tenant = tick()
        control.append(type(decision).__name__)
        if isinstance(decision, Dispatch):
            break
    else:
        raise RuntimeError("The drained fixture never dispatched before the mutation")

    with engine.connect() as connection:
        mutated_at = connection.execute(select(func.clock_timestamp())).scalar_one()
    wall_started = time.perf_counter()
    tenant_id = _stall_mutation(engine, mutation)
    events_created = _pending_events(engine)
    affected_events = _pending_events(engine, tenant_id) if tenant_id else events_created
    ticks = []
    first_dispatch = first_affected_dispatch = replay_done = None
    dispatches_before_replay_done = 0
    decisions = {}
    for index in range(1, max_ticks + 1):
        decision, duration_ms, tenant = tick()
        name = type(decision).__name__
        label = f"{name}:{decision.reason}" if hasattr(decision, "reason") else name
        decisions[label] = decisions.get(label, 0) + 1
        pending = _pending_events(engine, tenant_id)
        ticks.append(duration_ms)
        if isinstance(decision, Dispatch):
            first_dispatch = first_dispatch or index
            if replay_done is None:
                dispatches_before_replay_done += 1
            if first_affected_dispatch is None and (tenant_id is None or tenant == tenant_id):
                first_affected_dispatch = index
        if replay_done is None and pending == 0:
            replay_done = index
        if replay_done is not None and first_affected_dispatch is not None:
            break
        if index % 100 == 0:
            print(f"tick {index}: pending affected events {pending}", flush=True)
    wall_ms = (time.perf_counter() - wall_started) * 1000

    def estimate(count):
        return sum(max(_TICK_FLOOR_MS, value) for value in ticks[:count]) if count else None

    ordered = sorted(ticks)
    return {
        "mutation": mutation,
        "mutated_at_db": mutated_at.isoformat(),
        "eligibility_pages_drained_before": drained_before,
        "control_decisions_before_mutation": control,
        "events_created": events_created,
        "affected_tenant": str(tenant_id) if tenant_id else "all tenants with queued jobs",
        "affected_events": affected_events,
        "max_ticks": max_ticks,
        "ticks_measured": len(ticks),
        "ticks_until_first_dispatch": first_dispatch,
        "ticks_until_affected_dispatch": first_affected_dispatch,
        "ticks_until_affected_replay_done": replay_done,
        "dispatches_before_replay_done": dispatches_before_replay_done,
        "decisions": decisions,
        "tick_ms": {
            "median": statistics.median(ordered) if ordered else None,
            "p95": ordered[int(0.95 * (len(ordered) - 1))] if ordered else None,
            "max": ordered[-1] if ordered else None,
            "sum": sum(ordered),
        },
        "harness_wall_ms": wall_ms,
        "production_stall_estimate_ms": estimate(first_affected_dispatch),
        "production_replay_estimate_ms": estimate(replay_done),
        "estimate_basis": (
            "sum over the ticks up to the named tick of max(250 ms, measured tick): the "
            "production loop starts a tick at most every 250 ms"
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenants", type=int, default=100)
    parser.add_argument("--jobs-per-tenant", type=int, default=1000)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--prepare-api-worker", action="store_true")
    parser.add_argument("--prepare-dispatch", action="store_true")
    parser.add_argument("--expand-dispatch-capacity", action="store_true")
    parser.add_argument("--prepare-eligibility-event", action="store_true")
    parser.add_argument("--prepare-default-quota", action="store_true")
    parser.add_argument("--api-prefill-result", type=Path)
    parser.add_argument("--measure-eligibility-stall", choices=STALL_MUTATIONS)
    parser.add_argument("--stall-max-ticks", type=int, default=4000)
    parser.add_argument("--evidence-layer", choices=("P", "L"), default="P")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.tenants, args.jobs_per_tenant, args.repetitions) < 1:
        parser.error("tenants, jobs-per-tenant and repetitions must be positive")
    url = _guard_url(os.environ.get("NEXA_TEST_DATABASE_URL", ""))
    engine = create_engine(url, pool_pre_ping=True)
    if inspect_schema_compatibility(engine) is not SchemaCompatibility.CURRENT:
        raise RuntimeError("Migrate the isolated benchmark database to the current head first")
    with engine.connect() as connection:
        version = connection.execute(text("SHOW server_version_num")).scalar_one()
    if int(version) // 10000 != 17:
        raise RuntimeError("B13 production queue microbenchmark requires PostgreSQL 17")
    if args.seed:
        _seed(engine, args.tenants, args.jobs_per_tenant)
    if args.seed and args.prepare_api_worker:
        parser.error("--seed and --prepare-api-worker are mutually exclusive")
    if args.prepare_dispatch:
        _prepare_dispatch(engine)
    if args.expand_dispatch_capacity:
        _expand_dispatch_capacity(engine)
    event_tenant_id = _prepare_eligibility_event(engine) if args.prepare_eligibility_event else None
    default_quota = _prepare_default_quota(engine) if args.prepare_default_quota else None
    with engine.connect() as connection:
        count = connection.execute(select(func.count()).select_from(s.jobs)).scalar_one()
        queued = connection.execute(
            select(func.count()).select_from(s.jobs).where(s.jobs.c.state == "QUEUED")
        ).scalar_one()
        heads = connection.execute(select(func.count()).select_from(s.queue_heads)).scalar_one()
    if count < args.tenants * args.jobs_per_tenant:
        raise RuntimeError("Queue is smaller than the declared benchmark fixture")
    api_prefill = None
    if args.api_prefill_result:
        api_prefill = json.loads(args.api_prefill_result.read_text())
        accepted = api_prefill["accepted_count"]
        reconciliation = api_prefill["reconciliation"]
        if (
            accepted != count
            or accepted != reconciliation["traceable_job_session_spec"]
            or accepted != reconciliation["global_outstanding"]
            or api_prefill["measurement"]["errors"]
            or api_prefill["fixture"]["tenants"] != args.tenants
            or api_prefill["fixture"]["jobs_per_tenant"] != args.jobs_per_tenant
        ):
            raise RuntimeError("API prefill result does not match this queue fixture")
    if args.prepare_api_worker:
        _prepare_api_worker(engine)
    if args.measure_eligibility_stall:
        measurement = _measure_eligibility_stall(
            engine, args.measure_eligibility_stall, args.stall_max_ticks
        )
    else:
        measurement = _measure(
            engine, args.repetitions, release_after_dispatch=args.prepare_default_quota
        )
    result = {
        "evidence_layer": f"{args.evidence_layer} direct-DB microbenchmark; not API acceptance",
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "environment": {
            "os": platform.platform(),
            "postgres_version_num": version,
            "database_host": url.host,
            "database_port": url.port,
            "database_name": url.database,
        },
        "fixture": {
            "tenants": args.tenants,
            "jobs_per_tenant": args.jobs_per_tenant,
            "queued_jobs_before_measure": queued,
            "queue_heads": heads,
            "seeded_through_production_api": api_prefill is not None,
            "api_prefill_result": str(args.api_prefill_result) if api_prefill else None,
            "api_prefill_sha256": (
                hashlib.sha256(args.api_prefill_result.read_bytes()).hexdigest()
                if api_prefill
                else None
            ),
            "worker_fixture": "direct-DB CPU test worker" if args.prepare_api_worker else None,
            "eligibility_event_tenant": str(event_tenant_id) if event_tenant_id else None,
            "default_quota_fixture": (
                {
                    "description": "direct-DB: 2-core allocatable pool, every current tenant "
                    "policy at 50% CPU/memory quota, fixture release after each dispatch",
                    **default_quota,
                }
                if default_quota
                else None
            ),
            "dispatch_fixture": (
                "direct-DB: accounting rebase, 16-core host/12-core allocatable, "
                "reservation invalidation, queued age reset"
                if args.prepare_dispatch
                else "direct-DB: accounting rebase, 24-core host/18-core allocatable"
                if args.expand_dispatch_capacity
                else None
            ),
        },
        "provenance": _provenance(),
        "measurement": measurement,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {
                "count": count,
                "heads": heads,
                "median_ms": measurement.get("median_ms"),
                "errors": [row["error"] for row in measurement.get("runs", []) if row["error"]],
                "stall": {
                    key: measurement[key]
                    for key in (
                        "ticks_until_first_dispatch",
                        "ticks_until_affected_dispatch",
                        "ticks_until_affected_replay_done",
                        "harness_wall_ms",
                        "production_stall_estimate_ms",
                        "production_replay_estimate_ms",
                    )
                    if key in measurement
                },
            }
        )
    )


if __name__ == "__main__":
    main()
