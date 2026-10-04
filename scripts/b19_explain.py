"""B19 query-plan evidence for the storage GC lookups and the read-only checks.

Seeds a disposable test database (200k artifacts over 20 tenants with their
references, upload sessions in every state, 100k jobs and results for the
SUCCEEDED ones) and prints EXPLAIN (ANALYZE, BUFFERS) for the statements GC and
`nexa-maintenance storage-check` / `consistency-check` run. The database guard
is the one of scripts/b18_explain.py (loopback, ``nexa_b05_test_`` prefix, PG17).

    PYTHONPATH=src:. python scripts/b19_explain.py seed
    PYTHONPATH=src:. python scripts/b19_explain.py explain > out

The URL is read from NEXA_TEST_DATABASE_URL and never printed. Result rows are
seeded with ``session_replication_role = replica`` (no FK or trigger checks) on
the disposable database only: their attempts and reservations are irrelevant to
the plans, which read `results.job_id` and `jobs`.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, func, select, text

from nexa.application.storage_checks import (
    _COUNTER_DRIFT,
    _RESULT_CONSISTENCY,
    _RETAINED_UNPUBLISHED,
)
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from scripts.b18_explain import _engine, _explain, _insert_jobs, _reset
from tests.integration._factories import seed_tenant_graph

TENANTS = 20
JOBS = 100_000
ARTIFACTS_PER_TENANT = 10_000
UPLOADED_SHARE = 0.2  # artifacts that came through an upload session
RETAINED_SHARE = 0.5  # of those, never published (B19-R05)
ACTIVE_SESSIONS_PER_TENANT = 500
OVERDUE_SESSIONS_PER_TENANT = 50
EXPIRED_SESSIONS_PER_TENANT = 2_000
LOOKUP_KEYS = 500
BATCH = 5_000


def _checksum(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _session(tenant_id, state: str, expires_at: datetime) -> dict:
    upload_id = new_uuid7()
    return {
        "upload_id": upload_id,
        "tenant_id": tenant_id,
        "expected_size_bytes": 4096,
        "expected_checksum": _checksum(str(upload_id)),
        "staged_key": f"staging/{upload_id.hex}",
        "bytes_received": 4096 if state == "COMMITTED" else 0,
        "expires_at": expires_at,
        "state": state,
    }


def _insert(connection, table, rows: list[dict]) -> None:
    for start in range(0, len(rows), BATCH):
        connection.execute(table.insert(), rows[start : start + BATCH])


def _seed_tenant(connection, graph, rng: random.Random, now: datetime) -> None:
    tenant_id = graph["tenant_id"]
    job_ids = list(
        connection.execute(select(s.jobs.c.job_id).where(s.jobs.c.tenant_id == tenant_id))
        .scalars()
        .all()
    )
    artifacts, sessions, references = [], [], []
    for index in range(ARTIFACTS_PER_TENANT):
        artifact_id = new_uuid7()
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "kind": "INPUT",
                "media_type": "application/octet-stream",
                "size_bytes": rng.randint(1, 1 << 20),
                "checksum": _checksum(f"{tenant_id}-{index}"),
                "blob_key": f"blobs/{artifact_id.hex}",
                "state": "COMMITTED",
                "version": 1,
            }
        )
        published = True
        if rng.random() < UPLOADED_SHARE:
            session = _session(tenant_id, "COMMITTED", now - timedelta(days=1))
            sessions.append(session)
            references.append(
                {
                    "tenant_id": tenant_id,
                    "artifact_id": artifact_id,
                    "owner_type": "UPLOAD_SESSION",
                    "owner_id": session["upload_id"],
                    "purpose": "ATTEMPT_UPLOAD",
                    "logical_name": "upload",
                }
            )
            published = rng.random() >= RETAINED_SHARE
        if published:
            references.append(
                {
                    "tenant_id": tenant_id,
                    "artifact_id": artifact_id,
                    "owner_type": "JOB_SPEC",
                    "owner_id": rng.choice(job_ids),
                    "purpose": "INPUT",
                    "logical_name": f"a{index}",
                }
            )
    for _ in range(ACTIVE_SESSIONS_PER_TENANT):
        sessions.append(_session(tenant_id, "ACTIVE", now + timedelta(hours=1)))
    for _ in range(OVERDUE_SESSIONS_PER_TENANT):
        sessions.append(_session(tenant_id, "ACTIVE", now - timedelta(minutes=5)))
    for _ in range(EXPIRED_SESSIONS_PER_TENANT):
        sessions.append(_session(tenant_id, "EXPIRED", now - timedelta(days=2)))
    _insert(connection, s.artifacts, artifacts)
    _insert(connection, s.upload_sessions, sessions)
    _insert(connection, s.artifact_references, references)


def seed(engine: Engine) -> None:
    rng = random.Random(19)
    _reset(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        graphs = [seed_tenant_graph(connection, label=f"b19-perf-{i:02d}") for i in range(TENANTS)]
    for graph in graphs:
        with engine.begin() as connection:
            _insert_jobs(connection, graph, rng, now, JOBS // TENANTS)
            _seed_tenant(connection, graph, rng, now)
    with engine.begin() as connection:
        # Counters as the services keep them, then one drifted tenant to report.
        connection.execute(
            text(
                """
                INSERT INTO artifact_storage_counters (tenant_id, committed_bytes, reserved_bytes)
                SELECT t.tenant_id,
                       COALESCE((SELECT SUM(size_bytes) FROM artifacts a
                                  WHERE a.tenant_id = t.tenant_id AND a.state = 'COMMITTED'), 0),
                       COALESCE((SELECT SUM(expected_size_bytes) FROM upload_sessions u
                                  WHERE u.tenant_id = t.tenant_id AND u.state = 'ACTIVE'), 0)
                FROM tenants t
                ON CONFLICT (tenant_id) DO UPDATE
                SET committed_bytes = EXCLUDED.committed_bytes,
                    reserved_bytes = EXCLUDED.reserved_bytes
                """
            )
        )
        connection.execute(
            text(
                "UPDATE artifact_storage_counters SET committed_bytes = committed_bytes + 1"
                " WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": graphs[0]["tenant_id"]},
        )
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        succeeded = connection.execute(
            select(s.jobs.c.job_id, s.jobs.c.tenant_id).where(s.jobs.c.state == "SUCCEEDED")
        ).all()
        rows = [
            {
                "result_id": new_uuid7(),
                "tenant_id": tenant_id,
                "job_id": job_id,
                "attempt_id": new_uuid7(),
                "manifest_artifact_id": new_uuid7(),
                "manifest_checksum": _checksum(str(job_id)),
            }
            # Every SUCCEEDED job but one has its result: one row to report.
            for job_id, tenant_id in succeeded[1:]
        ]
        _insert(connection, s.results, rows)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("VACUUM ANALYZE"))
    _summary(engine)


def _count(connection, table, *where) -> int:
    return connection.execute(select(func.count()).select_from(table).where(*where)).scalar_one()


def _summary(engine: Engine) -> None:
    with engine.connect() as connection:
        sessions = s.upload_sessions
        print(f"server {connection.execute(text('SELECT version()')).scalar_one()}")
        print(f"tenants={_count(connection, s.tenants)} jobs={_count(connection, s.jobs)}")
        print(
            f"artifacts={_count(connection, s.artifacts)} "
            f"references={_count(connection, s.artifact_references)} "
            f"results={_count(connection, s.results)}"
        )
        for state in ("ACTIVE", "COMMITTED", "EXPIRED"):
            count = _count(connection, sessions, sessions.c.state == state)
            print(f"upload_sessions {state}={count}")
        indexes = connection.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE tablename IN ('artifacts',"
                " 'upload_sessions', 'artifact_references', 'results', 'jobs')"
                " ORDER BY tablename, indexname"
            )
        ).scalars()
        print("indexes: " + ", ".join(indexes))


def _timed(connection, title: str, sql: str, params: dict) -> None:
    started = time.perf_counter()
    count = len(connection.exec_driver_sql(sql, params).all())
    elapsed = (time.perf_counter() - started) * 1000
    print(f"# {title}: rows={count} wall_ms={elapsed:.1f}")
    _explain(connection, title, sql, params)


def _compiled(engine: Engine, statement, params: dict | None = None) -> tuple[str, dict]:
    compiled = statement.compile(
        dialect=engine.dialect, compile_kwargs={"render_postcompile": True}
    )
    values = dict(compiled.params)
    values.update(params or {})
    return str(compiled), values


def explain(engine: Engine) -> None:
    _summary(engine)
    print()
    rng = random.Random(1919)
    artifacts, sessions = s.artifacts, s.upload_sessions
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        blob_keys = list(connection.execute(select(artifacts.c.blob_key)).scalars())
        staged = list(
            connection.execute(
                select(sessions.c.staged_key).where(sessions.c.state.in_(["ACTIVE", "EXPIRED"]))
            ).scalars()
        )
        # A batch as GC sends it: half with rows, half orphans/stale names.
        orphan_batch = rng.sample(blob_keys, LOOKUP_KEYS // 2) + [
            f"blobs/{new_uuid7().hex}" for _ in range(LOOKUP_KEYS // 2)
        ]
        staging_batch = rng.sample(staged, LOOKUP_KEYS // 2) + [
            f"staging/{new_uuid7().hex}" for _ in range(LOOKUP_KEYS // 2)
        ]
        cases = [
            (
                "G2 orphan lookup (blob_key IN 500)",
                select(artifacts.c.blob_key).where(artifacts.c.blob_key.in_(orphan_batch)),
            ),
            (
                "G1 staging lookup (staged_key IN 500, ACTIVE)",
                select(sessions.c.staged_key).where(
                    sessions.c.staged_key.in_(staging_batch), sessions.c.state == "ACTIVE"
                ),
            ),
            (
                "G2 in-lock row check (blob_key =)",
                select(artifacts.c.artifact_id).where(artifacts.c.blob_key == orphan_batch[0]),
            ),
            (
                "G1 expire candidates (ACTIVE, expires_at <= now, limit 1000)",
                select(sessions)
                .where(sessions.c.state == "ACTIVE", sessions.c.expires_at <= func.now())
                .order_by(sessions.c.expires_at, sessions.c.upload_id)
                .limit(1000),
            ),
        ]
        for title, statement in cases:
            _timed(connection, title, *_compiled(engine, statement))
        _timed(connection, "storage-check counter drift", *_compiled(engine, _COUNTER_DRIFT))
        retained = _compiled(engine, _RETAINED_UNPUBLISHED)
        _timed(connection, "storage-check retained unpublished", *retained)
        accepted = list(connection.execute(select(s.jobs.c.job_id).limit(1000)).scalars())
        _timed(
            connection,
            "consistency-check (1000 accepted IDs)",
            *_compiled(engine, _RESULT_CONSISTENCY, {"accepted": accepted}),
        )
        connection.rollback()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["seed", "summary", "explain"])
    action = parser.parse_args().action
    engine = _engine()
    try:
        {"seed": seed, "summary": _summary, "explain": explain}[action](engine)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
