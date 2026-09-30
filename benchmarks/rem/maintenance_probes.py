"""Leader maintenance-probe microbenchmark (B15-R18 retention sweep, B14-K1 retry probe).

Direct-DB on an isolated loopback `nexa_b05_test_` database; never acceptance
evidence. The same file runs on the baseline and on the remediated tree: it drives
only `CoordinatorService.sweep_idempotency` and `promote_retries` and captures the
SQL they send, so each tree's own statements are timed and explained.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import statistics
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, select, text

from benchmarks.b13.queue_microbench import _guard_url, _seed
from nexa.coordinator.service import CoordinatorService
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.ids import new_uuid7

_HASH = "sha256:" + "0" * 64
_LOCKING = re.compile(r"^\s*(UPDATE|INSERT|DELETE)\b|\bFOR (NO KEY )?(UPDATE|SHARE)\b", re.I)
_WRITE = re.compile(r"^\s*(UPDATE|INSERT|DELETE)\b", re.I)
_SWEPT = "('submitJob','cancelJob','pauseJob','resumeJob','retryFailedJob')"


def _source_tree_sha256(root):
    """Path and content hash of every source file, so a tree copy without .git is identified."""
    digest = hashlib.sha256()
    for path in sorted(p for p in Path(root).rglob("*.py") if "__pycache__" not in p.parts):
        digest.update(f"{path}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
    return digest.hexdigest()


class Capture:
    """Statements sent through the engine while enabled."""

    def __init__(self, engine):
        self.statements = []
        self.enabled = False
        event.listen(engine, "before_cursor_execute", self._capture)

    def _capture(self, _connection, _cursor, statement, parameters, _context, executemany):
        if self.enabled:
            self.statements.append((statement, parameters, executemany))

    @contextmanager
    def __call__(self):
        self.statements = []
        self.enabled = True
        try:
            yield self.statements
        finally:
            self.enabled = False


def _timed(operation):
    started = time.perf_counter()
    value = operation()
    return value, (time.perf_counter() - started) * 1000


def _summary(samples):
    ordered = sorted(samples)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[max(0, int(0.95 * len(ordered)) - 1)], 3),
        "max_ms": round(ordered[-1], 3),
        "total_ms": round(sum(ordered), 3),
    }


def _condense(node):
    keys = {
        "Node Type": "node",
        "Relation Name": "relation",
        "Index Name": "index",
        "Actual Rows": "rows",
        "Actual Loops": "loops",
        "Rows Removed by Filter": "removed_by_filter",
        "Shared Hit Blocks": "shared_hit",
        "Shared Read Blocks": "shared_read",
        "Actual Total Time": "total_ms",
    }
    condensed = {short: node[key] for key, short in keys.items() if key in node}
    children = [_condense(child) for child in node.get("Plans", [])]
    if children:
        condensed["plans"] = children
    return condensed


def _explain(engine, statement, parameters):
    """EXPLAIN ANALYZE in a transaction that is rolled back (the walks lock rows)."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql("SET LOCAL jit = off")
            (plan,) = connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
            ).scalar_one()
        finally:
            transaction.rollback()
    return {
        "statement_head": " ".join(statement.split())[:160],
        "execution_ms": plan["Execution Time"],
        "planning_ms": plan["Planning Time"],
        "plan": _condense(plan["Plan"]),
    }


def _walk_statements(statements):
    return [
        (statement, parameters)
        for statement, parameters, executemany in statements
        if not executemany
        and statement.lstrip().upper().startswith("SELECT")
        and "FROM idempotency_records" in statement
        and "FOR UPDATE" in statement
    ]


def _migrate(raw_url):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", raw_url.replace("%", "%%"))
    command.upgrade(config, "head")


