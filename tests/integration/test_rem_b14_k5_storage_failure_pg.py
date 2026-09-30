"""Remediation B14-K5 on PostgreSQL: CHECKPOINT_STORAGE_FAILED is a safe, final reason.

The runner reports a checkpoint copy it could not complete (ENOSPC/EDQUOT/EIO in its
bounded ``/output``) as INTERNAL/CHECKPOINT_STORAGE_FAILED. The server allowlist accepts
that code only under INTERNAL; the failure ends the open checkpoint reservation and,
after exact cleanup, fails the job without spending a retry, even for a restart-safe job
with a committed checkpoint. Before the fix the same fault arrived as retryable
INFRASTRUCTURE/RUNNER_UNAVAILABLE, shown here as the contrast case.
"""

import pytest
from sqlalchemy import func, select

from nexa.infrastructure.persistence import schema as s
from tests.integration.test_control_b15 import Control, _container_failure, _publish_checkpoint
from tests.integration.test_retry_b14 import _job

pytestmark = pytest.mark.postgres


def _retry_schedules(engine, job_id):
    with engine.connect() as connection:
        return connection.execute(
            select(func.count())
            .select_from(s.retry_schedules)
            .where(s.retry_schedules.c.job_id == job_id)
        ).scalar_one()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (("INTERNAL", "CHECKPOINT_STORAGE_FAILED"), ("FAILED", 0, 0)),
        # The pre-fix classification of the same fault spent a retry.
        (("INFRASTRUCTURE", "RUNNER_UNAVAILABLE"), ("RETRY_WAIT", 1, 1)),
    ],
)
def test_storage_failure_fails_the_job_without_a_retry(
    migrated_postgres_engine, tmp_path, failure, expected
):
    engine = migrated_postgres_engine
    label = f"rem-k5-{failure[1].lower().replace('_', '-')}"
    with Control(engine, tmp_path, label=label) as control:
        control.counters(outstanding=1, active=1)
        # A restart-safe job with a committed checkpoint, then a second cycle whose
        # staging copy fails after the worker reserved it.
        assert _publish_checkpoint(control.job).status_code == 201
        reserved = control.job.reserve()
        assert reserved.status_code == 201, reserved.text

        failed = _container_failure(control, *failure)
        assert failed.status_code == 200, failed.text
        rows = control.job.rows()
        assert (rows["attempt"]["failure_class"], rows["attempt"]["failure_reason"]) == failure
        # The failure callback ends the reservation of the cycle that could not stage.
        assert [r["state"] for r in rows["reservations"]][-1] == "ABANDONED"

        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        job = _job(engine, control.job.job_id)
        state, retries, schedules = expected
        assert (job["state"], job["retry_count"]) == (state, retries)
        assert _retry_schedules(engine, control.job.job_id) == schedules
        if state == "FAILED":
            assert job["terminal_at"] is not None
            assert control.counter_values()["GLOBAL"] == (0, 0)


@pytest.mark.parametrize("failure_class", ["INFRASTRUCTURE", "TIMEOUT", "INVALID_INPUT"])
def test_storage_reason_is_accepted_only_as_internal(
    migrated_postgres_engine, tmp_path, failure_class
):
    engine = migrated_postgres_engine
    label = f"rem-k5-class-{failure_class.lower().replace('_', '-')}"
    with Control(engine, tmp_path, label=label) as control:
        control.counters(outstanding=1, active=1)
        rejected = _container_failure(control, failure_class, "CHECKPOINT_STORAGE_FAILED")
        assert rejected.status_code == 422, rejected.text
        assert control.job.rows()["attempt"]["failure_reason"] is None
