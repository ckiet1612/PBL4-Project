"""B18 fairness report: parameter bounds, bucket geometry and the reference aggregate.

The PostgreSQL aggregate (tests/integration/test_admin_fairness_b18.py) is compared
against aggregate_fairness, so the reference model is pinned here first.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nexa.application.errors import ApplicationError
from nexa.application.fairness_report import (
    LedgerSegment,
    aggregate_fairness,
    fairness_bucket_bounds,
    fairness_bucket_count,
    fairness_report,
)
from nexa.coordinator.accounting import epoch_ms

FROM = datetime(2026, 9, 1, tzinfo=UTC)
TENANT_A = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0a1")
TENANT_B = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0b2")


def _segment(index, tenant, start, end, share="0.5", weight="1"):
    return LedgerSegment(
        segment_id=UUID(int=index),
        tenant_id=tenant,
        started_at=start,
        ended_at=end,
        dominant_share=Decimal(share),
        weight=Decimal(weight),
    )


def _rejects(from_at, to_at, bucket_seconds):
    with pytest.raises(ApplicationError) as raised:
        fairness_bucket_count(from_at, to_at, bucket_seconds)
    assert raised.value.status == 400
    assert raised.value.code == "validation_failed"


def test_range_must_be_positive_and_at_most_31_days() -> None:
    _rejects(FROM, FROM, 60)
    _rejects(FROM, FROM - timedelta(seconds=1), 60)
    assert fairness_bucket_count(FROM, FROM + timedelta(days=31), 86_400) == 31
    _rejects(FROM, FROM + timedelta(days=31, microseconds=1), 86_400)


def test_bucket_seconds_bounds_and_bucket_count_limit() -> None:
    _rejects(FROM, FROM + timedelta(hours=1), 0)
    _rejects(FROM, FROM + timedelta(days=2), 86_401)
    assert fairness_bucket_count(FROM, FROM + timedelta(seconds=1000), 1) == 1000
    _rejects(FROM, FROM + timedelta(seconds=1001), 1)
    # ceil: a partial last bucket counts.
    assert fairness_bucket_count(FROM, FROM + timedelta(seconds=61), 60) == 2
    _rejects(FROM, FROM + timedelta(seconds=60_000, microseconds=1), 60)


def test_timestamps_must_carry_a_timezone() -> None:
    _rejects(FROM.replace(tzinfo=None), FROM.replace(tzinfo=None) + timedelta(hours=1), 60)


def test_bucket_bounds_are_half_open_and_the_last_bucket_stops_at_to() -> None:
    to_at = FROM + timedelta(seconds=150)
    assert fairness_bucket_bounds(FROM, to_at, 60, 0) == (FROM, FROM + timedelta(seconds=60))
    assert fairness_bucket_bounds(FROM, to_at, 60, 2) == (FROM + timedelta(seconds=120), to_at)


def test_overlap_is_split_across_buckets_and_clipped_to_the_range() -> None:
    to_at = FROM + timedelta(minutes=3)
    segments = [
        # 30 s before the range, 90 s inside: 60 s in bucket 0, 30 s in bucket 1.
        _segment(1, TENANT_A, FROM - timedelta(seconds=30), FROM + timedelta(seconds=90)),
        # Entirely before the range: ignored.
        _segment(2, TENANT_A, FROM - timedelta(minutes=5), FROM - timedelta(minutes=1)),
    ]
    rows = aggregate_fairness(segments, from_at=FROM, to_at=to_at, bucket_seconds=60, now=to_at)
    assert [(row.bucket_index, row.tenant_id) for row in rows] == [(0, TENANT_A), (1, TENANT_A)]
    assert rows[0].occupancy == Decimal(60)
    assert rows[0].dominant_resource_time == Decimal(30)
    assert rows[0].normalized_service == Decimal(30)
    assert rows[1].occupancy == Decimal(30)
    assert rows[1].weight == Decimal(1)


def test_open_segment_ends_at_statement_time() -> None:
    to_at = FROM + timedelta(hours=1)
    now = FROM + timedelta(seconds=90, microseconds=2_750)
    segments = [_segment(1, TENANT_B, FROM + timedelta(seconds=30), None, share="1", weight="2")]
    rows = aggregate_fairness(segments, from_at=FROM, to_at=to_at, bucket_seconds=60, now=now)
    assert [row.bucket_index for row in rows] == [0, 1]
    # The statement time is floored to the millisecond, like the ledger charge.
    assert rows[1].occupancy == Decimal("30.002")
    assert rows[1].normalized_service == Decimal("15.001")
    assert rows[1].weight == Decimal(2)


def test_instants_are_floored_to_the_millisecond_like_the_ledger_charge() -> None:
    """B18-R26: overlap uses epoch_ms (coordinator accounting), not microseconds."""
    from_at = FROM + timedelta(microseconds=700)
    to_at = from_at + timedelta(seconds=2)
    segments = [
        # [0.0009, 1.0011) s after FROM → floored [0.000, 1.001) → bucket 0 gets 1.000 s
        # (bucket 0 is [0.000, 1.000) after flooring from_at), bucket 1 gets 0.001 s.
        _segment(
            1,
            TENANT_A,
            FROM + timedelta(microseconds=900),
            FROM + timedelta(seconds=1, microseconds=1_100),
            share="1",
            weight="1",
        ),
        # Sub-millisecond segment inside one millisecond: no charge, no row.
        _segment(
            2,
            TENANT_B,
            FROM + timedelta(microseconds=1_100),
            FROM + timedelta(microseconds=1_900),
            share="1",
            weight="1",
        ),
    ]
    rows = aggregate_fairness(segments, from_at=from_at, to_at=to_at, bucket_seconds=1, now=to_at)
    assert [(row.bucket_index, row.tenant_id, row.occupancy) for row in rows] == [
        (0, TENANT_A, Decimal("1.000")),
        (1, TENANT_A, Decimal("0.001")),
    ]


def test_weight_is_service_weighted_and_falls_back_for_zero_share() -> None:
    to_at = FROM + timedelta(minutes=1)
    mixed = [
        _segment(1, TENANT_A, FROM, FROM + timedelta(seconds=30), share="0.5", weight="1"),
        _segment(2, TENANT_A, FROM + timedelta(seconds=30), to_at, share="0.5", weight="3"),
    ]
    [row] = aggregate_fairness(mixed, from_at=FROM, to_at=to_at, bucket_seconds=60, now=to_at)
    assert row.dominant_resource_time == Decimal(30)
    assert row.normalized_service == Decimal(15) + Decimal(5)
    assert row.weight == Decimal("1.5")
    zero = [
        _segment(3, TENANT_B, FROM, FROM + timedelta(seconds=10), share="0", weight="2"),
        _segment(4, TENANT_B, FROM + timedelta(seconds=20), to_at, share="0", weight="4"),
    ]
    [row] = aggregate_fairness(zero, from_at=FROM, to_at=to_at, bucket_seconds=60, now=to_at)
    assert row.normalized_service == 0
    assert row.occupancy == Decimal(50)
    assert row.weight == Decimal(4)


def test_rows_are_sparse_and_sorted_by_bucket_then_tenant() -> None:
    to_at = FROM + timedelta(minutes=5)
    segments = [
        _segment(1, TENANT_B, FROM + timedelta(minutes=3), FROM + timedelta(minutes=4)),
        _segment(2, TENANT_A, FROM + timedelta(minutes=3), FROM + timedelta(minutes=4)),
        _segment(3, TENANT_B, FROM, FROM + timedelta(seconds=1)),
    ]
    rows = aggregate_fairness(segments, from_at=FROM, to_at=to_at, bucket_seconds=60, now=to_at)
    assert [(row.bucket_index, row.tenant_id) for row in rows] == [
        (0, TENANT_B),
        (3, TENANT_A),
        (3, TENANT_B),
    ]


def test_report_serializes_numbers_and_rejects_more_than_1000_rows() -> None:
    to_at = FROM + timedelta(minutes=2)
    segments = [_segment(1, TENANT_A, FROM, FROM + timedelta(seconds=90), share="0.25")]
    rows = aggregate_fairness(segments, from_at=FROM, to_at=to_at, bucket_seconds=60, now=to_at)
    report = fairness_report(rows, from_at=FROM, to_at=to_at, bucket_seconds=60)
    assert report == {
        "from": "2026-09-01T00:00:00.000Z",
        "to": "2026-09-01T00:02:00.000Z",
        "bucket_seconds": 60,
        "buckets": [
            {
                "tenant_id": str(TENANT_A),
                "start_at": "2026-09-01T00:00:00.000Z",
                "end_at": "2026-09-01T00:01:00.000Z",
                "weight": 1.0,
                "dominant_resource_time_seconds": 15.0,
                "normalized_service": 15.0,
                "allocation_occupancy_seconds": 60.0,
            },
            {
                "tenant_id": str(TENANT_A),
                "start_at": "2026-09-01T00:01:00.000Z",
                "end_at": "2026-09-01T00:02:00.000Z",
                "weight": 1.0,
                "dominant_resource_time_seconds": 7.5,
                "normalized_service": 7.5,
                "allocation_occupancy_seconds": 30.0,
            },
        ],
    }
    with pytest.raises(ApplicationError) as raised:
        fairness_report(rows * 501, from_at=FROM, to_at=to_at, bucket_seconds=60)
    assert raised.value.status == 400


# Microsecond offsets: real coordinator segments carry sub-millisecond instants.
_US = st.integers(min_value=0, max_value=3_600_000_000)


@settings(max_examples=300, deadline=None)
@given(
    spans=st.lists(
        st.tuples(
            st.sampled_from([TENANT_A, TENANT_B]),
            _US,
            _US,
            st.sampled_from(["0", "0.125", "0.3333333333", "1"]),
            st.sampled_from(["0.5", "1", "3", "7.25"]),
        ),
        max_size=12,
    ),
    bucket_seconds=st.sampled_from([4, 7, 60, 600, 3600]),
    from_offset_us=st.integers(min_value=0, max_value=999),
)
def test_closed_range_totals_match_the_ledger_charge(spans, bucket_seconds, from_offset_us) -> None:
    """Σ normalized over the range = Σ charged_amount, which accounting computes as
    share × (epoch_ms(end) − epoch_ms(start)) / 1000 / weight (B18-R26)."""
    from_at = FROM + timedelta(microseconds=from_offset_us)
    to_at = FROM + timedelta(hours=1, microseconds=1_000)
    segments = []
    expected_normalized: dict[UUID, Decimal] = {}
    expected_occupancy: dict[UUID, Decimal] = {}
    for index, (tenant, a, b, share, weight) in enumerate(spans):
        start, end = sorted((a, b))
        started_at = max(FROM + timedelta(microseconds=start), from_at)
        ended_at = max(FROM + timedelta(microseconds=end), started_at)
        segment = _segment(index, tenant, started_at, ended_at, share=share, weight=weight)
        segments.append(segment)
        duration = Decimal(epoch_ms(ended_at) - epoch_ms(started_at)) / 1000
        with localcontext() as context:
            context.prec = 50
            expected_normalized[tenant] = expected_normalized.get(tenant, Decimal(0)) + (
                Decimal(share) * duration / Decimal(weight)
            )
        expected_occupancy[tenant] = expected_occupancy.get(tenant, Decimal(0)) + duration
    rows = aggregate_fairness(
        segments, from_at=from_at, to_at=to_at, bucket_seconds=bucket_seconds, now=to_at
    )
    for tenant in (TENANT_A, TENANT_B):
        normalized = sum(
            (row.normalized_service for row in rows if row.tenant_id == tenant), Decimal(0)
        )
        occupancy = sum((row.occupancy for row in rows if row.tenant_id == tenant), Decimal(0))
        expected = expected_normalized.get(tenant, Decimal(0))
        assert abs(normalized - expected) <= Decimal("1e-20") * max(expected, Decimal(1))
        assert occupancy == expected_occupancy.get(tenant, Decimal(0))
    assert all(row.occupancy > 0 for row in rows)
    assert [(row.bucket_index, row.tenant_id) for row in rows] == sorted(
        (row.bucket_index, row.tenant_id) for row in rows
    )