def _seed_records(engine, terminal_every):
    """One expired submitJob record per Job; every `terminal_every`-th Job is terminal."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO idempotency_records (idempotency_id, context, principal_id, "
                "operation_id, idempotency_key, request_hash, state, response_status, "
                "resource_id, expires_at) "
                "SELECT gen_random_uuid(), j.tenant_id::text, j.submitter_user_id::text, "
                "'submitJob', 'rem-probe-' || j.job_id::text, :hash, 'COMPLETED', 202, "
                "j.job_id, now() - interval '1 day' - random() * interval '10 days' FROM jobs j"
            ),
            {"hash": _HASH},
        )
        terminal = connection.execute(
            text(
                "UPDATE jobs SET state = 'CANCELLED', desired_state = 'CANCELLED', "
                "terminal_at = now() - interval '40 days' WHERE job_id IN (SELECT job_id FROM "
                "(SELECT job_id, row_number() OVER (ORDER BY job_id) AS n FROM jobs) ranked "
                "WHERE n % :every = 0)"
            ),
            {"every": terminal_every},
        ).rowcount
        connection.execute(text("ANALYZE jobs"))
        connection.execute(text("ANALYZE idempotency_records"))
    return terminal


def _due_remaining(engine):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM idempotency_records r "
                "JOIN jobs j ON j.job_id = r.resource_id "
                f"WHERE r.state = 'COMPLETED' AND r.operation_id IN {_SWEPT} "
                "AND r.expires_at < now() AND j.state IN ('SUCCEEDED','FAILED','CANCELLED')"
            )
        ).scalar_one()


def _expired_walkable(engine):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM idempotency_records WHERE state = 'COMPLETED' "
                f"AND operation_id IN {_SWEPT} AND expires_at < now()"
            )
        ).scalar_one()


class Leader:
    """One leader; renewed outside the timed calls so long runs keep the lease."""

    def __init__(self, engine):
        self.service = CoordinatorService(create_session_factory(engine), holder_id=new_uuid7())
        self.epoch = self.service.acquire()
        if self.epoch is None:
            raise RuntimeError("leadership is held by another coordinator")
        self._renewed = time.monotonic()

    def renew(self):
        if time.monotonic() - self._renewed > 5:
            if not self.service.renew(self.epoch):
                raise RuntimeError("leadership was lost during the measurement")
            self._renewed = time.monotonic()


def _measure_sweep(engine, leader, capture, steady_ticks, max_ticks):
    before = {"expired_walkable": _expired_walkable(engine), "due": _due_remaining(engine)}
    drain, deleted, write_ticks, first = [], 0, 0, None
    for tick in range(max_ticks):
        leader.renew()
        with capture() as statements:
            count, ms = _timed(lambda: leader.service.sweep_idempotency(leader.epoch))
        drain.append(ms)
        deleted += count
        wrote = any(_WRITE.search(statement) for statement, _p, _m in statements)
        write_ticks += wrote
        if tick == 0:
            first = [_explain(engine, *walk) for walk in _walk_statements(statements)]
        if not wrote and _due_remaining(engine) == 0:
            break
    steady, walks = [], []
    for _ in range(steady_ticks):
        leader.renew()
        with capture() as statements:
            count, ms = _timed(lambda: leader.service.sweep_idempotency(leader.epoch))
        if count:
            raise RuntimeError("a steady tick deleted records")
        steady.append(ms)
        walks = _walk_statements(statements)
    return {
        "before": before,
        "drain": {
            "ticks": len(drain),
            "ticks_with_writes": write_ticks,
            "deleted": deleted,
            "tick": _summary(drain),
            "explain_after_first_tick": first,
        },
        "after": {"expired_walkable": _expired_walkable(engine), "due": _due_remaining(engine)},
        "steady": {
            "tick": _summary(steady),
            "explain": [_explain(engine, *walk) for walk in walks],
        },
    }


def _seed_blocked_retries(engine, count):
    """Due retries of one tenant that its zero CPU quota blocks, as RETRY_WAIT entry leaves them."""
    with engine.begin() as connection:
        tenant_id = connection.execute(
            select(s.jobs.c.tenant_id).where(s.jobs.c.state == "QUEUED").limit(1)
        ).scalar_one()
        job_ids = (
            connection.execute(
                select(s.jobs.c.job_id)
                .where(s.jobs.c.tenant_id == tenant_id, s.jobs.c.state == "QUEUED")
                .order_by(s.jobs.c.job_id)
                .limit(count)
            )
            .scalars()
            .all()
        )
        connection.execute(
            s.tenant_policies.update()
            .where(s.tenant_policies.c.tenant_id == tenant_id)
            .values(cpu_limit_millis=0)
        )
        ready_at = connection.execute(select(func.clock_timestamp())).scalar_one()
        connection.execute(
            s.jobs.update()
            .where(s.jobs.c.job_id.in_(job_ids))
            .values(
                state="RETRY_WAIT",
                retry_count=1,
                job_fence=2,
                retry_ready_at=ready_at,
                waiting_reason="waiting_for_retry",
                eligible_since=None,
            )
        )
        connection.execute(
            s.retry_schedules.insert(),
            [
                {
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "retry_number": 1,
                    "ready_at": ready_at,
                    "jitter_milliseconds": 0,
                    "reason": "INFRASTRUCTURE",
                }
                for job_id in job_ids
            ],
        )
    return tenant_id, len(job_ids)


def _policy_lock_wait(engine, leader, holder_ms):
    """Wait of a submit-like policy FOR UPDATE while one probe meets a busy GLOBAL counter."""
    locked, probe_done = threading.Event(), {}

    def hold():
        with engine.connect() as holder:
            transaction = holder.begin()
            holder.execute(
                select(s.admission_counters.c.version)
                .where(s.admission_counters.c.scope_type == "GLOBAL")
                .with_for_update()
            )
            locked.set()
            time.sleep(holder_ms / 1000)
            transaction.commit()

    def probe():
        try:
            probe_done["result"], probe_done["ms"] = _timed(
                lambda: leader.service.promote_retries(leader.epoch)
            )
        except Exception as error:  # reported, not hidden: a lock timeout is a result
            probe_done["error"] = type(error).__name__

    holder = threading.Thread(target=hold)
    holder.start()
    locked.wait()
    prober = threading.Thread(target=probe)
    prober.start()
    time.sleep(0.05)
    with engine.connect() as victim:
        transaction = victim.begin()
        victim.exec_driver_sql("SET LOCAL lock_timeout = '2000ms'")
        _, wait_ms = _timed(
            lambda: victim.execute(
                select(s.policy_versions.c.policy_version)
                .where(s.policy_versions.c.is_current.is_(True))
                .with_for_update()
            ).one()
        )
        transaction.rollback()
    prober.join()
    holder.join()
    if "ms" in probe_done:
        probe_done["ms"] = round(probe_done["ms"], 3)
    return {"policy_lock_wait_ms": round(wait_ms, 3), **probe_done}


def _measure_retry(engine, leader, capture, tenant_id, probes, holder_ms, contention_samples):
    # The first probe announces the block reason under the locks, in both trees.
    with capture() as statements:
        promoted, first_ms = _timed(lambda: leader.service.promote_retries(leader.epoch))
    if promoted:
        raise RuntimeError("a blocked retry was promoted")
    first_locking = sum(bool(_LOCKING.search(statement)) for statement, _p, _m in statements)
    with engine.connect() as connection:
        reasons = sorted(
            set(
                connection.execute(
                    select(s.jobs.c.waiting_reason).where(
                        s.jobs.c.tenant_id == tenant_id, s.jobs.c.state == "RETRY_WAIT"
                    )
                ).scalars()
            )
        )
    idle, locking = [], []
    for _ in range(probes):
        leader.renew()
        with capture() as statements:
            promoted, ms = _timed(lambda: leader.service.promote_retries(leader.epoch))
        if promoted:
            raise RuntimeError("a blocked retry was promoted")
        idle.append(ms)
        locking.append(sum(bool(_LOCKING.search(statement)) for statement, _p, _m in statements))
    contention = []
    for _ in range(contention_samples):
        leader.renew()
        contention.append(_policy_lock_wait(engine, leader, holder_ms))
    return {
        "announcing_probe": {"ms": round(first_ms, 3), "locking_statements": first_locking},
        "waiting_reasons_after_announcement": reasons,
        "idle_probe": _summary(idle),
        "locking_statements_per_idle_probe": sorted(set(locking)),
        "locking_statements_per_hour_at_1s": statistics.mean(locking) * 3600,
        "holder_ms": holder_ms,
        "contention": contention,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", required=True, help="label of the source tree measured")
    parser.add_argument("--tenants", type=int, default=100)
    parser.add_argument("--jobs-per-tenant", type=int, default=1000)
    parser.add_argument("--terminal-every", type=int, default=100)
    parser.add_argument("--steady-ticks", type=int, default=30)
    parser.add_argument("--max-drain-ticks", type=int, default=5000)
    parser.add_argument("--blocked-retries", type=int, default=16)
    parser.add_argument("--retry-probes", type=int, default=60)
    parser.add_argument("--holder-ms", type=int, default=300)
    parser.add_argument("--contention-samples", type=int, default=5)
    parser.add_argument("--evidence-layer", choices=("P", "L"), default="L")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    raw_url = os.environ.get("NEXA_TEST_DATABASE_URL", "")
    url = _guard_url(raw_url)
    _migrate(raw_url)
    engine = create_engine(url, pool_pre_ping=True)
    try:
        _seed(engine, args.tenants, args.jobs_per_tenant)
        terminal = _seed_records(engine, args.terminal_every)
        capture = Capture(engine)
        leader = Leader(engine)
        sweep = _measure_sweep(engine, leader, capture, args.steady_ticks, args.max_drain_ticks)
        tenant_id, blocked = _seed_blocked_retries(engine, args.blocked_retries)
        retry = _measure_retry(
            engine,
            leader,
            capture,
            tenant_id,
            args.retry_probes,
            args.holder_ms,
            args.contention_samples,
        )
        with engine.connect() as connection:
            version = connection.execute(text("SHOW server_version_num")).scalar_one()
    finally:
        engine.dispose()
    result = {
        "evidence_layer": f"{args.evidence_layer} direct-DB microbenchmark; not acceptance",
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "tree": args.tree,
        "source_tree_sha256": _source_tree_sha256("src/"),
        "environment": {"os": platform.platform(), "postgres_version_num": version},
        "fixture": {
            "jobs": args.tenants * args.jobs_per_tenant,
            "expired_submit_records": args.tenants * args.jobs_per_tenant,
            "terminal_jobs": terminal,
            "blocked_due_retries": blocked,
        },
        "retention_sweep": sweep,
        "retry_probe": retry,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(args.output), "tree": args.tree}))


if __name__ == "__main__":
    main()
