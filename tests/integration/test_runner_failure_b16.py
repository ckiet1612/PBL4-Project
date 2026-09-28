"""B16-R24/R26: runner-classified failures reach the job as typed, non-retried failures."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from nexa.infrastructure.persistence import schema as s
from tests.integration.test_control_b15 import Control, _job, _publish_checkpoint
from tests.integration.test_worker_authority_b10 import CONTAINER_ID, DIGEST

pytestmark = pytest.mark.postgres


def _runner_failure(control, failure_class, reason_code):
    # The observation shape the worker sends for a runner FAILED frame while its
    # container is still alive: no Docker exit code, cgroup OOM only for OOM.
    return control.job.post(
        "/fail",
        {
            "authority": control.job.authority,
            "failure_class": failure_class,
            "reason_code": reason_code,
            "observation": {
                "observation_type": "CONTAINER",
                "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
                "observed_at": datetime.now(UTC).isoformat(),
                "exit_code": None,
                "oom_killed": failure_class == "OOM",
                "runtime_limit_reached": False,
            },
        },
    )


@pytest.mark.parametrize(
    ("failure_class", "reason_code", "label"),
    [("OOM", "CONTAINER_OOM", "b16-oom"), ("INVALID_INPUT", "INVALID_INPUT", "b16-invalid")],
)
def test_runner_oom_and_invalid_input_fail_the_job_without_a_retry(
    migrated_postgres_engine, tmp_path, failure_class, reason_code, label
):
    # Contract: OOM and INVALID_INPUT are not retried, even for a restart-safe job
    # with a committed checkpoint.
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label=label) as control:
        control.counters(outstanding=1, active=1)
        assert _publish_checkpoint(control.job).status_code == 201
        failed = _runner_failure(control, failure_class, reason_code)
        assert failed.status_code == 200, failed.text
        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["retry_count"], job["retry_ready_at"]) == ("FAILED", 0, None)
        rows = control.authority_rows()
        assert (rows["attempt"]["failure_class"], rows["attempt"]["failure_reason"]) == (
            failure_class,
            reason_code,
        )
        assert rows["allocation"]["state"] == "RELEASED"
        with engine.connect() as connection:
            schedules = connection.execute(
                select(func.count())
                .select_from(s.retry_schedules)
                .where(s.retry_schedules.c.job_id == control.job.job_id)
            ).scalar_one()
            attempts = connection.execute(
                select(func.count())
                .select_from(s.attempts)
                .where(s.attempts.c.job_id == control.job.job_id)
            ).scalar_one()
        assert (schedules, attempts) == (0, 1)
        assert control.counter_values()["GLOBAL"] == (0, 0)


def test_runner_oom_without_the_oom_observation_is_rejected(migrated_postgres_engine, tmp_path):
    # The worker marks the observation from the forwarded frame; a mismatch is a conflict.
    engine = migrated_postgres_engine
    with Control(engine, tmp_path, label="b16-oom-mismatch") as control:
        control.counters(outstanding=1, active=1)
        rejected = control.job.post(
            "/fail",
            {
                "authority": control.job.authority,
                "failure_class": "OOM",
                "reason_code": "CONTAINER_OOM",
                "observation": {
                    "observation_type": "CONTAINER",
                    "container": {
                        "container_id": CONTAINER_ID,
                        "runtime_identity_digest": DIGEST,
                    },
                    "observed_at": datetime.now(UTC).isoformat(),
                    "exit_code": None,
                    "oom_killed": False,
                    "runtime_limit_reached": False,
                },
            },
        )
        assert rejected.status_code == 409, rejected.text
        assert control.authority_rows()["attempt"]["failure_class"] is None
