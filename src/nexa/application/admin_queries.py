"""B18 cross-tenant admin reads: the job queue (read-only) and the fairness report.

Every read authorizes the live SYSTEM_ADMIN principal and appends one audit record in
the same transaction. Queries are plain bounded SELECTs: no row locks, so they never
wait on or delay coordinator accounting (charge_locked locks open segments).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, or_, select, text
from sqlalchemy.orm import Session

from nexa.application.errors import ApplicationError
from nexa.application.fairness_report import (
    MAX_FAIRNESS_BUCKETS,
    FairnessRow,
    fairness_bucket_count,
    fairness_report,
)
from nexa.application.job_service import JobService
from nexa.application.json_codec import json_wire_value
from nexa.coordinator.accounting import epoch_ms
from nexa.domain.identity import Principal
from nexa.infrastructure.persistence.locking import transaction_timestamp
from nexa.infrastructure.persistence.schema import job_specs, jobs, logical_sessions
from nexa.infrastructure.persistence.transactions import run_transaction


def _exact_decimal(column: str) -> str:
    """EXACT_DECIMAL_TEXT ``sign:digits:exponent`` as numeric, using pg_catalog built-ins only.

    No migration helper function and no search_path lookup (B13-R12).
    """
    return (
        f"(CASE WHEN pg_catalog.split_part({column}, ':', 1) = '1' THEN -1 ELSE 1 END"
        f" * CAST(pg_catalog.concat(pg_catalog.split_part({column}, ':', 2), 'e',"
        f" pg_catalog.split_part({column}, ':', 3)) AS pg_catalog.numeric))"
    )


# A segment covers [started_at, ended_at); an open segment (NULL end) is unbounded, so
# one range-overlap test selects closed and open segments alike. The expression matches
# the GiST index ix_allocation_ledger_segments_period exactly.
SEGMENT_RANGE = "pg_catalog.tstzrange(seg.started_at, seg.ended_at, '[)')"
# JIT is off for this read: the planner cannot estimate generate_series rows, and the
# inflated cost made JIT compilation dominate the statement time.
FAIRNESS_SETTINGS = text("SET LOCAL jit = off")

# Offsets are integer milliseconds from epoch_ms(:from_at): every instant is floored to
# the millisecond exactly as coordinator accounting charges (epoch_ms, B18-R26), so a
# closed segment inside the range sums to its charged interval. Open segments end at
# the statement time. EXTRACT/GREATEST/LEAST/COALESCE are grammar, not search_path
# lookups; EXTRACT(EPOCH) is an exact numeric with six decimals.
_FAIRNESS_SQL = """
WITH bounds AS (
    SELECT CAST(:bucket_ms AS bigint) AS bucket_ms,
           CAST(:from_ms AS bigint) AS from_ms,
           CAST(:range_ms AS bigint) AS range_ms,
           pg_catalog.statement_timestamp() AS now_at
),
segments AS (
    SELECT seg.segment_id,
           seg.tenant_id,
           seg.started_at,
           {share} AS share,
           {weight} AS weight,
           GREATEST(
               CAST(pg_catalog.floor(EXTRACT(EPOCH FROM seg.started_at) * 1000) AS bigint)
               - b.from_ms,
               0
           ) AS start_ms,
           LEAST(
               CAST(
                   pg_catalog.floor(EXTRACT(EPOCH FROM COALESCE(seg.ended_at, b.now_at)) * 1000)
                   AS bigint
               )
               - b.from_ms,
               b.range_ms
           ) AS end_ms
    FROM allocation_ledger_segments AS seg CROSS JOIN bounds AS b
    WHERE {segment_range} OPERATOR(pg_catalog.&&) pg_catalog.tstzrange(:from_at, :to_at, '[)')
      {tenant_filter}
),
pieces AS (
    SELECT seg.segment_id,
           seg.tenant_id,
           seg.started_at,
           seg.share,
           seg.weight,
           k AS bucket_index,
           CAST(
               LEAST(seg.end_ms, (k + 1) * b.bucket_ms) - GREATEST(seg.start_ms, k * b.bucket_ms)
               AS pg_catalog.numeric
           ) / 1000 AS seconds
    FROM segments AS seg
    CROSS JOIN bounds AS b
    CROSS JOIN LATERAL pg_catalog.generate_series(
        seg.start_ms / b.bucket_ms,
        (seg.end_ms + b.bucket_ms - 1) / b.bucket_ms - 1
    ) AS k
    WHERE seg.end_ms > seg.start_ms
)
SELECT bucket_index,
       tenant_id,
       pg_catalog.sum(share * seconds) AS dominant_resource_time,
       pg_catalog.sum(share * seconds / weight) AS normalized_service,
       pg_catalog.sum(seconds) AS occupancy,
       (pg_catalog.array_agg(weight ORDER BY started_at DESC, segment_id DESC))[1] AS latest_weight
