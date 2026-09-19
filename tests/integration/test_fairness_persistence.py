import hashlib
from datetime import UTC, datetime
from decimal import MAX_EMAX, MIN_ETINY, Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from nexa.domain.scheduling import (
    Candidate,
    CandidateWindow,
    Dispatch,
    Ok,
    ResourceCapacity,
    ResourceRequest,
    SchedulingSnapshot,
    TenantLedger,
    TenantPolicySnapshot,
)
from nexa.infrastructure.persistence.schema import (
    fairness_ledgers,
    fairness_state,
    policy_versions,
    rate_buckets,
    tenant_policies,
)
from nexa.scheduler.policy import WeightedDominantResourceTimePolicy

from ._factories import seed_tenant_graph

pytestmark = pytest.mark.postgres

ONE_THIRD_50 = Decimal("0.33333333333333333333333333333333333333333333333333")
CLOSE_LOW = Decimal("1.0000000000000000000000000000000000000000000000001")
CLOSE_HIGH = Decimal("1.0000000000000000000000000000000000000000000000002")
LARGE_EXACT = Decimal("9" * 1000 + "." + "1" * 200)
TINY_EXACT = Decimal("1E-20000")
TINY_NEXT = Decimal("2E-20000")
_LONG_RANDOM_DIGITS = "".join(
    str(int(digit, 16) % 10)
    for digit in hashlib.shake_256(b"nexa-b05-r06-significand").hexdigest(2500)
)
LONG_SIGNIFICAND_LOW = Decimal("7" + _LONG_RANDOM_DIGITS[1:4999] + "1")
LONG_SIGNIFICAND_HIGH = Decimal("7" + _LONG_RANDOM_DIGITS[1:4999] + "2")


def _insert_policy(connection, tenant_id, *, version: int, weight: Decimal) -> None:
    connection.execute(
        tenant_policies.insert(),
        {
            "tenant_id": tenant_id,
            "version": version,
            "weight": weight,
            "cpu_limit_millis": 8_000,
            "memory_limit_bytes": 16 * 1024**3,
            "gpu_limit": 1,
            "outstanding_limit": 2_000,
            "user_outstanding_limit": 2_000,
            "tenant_active_limit": 2,
            "user_active_limit": 1,
            "tenant_rate_per_second": Decimal("5"),
            "tenant_rate_burst": Decimal("20"),
            "user_rate_per_second": Decimal("2"),
            "user_rate_burst": Decimal("10"),
            "is_current": True,
        },
    )


def _insert_policy_with_outstanding_limits(
    connection,
    tenant_id,
    *,
    outstanding_limit: int,
    user_outstanding_limit: int,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO tenant_policies (
                tenant_id,
                version,
                weight,
                cpu_limit_millis,
                memory_limit_bytes,
                gpu_limit,
                outstanding_limit,
                user_outstanding_limit,
                tenant_active_limit,
                user_active_limit,
                tenant_rate_per_second,
                tenant_rate_burst,
                user_rate_per_second,
                user_rate_burst,
                is_current
            ) VALUES (
                :tenant_id,
                1,
                '0:1:0',
                8000,
                17179869184,
                1,
                :outstanding_limit,
                :user_outstanding_limit,
                2,
                1,
                '0:5:0',
                '0:20:0',
                '0:2:0',
                '0:10:0',
                true
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "outstanding_limit": outstanding_limit,
            "user_outstanding_limit": user_outstanding_limit,
        },
    )


def _candidate(job_id: str, tenant_id: str, sequence: int) -> Candidate:
    return Candidate(
        job_id=job_id,
        tenant_id=tenant_id,
        user_id=f"user-{tenant_id}",
        job_version=1,
        ready_sequence=sequence,
        resources=ResourceRequest(1000, 1024, 0),
        base_priority=1,
        eligible_wait_seconds=0,
        retry_ready_at_ms=0,
        template_version=1,
        required_capabilities=frozenset({"cpu"}),
        tenant_active_attempts=0,
        user_active_attempts=0,
    )


