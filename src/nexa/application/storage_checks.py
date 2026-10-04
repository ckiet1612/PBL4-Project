"""Read-only storage and result consistency checks (B19-R05, B19-R14).

Each check is one SQL statement, so it reads a single snapshot: a commit racing the
check cannot show up as a false drift. Nothing here writes, locks rows or records an
audit: the GC loop exports the result as metrics and `nexa-maintenance storage-check`
/ `consistency-check` print it, also while the policy is WRITE_FROZEN. A drift is
reported, never repaired.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

_COUNTER_DRIFT = text(
    """
    WITH counters AS (
        SELECT tenant_id, committed_bytes, reserved_bytes FROM artifact_storage_counters
    ), committed AS (
        SELECT tenant_id, SUM(size_bytes) AS total
        FROM artifacts WHERE state = 'COMMITTED' GROUP BY tenant_id
    ), reserved AS (
        SELECT tenant_id, SUM(expected_size_bytes) AS total
        FROM upload_sessions WHERE state = 'ACTIVE' GROUP BY tenant_id
    )
    SELECT tenant_id,
           COALESCE(counters.committed_bytes, 0) AS committed_counter,
           COALESCE(committed.total, 0) AS committed_artifacts,
           COALESCE(counters.reserved_bytes, 0) AS reserved_counter,
           COALESCE(reserved.total, 0) AS reserved_sessions
    FROM counters
    FULL JOIN committed USING (tenant_id)
    FULL JOIN reserved USING (tenant_id)
    WHERE COALESCE(counters.committed_bytes, 0) <> COALESCE(committed.total, 0)
       OR COALESCE(counters.reserved_bytes, 0) <> COALESCE(reserved.total, 0)
    ORDER BY tenant_id
    """
)

# B19-R05: committed artifacts referenced only by their worker upload session
# (purpose ATTEMPT_UPLOAD) and never published as a checkpoint, result, chunk,
# job spec or log segment. They are retained, not collected.
_RETAINED_UNPUBLISHED = text(
    """
    SELECT COUNT(*) AS artifacts, COALESCE(SUM(a.size_bytes), 0) AS bytes
    FROM artifacts AS a
    WHERE a.state = 'COMMITTED'
      AND EXISTS (
        SELECT 1 FROM artifact_references AS r
        WHERE r.artifact_id = a.artifact_id AND r.owner_type = 'UPLOAD_SESSION'
      )
      AND NOT EXISTS (
        SELECT 1 FROM artifact_references AS r
        WHERE r.artifact_id = a.artifact_id AND r.owner_type <> 'UPLOAD_SESSION'
      )
    """
)

_RESULT_CONSISTENCY = text(
    """
    WITH accepted AS (SELECT DISTINCT unnest(:accepted) AS job_id),
    result_counts AS (
        SELECT job_id, COUNT(*) AS results FROM results GROUP BY job_id
    )
    SELECT
        (SELECT COUNT(*) FROM accepted
          WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.job_id = accepted.job_id))
          AS missing_accepted,
        (SELECT COUNT(*) FROM jobs
          WHERE jobs.state = 'SUCCEEDED'
            AND NOT EXISTS (SELECT 1 FROM results WHERE results.job_id = jobs.job_id))
          AS succeeded_without_result,
        (SELECT COUNT(*) FROM result_counts WHERE results > 1) AS multiple_results,
        (SELECT COUNT(*) FROM result_counts JOIN jobs USING (job_id)
          WHERE jobs.state <> 'SUCCEEDED') AS result_of_unsucceeded_job,
        (SELECT COUNT(*) FROM accepted) AS accepted,
        (SELECT COUNT(*) FROM jobs WHERE state = 'SUCCEEDED') AS succeeded
    """
).bindparams(bindparam("accepted", type_=ARRAY(PG_UUID(as_uuid=True))))


@dataclass(frozen=True, slots=True)
class CounterDrift:
    tenant_id: UUID
    committed_counter: int
    committed_artifacts: int
    reserved_counter: int
    reserved_sessions: int


def counter_drift(session: Session) -> list[CounterDrift]:
    """Tenants whose byte counters differ from committed artifacts / ACTIVE sessions."""
    return [
        CounterDrift(
            row["tenant_id"],
            int(row["committed_counter"]),
            int(row["committed_artifacts"]),
            int(row["reserved_counter"]),
            int(row["reserved_sessions"]),
        )
        for row in session.execute(_COUNTER_DRIFT).mappings()
    ]


def retained_unpublished(session: Session) -> tuple[int, int]:
    """(artifacts, bytes) kept but never published (B19-R05)."""
    row = session.execute(_RETAINED_UNPUBLISHED).mappings().one()
    return int(row["artifacts"]), int(row["bytes"])


def storage_summary(session: Session) -> dict[str, object]:
    """The `storage-check` JSON: counts and per-tenant drift; no path or secret."""
    drift = counter_drift(session)
    artifacts, retained_bytes = retained_unpublished(session)
    return {
        "consistent": not drift,
        "drift_tenants": len(drift),
        "drift": [
            {
                "tenant_id": str(item.tenant_id),
                "committed_counter": item.committed_counter,
                "committed_artifacts": item.committed_artifacts,
                "reserved_counter": item.reserved_counter,
                "reserved_sessions": item.reserved_sessions,
            }
            for item in drift
        ],
        "retained_unpublished_artifacts": artifacts,
        "retained_unpublished_bytes": retained_bytes,
    }


def result_consistency(session: Session, accepted: Iterable[UUID]) -> dict[str, object]:
    """The `consistency-check` JSON: accepted IDs and the one-final-result rule."""
    row = session.execute(_RESULT_CONSISTENCY, {"accepted": list(accepted)}).mappings().one()
    summary: dict[str, object] = {key: int(value) for key, value in row.items()}
    summary["consistent"] = all(
        summary[key] == 0
        for key in (
            "missing_accepted",
            "succeeded_without_result",
            "multiple_results",
            "result_of_unsucceeded_job",
        )
    )
    return summary
