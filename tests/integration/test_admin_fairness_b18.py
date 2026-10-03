"""B18 adminQueryFairness over PostgreSQL.

The SQL aggregate is compared with the pure reference model, against the ledger
charged by coordinator accounting, and checked to be a bounded plain read that never
waits on (or blocks) the accounting row locks.
"""

import random
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import event, func, select, text, update

from nexa.application.fairness_report import (
    LedgerSegment,
    aggregate_fairness,
    fairness_report,
)
from nexa.coordinator.accounting import account_now_locked, rebase_locked
from nexa.coordinator.service import CoordinatorService
from nexa.domain.scheduling import NoDecision
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.integration._admin_b18 import admin_session, audit_rows, member_session
from tests.integration._factories import seed_authority, seed_job, seed_tenant_graph, seed_worker
from tests.integration.test_coordinator_b11 import seed_dispatchable

pytestmark = pytest.mark.postgres

FROM = datetime(2026, 9, 1, tzinfo=UTC)


def _wire(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _db_now(engine) -> datetime:
    with engine.connect() as connection:
        return connection.execute(select(func.clock_timestamp())).scalar_one()


def _query(client, from_at, to_at, bucket_seconds, **extra):
    params = {"from": _wire(from_at), "to": _wire(to_at), "bucket_seconds": bucket_seconds}
    params.update(extra)
    return client.get("/v1/admin/fairness", params=params)


def _seed_allocations(engine, *, tenants=2, per_tenant=3):
    """Return {tenant_id: [allocation_id, ...]} backed by real attempts and allocations."""
    created: dict[UUID, list[UUID]] = {}
    with engine.begin() as connection:
        worker = seed_worker(connection, label="b18-fair")
        for index in range(tenants):
            graph = seed_tenant_graph(connection, label=f"b18-fair-{index}")
            created[graph["tenant_id"]] = []
            for _ in range(per_tenant):
                job = seed_job(connection, graph)
                authority = seed_authority(connection, graph, job, worker)
                created[graph["tenant_id"]].append(authority["allocation_id"])
    return created


def _current_policy_version(connection):
    return connection.execute(
        select(s.policy_versions.c.policy_version).where(s.policy_versions.c.is_current.is_(True))
    ).scalar_one()


def _insert_segments(engine, segments_by_allocation):
    with engine.begin() as connection:
        version = _current_policy_version(connection)
        for allocation_id, segment in segments_by_allocation:
            connection.execute(
                s.allocation_ledger_segments.insert().values(
                    segment_id=segment.segment_id,
                    allocation_id=allocation_id,
                    tenant_id=segment.tenant_id,
                    started_at=segment.started_at,
                    ended_at=segment.ended_at,
                    dominant_share=segment.dominant_share,
                    weight=segment.weight,
                    charged_amount=Decimal(0),
                    policy_version=version,
                )
            )


def _random_closed_segments(allocations, *, count, span, seed):
    rng = random.Random(seed)
    pairs = []
    shares = ["0", "0.125", "0.3333333333333333333333333333333333333333333333333", "1"]
    weights = ["0.5", "1", "3", "7.25"]
    pool = [(tenant, allocation) for tenant, ids in allocations.items() for allocation in ids]
    for _ in range(count):
        tenant, allocation = rng.choice(pool)
        a = rng.randrange(-span // 4, span + span // 4)
        b = a + rng.randrange(0, span // 2)
        pairs.append(
            (
                allocation,
                LedgerSegment(
                    segment_id=new_uuid7(),
                    tenant_id=tenant,
                    started_at=FROM + timedelta(microseconds=a),
                    ended_at=FROM + timedelta(microseconds=b),
                    dominant_share=Decimal(rng.choice(shares)),
                    weight=Decimal(rng.choice(weights)),
                ),
            )
        )
    return pairs


def _assert_close(actual, expected):
    assert set(actual) == set(expected)
    for key in expected:
        tolerance = 1e-9 * max(abs(expected[key]), 1.0)
        assert abs(actual[key] - expected[key]) <= tolerance, (key, actual[key], expected[key])


def _assert_report_matches(body, expected):
    assert {key: body[key] for key in ("from", "to", "bucket_seconds")} == {
        key: expected[key] for key in ("from", "to", "bucket_seconds")
    }
    assert len(body["buckets"]) == len(expected["buckets"])
    for got, want in zip(body["buckets"], expected["buckets"], strict=True):
        assert (got["tenant_id"], got["start_at"], got["end_at"]) == (
            want["tenant_id"],
            want["start_at"],
            want["end_at"],
        )
        _assert_close(
            {key: got[key] for key in got if isinstance(got[key], float)},
            {key: want[key] for key in want if isinstance(want[key], float)},
        )


@pytest.mark.parametrize("bucket_seconds", [11, 60, 600])
def test_sql_aggregate_matches_the_reference_model(
    migrated_postgres_engine, tmp_path, bucket_seconds
):
    allocations = _seed_allocations(migrated_postgres_engine)
    span = 3_600 * 1_000_000
    pairs = _random_closed_segments(allocations, count=60, span=span, seed=bucket_seconds)
    _insert_segments(migrated_postgres_engine, pairs)
    to_at = FROM + timedelta(hours=1)
    segments = [segment for _, segment in pairs]
    with admin_session(migrated_postgres_engine, tmp_path) as (client, _):
        response = _query(client, FROM, to_at, bucket_seconds)
        assert response.status_code == 200, response.text
        rows = aggregate_fairness(
            segments, from_at=FROM, to_at=to_at, bucket_seconds=bucket_seconds, now=to_at
        )
        expected = fairness_report(rows, from_at=FROM, to_at=to_at, bucket_seconds=bucket_seconds)
        assert response.json()["buckets"], "the seed must overlap the range"
        _assert_report_matches(response.json(), expected)

        tenant = next(iter(allocations))
        filtered = _query(client, FROM, to_at, bucket_seconds, tenant_id=str(tenant))
        assert filtered.status_code == 200, filtered.text
        assert filtered.json()["buckets"] == [
            bucket for bucket in response.json()["buckets"] if bucket["tenant_id"] == str(tenant)
        ]
        unknown = _query(client, FROM, to_at, bucket_seconds, tenant_id=str(new_uuid7()))
        assert unknown.status_code == 200
        assert unknown.json()["buckets"] == []


def test_open_segment_is_measured_to_the_statement_time(migrated_postgres_engine, tmp_path):
    allocations = _seed_allocations(migrated_postgres_engine, tenants=1, per_tenant=1)
    [(tenant, [allocation])] = allocations.items()
    now = datetime.now(UTC)
    started = now - timedelta(seconds=30)
    _insert_segments(
        migrated_postgres_engine,
        [
            (
                allocation,
                LedgerSegment(
                    segment_id=new_uuid7(),
                    tenant_id=tenant,
                    started_at=started,
                    ended_at=None,
                    dominant_share=Decimal("0.5"),
                    weight=Decimal(2),
                ),
            )
        ],
    )
    with admin_session(migrated_postgres_engine, tmp_path) as (client, _):
        before = _db_now(migrated_postgres_engine)
        response = _query(client, now - timedelta(minutes=1), now + timedelta(minutes=1), 3600)
        after = _db_now(migrated_postgres_engine)
    assert response.status_code == 200, response.text
    [bucket] = response.json()["buckets"]
    occupancy = bucket["allocation_occupancy_seconds"]
    # The open end is the DB statement time, so the bounds are DB clock readings (the Docker VM
    # clock drifts tens of ms from the host). Instants are floored to the millisecond (B18-R26):
    # allow one millisecond each side.
    lower = (before - started).total_seconds() - 0.001
    assert lower <= occupancy <= (after - started).total_seconds() + 0.001
    assert bucket["normalized_service"] == pytest.approx(occupancy / 4, rel=1e-12)
    assert bucket["dominant_resource_time_seconds"] == pytest.approx(occupancy / 2, rel=1e-12)
    assert bucket["weight"] == 2.0


def test_normalized_service_equals_the_charged_ledger_amount(migrated_postgres_engine, tmp_path):
    """Σ normalized over a range that fully contains closed segments = Σ charged_amount.

    The segments are the ones coordinator accounting wrote and charged, with their
    microsecond instants unchanged (B18-RV01)."""
    engine = migrated_postgres_engine
    seed_dispatchable(engine, count=2)
    with engine.begin() as connection:
        connection.execute(
            update(s.tenant_policies).values(
                tenant_active_limit=2, user_active_limit=2, weight=Decimal(3)
            )
        )
    service = CoordinatorService(create_session_factory(engine))
    epoch = service.acquire()
    for _ in range(4):
        if isinstance(service.tick(epoch), NoDecision):
            break
    sessions = create_session_factory(engine)
    for _ in range(2):
        with sessions.begin() as session:
            rebase_locked(session, account_now_locked(session))
    with engine.connect() as connection:
        closed = (
            connection.execute(
                select(s.allocation_ledger_segments).where(
                    s.allocation_ledger_segments.c.ended_at.is_not(None)
                )
            )
            .mappings()
            .all()
        )
    assert len(closed) >= 2
    # The coordinator's own instants, untouched: they carry sub-millisecond parts, which
    # the report must floor exactly like accounting (B18-R26), not integrate in µs.
    instants = [row[key] for row in closed for key in ("started_at", "ended_at")]
    assert any(instant.microsecond % 1000 for instant in instants), instants
    from_at = min(row["started_at"] for row in closed)
    to_at = max(row["ended_at"] for row in closed)
    charged: dict[str, Decimal] = {}
    for row in closed:
        charged[str(row["tenant_id"])] = (
            charged.get(str(row["tenant_id"]), Decimal(0)) + (row["charged_amount"])
        )
    assert sum(charged.values()) > 0
    with admin_session(engine, tmp_path) as (client, _):
        response = _query(client, from_at, to_at, 1)
    assert response.status_code == 200, response.text
    normalized: dict[str, float] = {}
    for bucket in response.json()["buckets"]:
        normalized[bucket["tenant_id"]] = (
            normalized.get(bucket["tenant_id"], 0.0) + bucket["normalized_service"]
        )
    _assert_close(normalized, {tenant: float(amount) for tenant, amount in charged.items()})


def test_validation_audit_and_authorization(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with admin_session(engine, tmp_path) as (client, write):
        day31 = FROM + timedelta(days=31)
        assert _query(client, FROM, day31, 86_400).status_code == 200
        for from_at, to_at, bucket_seconds in [
            (FROM, FROM, 60),
            (FROM, FROM - timedelta(seconds=1), 60),
            (FROM, day31 + timedelta(milliseconds=1), 86_400),
            (FROM, FROM + timedelta(hours=1), 0),
            (FROM, FROM + timedelta(days=2), 86_401),
            (FROM, FROM + timedelta(seconds=1001), 1),
        ]:
            response = _query(client, from_at, to_at, bucket_seconds)
            assert response.status_code == 400, (from_at, to_at, bucket_seconds, response.text)
            assert response.json()["code"] == "validation_failed"
        naive = client.get(
            "/v1/admin/fairness",
            params={
                "from": "2026-09-01T00:00:00",
                "to": "2026-09-01T01:00:00",
                "bucket_seconds": 60,
            },
        )
        assert naive.status_code == 400
        assert client.get("/v1/admin/fairness", params={"bucket_seconds": 60}).status_code == 400
        bad_tenant = _query(client, FROM, FROM + timedelta(hours=1), 60, tenant_id="nope")
        assert bad_tenant.status_code == 400

        audits = audit_rows(engine, "admin.fairness.query")
        assert len(audits) == 1
        assert (audits[0]["actor_type"], audits[0]["target_id"]) == ("ADMIN", "all")

        with member_session(engine, tmp_path, client, write) as member:
            assert _query(member, FROM, FROM + timedelta(hours=1), 60).status_code == 403
        client.cookies.clear()
        assert _query(client, FROM, FROM + timedelta(hours=1), 60).status_code == 401
    assert len(audit_rows(engine, "admin.fairness.query")) == 1


def test_more_than_1000_tenant_buckets_is_rejected(migrated_postgres_engine, tmp_path):
    allocations = _seed_allocations(migrated_postgres_engine, tenants=2, per_tenant=1)
    pairs = [
        (
            ids[0],
            LedgerSegment(
                segment_id=new_uuid7(),
                tenant_id=tenant,
                started_at=FROM,
                ended_at=FROM + timedelta(seconds=501),
                dominant_share=Decimal("0.5"),
                weight=Decimal(1),
            ),
        )
        for tenant, ids in allocations.items()
    ]
    _insert_segments(migrated_postgres_engine, pairs)
    with admin_session(migrated_postgres_engine, tmp_path) as (client, _):
        ok = _query(client, FROM, FROM + timedelta(seconds=500), 1)
        assert ok.status_code == 200, ok.text
        assert len(ok.json()["buckets"]) == 1000
        over = _query(client, FROM, FROM + timedelta(seconds=501), 1)
        assert over.status_code == 400, over.text
        assert over.json()["code"] == "validation_failed"


def test_query_is_a_plain_read_that_does_not_block_accounting(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    seed_dispatchable(engine, count=1)
    service = CoordinatorService(create_session_factory(engine))
    service.tick(service.acquire())
    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).where(s.allocation_ledger_segments.c.ended_at.is_(None))
        ).scalar_one()
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with admin_session(engine, tmp_path) as (client, _):
            now = datetime.now(UTC)
            statements.clear()
            response = _query(client, now - timedelta(hours=1), now + timedelta(hours=1), 3600)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    assert response.json()["buckets"]
    fairness = [sql for sql in statements if "allocation_ledger_segments" in sql]
    assert fairness and not any("FOR UPDATE" in sql.upper() for sql in fairness)
    writes = {
        sql.split()[1 if sql.lstrip().upper().startswith("UPDATE") else 2]
        for sql in statements
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    }
    # Only the audit row; browser_sessions.last_seen_at is the authentication touch.
    assert writes <= {"audit_records", "browser_sessions"}, writes
    assert "audit_records" in writes

    # A reader holding its snapshot open must not delay the accounting row locks.
    from nexa.application.admin_queries import fairness_rows

    sessions = create_session_factory(engine)
    reader = sessions()
    released = threading.Event()
    try:
        now = datetime.now(UTC)
        assert fairness_rows(
            reader,
            from_at=now - timedelta(hours=1),
            to_at=now + timedelta(hours=1),
            bucket_seconds=3600,
            tenant_id=None,
        )
        with sessions.begin() as writer:
            writer.execute(text("SET LOCAL lock_timeout = '2s'"))
            rebase_locked(writer, account_now_locked(writer))
        released.set()
    finally:
        reader.rollback()
        reader.close()
    assert released.is_set()