def _snapshot(rows: list[tuple[str, Decimal, Decimal]]) -> SchedulingSnapshot:
    windows = []
    ledgers = []
    limits = []
    for sequence, (tenant_id, score, weight) in enumerate(rows, start=1):
        candidate = _candidate(f"job-{tenant_id}", tenant_id, sequence)
        windows.append((tenant_id, CandidateWindow((candidate,), candidate, None)))
        ledgers.append(TenantLedger(tenant_id, score, 0, True))
        limits.append(
            TenantPolicySnapshot(
                tenant_id=tenant_id,
                weight=weight,
                resource_quota=ResourceCapacity(8_000, 16 * 1024**3, 1),
                max_active_attempts=2,
                max_user_active_attempts=1,
            )
        )
    return SchedulingSnapshot(
        policy_version=1,
        allocatable_capacity=ResourceCapacity(8_000, 16 * 1024**3, 1),
        held_allocations=(),
        tenant_ledgers=tuple(ledgers),
        tenant_limits=tuple(limits),
        candidates_by_tenant=tuple(windows),
        active_reservation=None,
        virtual_floor=ONE_THIRD_50,
    )


def test_tenant_policy_persists_independent_outstanding_limits(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="outstanding-limits")
        _insert_policy_with_outstanding_limits(
            connection,
            graph["tenant_id"],
            outstanding_limit=2_000,
            user_outstanding_limit=37,
        )

    with migrated_postgres_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT outstanding_limit, user_outstanding_limit "
                "FROM tenant_policies WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": graph["tenant_id"]},
        ).one()

    assert row.outstanding_limit == 2_000
    assert row.user_outstanding_limit == 37


def test_tenant_policy_rejects_zero_user_outstanding_limit(
    migrated_postgres_engine,
) -> None:
    with (
        pytest.raises(IntegrityError, match="user_outstanding_limit"),
        migrated_postgres_engine.begin() as connection,
    ):
        graph = seed_tenant_graph(connection, label="invalid-user-outstanding")
        _insert_policy_with_outstanding_limits(
            connection,
            graph["tenant_id"],
            outstanding_limit=2_000,
            user_outstanding_limit=0,
        )


