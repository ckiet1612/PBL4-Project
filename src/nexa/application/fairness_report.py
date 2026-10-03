"""Fairness report (adminQueryFairness): parameter bounds, buckets and reference aggregate.

Buckets are half-open ``[from + k·b, min(from + (k+1)·b, to))``. A ledger segment
contributes its overlap with each bucket; an open segment ends at the database
statement time. Every instant (segment ends, bucket ends, statement time) is floored
to the whole millisecond with ``epoch_ms`` before the overlap is taken, exactly as
coordinator accounting charges ``epoch_ms(end) − epoch_ms(start)`` (B18-R26). Bucket
ends are ``from`` plus whole seconds, so flooring keeps them contiguous and a closed
segment inside the range contributes exactly its charged interval. Per (tenant, bucket):

* dominant resource time = Σ share × overlap
* normalized service = Σ share × overlap / weight
* allocation occupancy = Σ overlap
* weight = DRT / normalized, or the weight of the latest overlapping segment when
  the normalized service is zero.

The service computes the same aggregate in PostgreSQL; ``aggregate_fairness`` is the
reference model it is tested against.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from nexa.application.errors import ApplicationError
from nexa.application.json_codec import json_wire_value
from nexa.coordinator.accounting import epoch_ms

MAX_FAIRNESS_RANGE = timedelta(days=31)
MAX_FAIRNESS_BUCKET_SECONDS = 86_400
MAX_FAIRNESS_BUCKETS = 1_000
_PRECISION = 50


@dataclass(frozen=True, slots=True)
class LedgerSegment:
    segment_id: UUID
    tenant_id: UUID
    started_at: datetime
    ended_at: datetime | None
    dominant_share: Decimal
    weight: Decimal


@dataclass(frozen=True, slots=True)
class FairnessRow:
    bucket_index: int
    tenant_id: UUID
    weight: Decimal
    dominant_resource_time: Decimal
    normalized_service: Decimal
    occupancy: Decimal


def _invalid(message: str) -> ApplicationError:
    return ApplicationError(code="validation_failed", status=400, message=message)


def fairness_bucket_count(from_at: datetime, to_at: datetime, bucket_seconds: int) -> int:
    """Validate the query and return ceil(range / bucket_seconds)."""
    for value in (from_at, to_at):
        if value.tzinfo is None or value.utcoffset() is None:
            raise _invalid("Fairness timestamps must include a timezone")
    if to_at <= from_at:
        raise _invalid("Fairness range end must be after its start")
    if to_at - from_at > MAX_FAIRNESS_RANGE:
        raise _invalid("Fairness range must not exceed 31 days")
    if not 1 <= bucket_seconds <= MAX_FAIRNESS_BUCKET_SECONDS:
        raise _invalid("Fairness bucket_seconds must be between 1 and 86400")
    count = math.ceil((to_at - from_at) / timedelta(seconds=bucket_seconds))
    if count > MAX_FAIRNESS_BUCKETS:
        raise _invalid("Fairness query must not produce more than 1000 buckets")
    return count


def fairness_bucket_bounds(
    from_at: datetime, to_at: datetime, bucket_seconds: int, index: int
) -> tuple[datetime, datetime]:
    start = from_at + timedelta(seconds=bucket_seconds * index)
    return start, min(start + timedelta(seconds=bucket_seconds), to_at)


def _overlap_seconds(start: datetime, end: datetime, low: datetime, high: datetime) -> Decimal:
    """Overlap of [start, end) with [low, high) on millisecond-floored instants."""
    milliseconds = min(epoch_ms(end), epoch_ms(high)) - max(epoch_ms(start), epoch_ms(low))
    return Decimal(milliseconds) / Decimal(1000) if milliseconds > 0 else Decimal(0)


def aggregate_fairness(
    segments: Iterable[LedgerSegment],
    *,
    from_at: datetime,
    to_at: datetime,
    bucket_seconds: int,
    now: datetime,
) -> list[FairnessRow]:
    count = fairness_bucket_count(from_at, to_at, bucket_seconds)
    totals: dict[tuple[int, UUID], list[Any]] = {}
    with localcontext() as context:
        context.prec = _PRECISION
        for segment in segments:
            end = segment.ended_at if segment.ended_at is not None else now
            if segment.started_at >= to_at or end <= from_at:
                continue
            for index in range(count):
                bucket_start, bucket_end = fairness_bucket_bounds(
                    from_at, to_at, bucket_seconds, index
                )
                seconds = _overlap_seconds(segment.started_at, end, bucket_start, bucket_end)
                if seconds <= 0:
                    continue
                entry = totals.setdefault(
                    (index, segment.tenant_id), [Decimal(0), Decimal(0), Decimal(0), None]
                )
                entry[0] += segment.dominant_share * seconds
                entry[1] += segment.dominant_share * seconds / segment.weight
                entry[2] += seconds
                latest = entry[3]
                if latest is None or (segment.started_at, segment.segment_id) > (
                    latest.started_at,
                    latest.segment_id,
                ):
                    entry[3] = segment
        rows = []
        for (index, tenant_id), (drt, normalized, occupancy, latest) in sorted(totals.items()):
            weight = drt / normalized if normalized > 0 else latest.weight
            rows.append(
                FairnessRow(
                    bucket_index=index,
                    tenant_id=tenant_id,
                    weight=weight,
                    dominant_resource_time=drt,
                    normalized_service=normalized,
                    occupancy=occupancy,
                )
            )
    return rows


def fairness_report(
    rows: Sequence[FairnessRow],
    *,
    from_at: datetime,
    to_at: datetime,
    bucket_seconds: int,
) -> Mapping[str, Any]:
    """Build the FairnessReport wire body; rows must already be sorted."""
    if len(rows) > MAX_FAIRNESS_BUCKETS:
        raise _invalid("Fairness report would exceed 1000 tenant buckets; narrow the query")
    buckets = []
    for row in rows:
        start_at, end_at = fairness_bucket_bounds(from_at, to_at, bucket_seconds, row.bucket_index)
        buckets.append(
            {
                "tenant_id": row.tenant_id,
                "start_at": start_at,
                "end_at": end_at,
                "weight": row.weight,
                "dominant_resource_time_seconds": row.dominant_resource_time,
                "normalized_service": row.normalized_service,
                "allocation_occupancy_seconds": row.occupancy,
            }
        )
    return json_wire_value(
        {"from": from_at, "to": to_at, "bucket_seconds": bucket_seconds, "buckets": buckets}
    )