FROM pieces
WHERE seconds > 0
GROUP BY bucket_index, tenant_id
ORDER BY bucket_index, tenant_id
LIMIT {limit}
"""
_FAIRNESS_ALL = text(
    _FAIRNESS_SQL.format(
        share=_exact_decimal("seg.dominant_share"),
        weight=_exact_decimal("seg.weight"),
        segment_range=SEGMENT_RANGE,
        tenant_filter="",
        limit=MAX_FAIRNESS_BUCKETS + 1,
    )
)
# A separate statement (not ":tenant_id IS NULL OR ...") keeps the tenant index usable
# under generic prepared plans.
_FAIRNESS_TENANT = text(
    _FAIRNESS_SQL.format(
        share=_exact_decimal("seg.dominant_share"),
        weight=_exact_decimal("seg.weight"),
        segment_range=SEGMENT_RANGE,
        tenant_filter="AND seg.tenant_id = :tenant_id",
        limit=MAX_FAIRNESS_BUCKETS + 1,
    )
)


def fairness_parameters(
    *, from_at: datetime, to_at: datetime, bucket_seconds: int, tenant_id: UUID | None = None
) -> dict[str, Any]:
    """Bind values of the aggregate, in whole milliseconds like the ledger (B18-R26)."""
    parameters: dict[str, Any] = {
        "from_at": from_at,
        "to_at": to_at,
        "bucket_ms": bucket_seconds * 1_000,
        "from_ms": epoch_ms(from_at),
        "range_ms": epoch_ms(to_at) - epoch_ms(from_at),
    }
    if tenant_id is not None:
        parameters["tenant_id"] = tenant_id
    return parameters


def fairness_rows(
    session: Session,
    *,
    from_at: datetime,
    to_at: datetime,
    bucket_seconds: int,
    tenant_id: UUID | None,
) -> list[FairnessRow]:
    """Run the bounded aggregate; returns at most 1001 rows (the caller rejects >1000)."""
    parameters = fairness_parameters(
        from_at=from_at, to_at=to_at, bucket_seconds=bucket_seconds, tenant_id=tenant_id
    )
    session.execute(FAIRNESS_SETTINGS)
    result = session.execute(_FAIRNESS_ALL if tenant_id is None else _FAIRNESS_TENANT, parameters)
    rows = []
    for row in result.mappings():
        normalized = row["normalized_service"]
        rows.append(
            FairnessRow(
                bucket_index=row["bucket_index"],
                tenant_id=row["tenant_id"],
                weight=(
                    row["dominant_resource_time"] / normalized
                    if normalized > 0
                    else row["latest_weight"]
                ),
                dominant_resource_time=row["dominant_resource_time"],
                normalized_service=normalized,
                occupancy=row["occupancy"],
            )
        )
    return rows


def admin_job_statement(
    *,
    tenant_id: UUID | None,
    user_id: UUID | None,
    state: str | None,
    waiting_reason: str | None,
    created_after: datetime | None,
    after: tuple[datetime, UUID] | None,
    limit: int,
) -> Select:
    """The adminListJobs page query: newest first, keyset on (created_at, job_id).

    The session join carries tenant_id (its FK is (tenant_id, job_id)), so it can use
    uq_logical_sessions_tenant_job inside an index-ordered nested loop instead of hashing
    every session.
    """
    statement = select(*JobService._job_columns()).select_from(
        jobs.join(job_specs, job_specs.c.job_id == jobs.c.job_id).join(
            logical_sessions,
            and_(
                logical_sessions.c.tenant_id == jobs.c.tenant_id,
                logical_sessions.c.job_id == jobs.c.job_id,
            ),
        )
    )
    if tenant_id is not None:
        statement = statement.where(jobs.c.tenant_id == tenant_id)
    if user_id is not None:
        statement = statement.where(jobs.c.submitter_user_id == user_id)
    if state:
        statement = statement.where(jobs.c.state == state)
    if waiting_reason:
        statement = statement.where(jobs.c.waiting_reason == waiting_reason)
    if created_after is not None:
        statement = statement.where(jobs.c.created_at >= created_after)
    if after is not None:
        created_at, last_id = after
        statement = statement.where(
            or_(
                jobs.c.created_at < created_at,
                and_(jobs.c.created_at == created_at, jobs.c.job_id < last_id),
            )
        )
    return statement.order_by(jobs.c.created_at.desc(), jobs.c.job_id.desc()).limit(limit)


class AdminQueryMixin:
    """Cross-tenant admin reads mixed into AdminWorkerService (needs AdminService helpers)."""

    def list_jobs(
        self,
        principal: Principal,
        *,
        page_size: int,
        cursor: str | None,
        tenant_id: UUID | None,
        user_id: UUID | None,
        state: str | None,
        waiting_reason: str | None,
        created_after: datetime | None,
    ) -> dict[str, Any]:
        self._validate_page_size(page_size)

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            now = transaction_timestamp(session)
            binding = {
                "actor_id": str(live.user_id),
                "operation_id": "adminListJobs",
                "tenant_id": str(tenant_id) if tenant_id else "",
                "user_id": str(user_id) if user_id else "",
                "state": state or "",
                "waiting_reason": waiting_reason or "",
                "created_after": created_after.isoformat() if created_after else "",
            }
            after = self._decode_cursor(cursor, binding=binding, now=now) if cursor else None
            rows = (
                session.execute(
                    admin_job_statement(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        state=state,
                        waiting_reason=waiting_reason,
                        created_after=created_after,
                        after=after,
                        limit=page_size + 1,
                    )
                )
                .mappings()
                .all()
            )
            visible = rows[:page_size]
            next_cursor = (
                self._encode_cursor(visible[-1], id_name="job_id", binding=binding, now=now)
                if len(rows) > page_size
                else None
            )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.job.list",
                target_type="JOB_COLLECTION",
                target_id=str(tenant_id) if tenant_id else "all",
                reason="Cross-tenant job list read",
            )
            return json_wire_value(
                {
                    "items": [JobService._job_view(row) for row in visible],
                    "page": {"next_cursor": next_cursor, "page_size": page_size},
                }
            )

        return run_transaction(self.session_factory, operation)

    def get_job(self, principal: Principal, *, job_id: UUID) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            row = (
                session.execute(JobService._job_query().where(jobs.c.job_id == job_id))
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Job was not found"
                )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.job.get",
                target_type="JOB",
                target_id=str(job_id),
                tenant_id=row["tenant_id"],
                reason="Cross-tenant job read",
            )
            return JobService._job_view(row)

        return run_transaction(self.session_factory, operation)

    def query_fairness(
        self,
        principal: Principal,
        *,
        from_at: datetime,
        to_at: datetime,
        bucket_seconds: int,
        tenant_id: UUID | None,
    ) -> dict[str, Any]:
        fairness_bucket_count(from_at, to_at, bucket_seconds)

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            rows = fairness_rows(
                session,
                from_at=from_at,
                to_at=to_at,
                bucket_seconds=bucket_seconds,
                tenant_id=tenant_id,
            )
            report = fairness_report(
                rows, from_at=from_at, to_at=to_at, bucket_seconds=bucket_seconds
            )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.fairness.query",
                target_type="FAIRNESS_REPORT",
                target_id=str(tenant_id) if tenant_id is not None else "all",
                reason="Fairness report read",
            )
            return dict(report)

        return run_transaction(self.session_factory, operation)