def test_decimal_scores_weights_and_virtual_floor_round_trip_exactly(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graphs = [seed_tenant_graph(connection, label=f"decimal-{index}") for index in range(4)]
        connection.execute(
            policy_versions.insert(),
            {
                "policy_version": 1,
                "global_outstanding_limit": 100_000,
                "operational_mode": "NORMAL",
                "is_current": True,
            },
        )
        connection.execute(
            fairness_state.insert(),
            {"singleton_key": "local", "virtual_floor": ONE_THIRD_50, "version": 1},
        )
        for graph, score in zip(
            graphs,
            (ONE_THIRD_50, CLOSE_LOW, CLOSE_HIGH, LARGE_EXACT),
            strict=True,
        ):
            _insert_policy(connection, graph["tenant_id"], version=1, weight=ONE_THIRD_50)
            connection.execute(
                fairness_ledgers.insert(),
                {
                    "tenant_id": graph["tenant_id"],
                    "virtual_score": score,
                    "accounted_through": datetime(2026, 1, 1, tzinfo=UTC),
                    "had_eligible_demand": True,
                    "version": 1,
                },
            )

    with migrated_postgres_engine.connect() as connection:
        floor = connection.execute(select(fairness_state.c.virtual_floor)).scalar_one()
        stored = (
            connection.execute(
                select(fairness_ledgers.c.virtual_score).order_by(
                    func.nexa_decimal_zero_rank(fairness_ledgers.c.virtual_score),
                    func.nexa_decimal_adjusted_exponent(fairness_ledgers.c.virtual_score),
                    func.nexa_decimal_normalized_significand(
                        fairness_ledgers.c.virtual_score
                    ).collate("C"),
                )
            )
            .scalars()
            .all()
        )
        weights = (
            connection.execute(
                select(tenant_policies.c.weight).order_by(tenant_policies.c.tenant_id)
            )
            .scalars()
            .all()
        )

    assert floor.as_tuple() == ONE_THIRD_50.as_tuple()
    assert [value.as_tuple() for value in stored] == [
        value.as_tuple() for value in (ONE_THIRD_50, CLOSE_LOW, CLOSE_HIGH, LARGE_EXACT)
    ]
    assert all(weight.as_tuple() == ONE_THIRD_50.as_tuple() for weight in weights)


def test_extreme_exponent_round_trips_for_weight_score_and_floor(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="extreme-exponent")
        _insert_policy(connection, graph["tenant_id"], version=1, weight=TINY_EXACT)
        connection.execute(
            fairness_state.insert(),
            {"singleton_key": "local", "virtual_floor": TINY_EXACT, "version": 1},
        )
        connection.execute(
            fairness_ledgers.insert(),
            {
                "tenant_id": graph["tenant_id"],
                "virtual_score": TINY_NEXT,
                "accounted_through": datetime(2026, 1, 1, tzinfo=UTC),
                "had_eligible_demand": True,
                "version": 1,
            },
        )

    with migrated_postgres_engine.connect() as connection:
        stored_weight = connection.execute(select(tenant_policies.c.weight)).scalar_one()
        stored_score = connection.execute(select(fairness_ledgers.c.virtual_score)).scalar_one()
        stored_floor = connection.execute(select(fairness_state.c.virtual_floor)).scalar_one()

    assert stored_weight.as_tuple() == TINY_EXACT.as_tuple()
    assert stored_score.as_tuple() == TINY_NEXT.as_tuple()
    assert stored_floor.as_tuple() == TINY_EXACT.as_tuple()


def test_long_uncompressible_scores_round_trip_and_order_exactly(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        low = seed_tenant_graph(connection, label="long-significand-low")
        high = seed_tenant_graph(connection, label="long-significand-high")
        for graph, score in ((low, LONG_SIGNIFICAND_LOW), (high, LONG_SIGNIFICAND_HIGH)):
            connection.execute(
                fairness_ledgers.insert(),
                {
                    "tenant_id": graph["tenant_id"],
                    "virtual_score": score,
                    "accounted_through": datetime(2026, 1, 1, tzinfo=UTC),
                    "had_eligible_demand": True,
                    "version": 1,
                },
            )

    with migrated_postgres_engine.connect() as connection:
        stored = (
            connection.execute(
                select(fairness_ledgers.c.virtual_score).order_by(
                    func.nexa_decimal_zero_rank(fairness_ledgers.c.virtual_score),
                    func.nexa_decimal_adjusted_exponent(fairness_ledgers.c.virtual_score),
                    func.nexa_decimal_normalized_significand(
                        fairness_ledgers.c.virtual_score
                    ).collate("C"),
                )
            )
            .scalars()
            .all()
        )

    assert [value.as_tuple() for value in stored] == [
        LONG_SIGNIFICAND_LOW.as_tuple(),
        LONG_SIGNIFICAND_HIGH.as_tuple(),
    ]


def test_database_rejects_exponent_outside_python_decimal_domain(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="invalid-decimal-exponent")

    with (
        pytest.raises(IntegrityError, match="virtual_score_finite_nonnegative"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO fairness_ledgers "
                "(tenant_id, virtual_score, accounted_through, had_eligible_demand, version) "
                "VALUES (:tenant_id, '0:1:1000000000000000000', now(), true, 1)"
            ),
            {"tenant_id": graph["tenant_id"]},
        )


@pytest.mark.parametrize(
    ("encoded", "expected"),
    [
        (f"0:1:{MAX_EMAX}", True),
        (f"0:12:{MAX_EMAX - 1}", True),
        (f"0:12:{MAX_EMAX}", False),
        (f"0:10:{MAX_EMAX - 1}", True),
        (f"0:10:{MAX_EMAX}", False),
        (f"0:0:{MAX_EMAX}", True),
        (f"0:1:{MIN_ETINY}", True),
    ],
)
def test_database_decimal_validator_matches_python_decimal_domain(
    migrated_postgres_engine,
    encoded: str,
    expected: bool,
) -> None:
    with migrated_postgres_engine.connect() as connection:
        actual = connection.execute(select(func.nexa_decimal_is_valid(encoded))).scalar_one()

    assert actual is expected


def test_database_rejects_adjusted_exponent_above_python_decimal_domain(
    migrated_postgres_engine,
) -> None:
    with (
        pytest.raises(IntegrityError, match="virtual_floor_finite_nonnegative"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO fairness_state (singleton_key, virtual_floor, version) "
                f"VALUES ('local', '0:12:{MAX_EMAX}', 1)"
            )
        )


def test_rate_bucket_compares_extreme_exponents_exactly(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            rate_buckets.insert(),
            {
                "scope_type": "TENANT",
                "scope_id": "valid-extreme-rate",
                "tokens": TINY_EXACT,
                "capacity": TINY_NEXT,
                "refill_rate": TINY_EXACT,
                "last_refill_at": datetime.now(UTC),
                "version": 1,
            },
        )

    with (
        pytest.raises(IntegrityError, match="tokens_within_capacity"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            rate_buckets.insert(),
            {
                "scope_type": "TENANT",
                "scope_id": "invalid-extreme-rate",
                "tokens": TINY_NEXT,
                "capacity": TINY_EXACT,
                "refill_rate": TINY_EXACT,
                "last_refill_at": datetime.now(UTC),
                "version": 1,
            },
        )


@pytest.mark.parametrize("invalid", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_fairness_ledger_rejects_invalid_numeric_values(
    migrated_postgres_engine, invalid: Decimal
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label=f"invalid-{str(invalid).lower()}")
    with (
        pytest.raises(
            (IntegrityError, StatementError), match="finite and non-negative|virtual_score"
        ),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            fairness_ledgers.insert(),
            {
                "tenant_id": graph["tenant_id"],
                "virtual_score": invalid,
                "accounted_through": datetime.now(UTC),
                "had_eligible_demand": True,
                "version": 1,
            },
        )


def test_policy_selection_is_unchanged_after_decimal_persistence(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        low = seed_tenant_graph(connection, label="policy-low")
        high = seed_tenant_graph(connection, label="policy-high")
        for graph, score in ((low, CLOSE_LOW), (high, CLOSE_HIGH)):
            _insert_policy(connection, graph["tenant_id"], version=1, weight=Decimal("1"))
            connection.execute(
                fairness_ledgers.insert(),
                {
                    "tenant_id": graph["tenant_id"],
                    "virtual_score": score,
                    "accounted_through": datetime(1970, 1, 1, tzinfo=UTC),
                    "had_eligible_demand": True,
                    "version": 1,
                },
            )

    source_rows = [
        (str(low["tenant_id"]), CLOSE_LOW, Decimal("1")),
        (str(high["tenant_id"]), CLOSE_HIGH, Decimal("1")),
    ]
    before = WeightedDominantResourceTimePolicy().decide(_snapshot(source_rows), now_ms=0)

    with migrated_postgres_engine.connect() as connection:
        persisted = connection.execute(
            select(
                fairness_ledgers.c.tenant_id,
                fairness_ledgers.c.virtual_score,
                tenant_policies.c.weight,
            ).join(
                tenant_policies,
                (tenant_policies.c.tenant_id == fairness_ledgers.c.tenant_id)
                & tenant_policies.c.is_current,
            )
        ).all()
    persisted_rows = [(str(row.tenant_id), row.virtual_score, row.weight) for row in persisted]
    after = WeightedDominantResourceTimePolicy().decide(_snapshot(persisted_rows), now_ms=0)

    assert isinstance(before, Ok) and isinstance(before.value, Dispatch)
    assert isinstance(after, Ok) and isinstance(after.value, Dispatch)
    assert before.value.job_id == after.value.job_id == f"job-{low['tenant_id']}"


def test_policy_selection_with_extreme_exponents_is_unchanged_after_persistence(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        low = seed_tenant_graph(connection, label="policy-tiny-low")
        high = seed_tenant_graph(connection, label="policy-tiny-high")
        for graph, score in ((low, TINY_EXACT), (high, TINY_NEXT)):
            _insert_policy(connection, graph["tenant_id"], version=1, weight=Decimal("1"))
            connection.execute(
                fairness_ledgers.insert(),
                {
                    "tenant_id": graph["tenant_id"],
                    "virtual_score": score,
                    "accounted_through": datetime(1970, 1, 1, tzinfo=UTC),
                    "had_eligible_demand": True,
                    "version": 1,
                },
            )

    source_rows = [
        (str(low["tenant_id"]), TINY_EXACT, Decimal("1")),
        (str(high["tenant_id"]), TINY_NEXT, Decimal("1")),
    ]
    before = WeightedDominantResourceTimePolicy().decide(_snapshot(source_rows), now_ms=0)

    with migrated_postgres_engine.connect() as connection:
        persisted = connection.execute(
            select(
                fairness_ledgers.c.tenant_id,
                fairness_ledgers.c.virtual_score,
                tenant_policies.c.weight,
            ).join(
                tenant_policies,
                (tenant_policies.c.tenant_id == fairness_ledgers.c.tenant_id)
                & tenant_policies.c.is_current,
            )
        ).all()
    persisted_rows = [(str(row.tenant_id), row.virtual_score, row.weight) for row in persisted]
    after = WeightedDominantResourceTimePolicy().decide(_snapshot(persisted_rows), now_ms=0)

    assert isinstance(before, Ok) and isinstance(before.value, Dispatch)
    assert isinstance(after, Ok) and isinstance(after.value, Dispatch)
    assert before.value.job_id == after.value.job_id == f"job-{low['tenant_id']}"
