"""B15-R02: a non-retryable failure class ends the Job FAILED even when desired is PAUSED.

state-machines.md, RECOVERING rows: both pause branches (PAUSED with a committed
checkpoint, the `CHECKPOINT_FOR_PAUSE` retry without one) need an infrastructure
failure; any other committed class takes the FAILED row, whose guard is only
"desired not CANCELLED". `test_pausing_failure_with_committed_checkpoint` pins the
branch with a checkpoint; this pins the restart-safe branch without one.
"""

import pytest
from sqlalchemy import func, select

from nexa.infrastructure.persistence import schema as s
from tests.integration.test_control_b15 import Control, _container_failure
from tests.integration.test_retry_b14 import _job

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    ("failure_class", "reason_code", "flags"),
    [
        ("TIMEOUT", "RUNTIME_LIMIT_REACHED", {"timeout": True}),
        ("OOM", "CONTAINER_OOM", {"oom": True}),
        ("INTERNAL", "CHECKPOINT_PROTOCOL_ERROR", {}),
    ],
)
def test_a_non_retryable_failure_while_pausing_without_a_checkpoint_fails_the_job(
    migrated_postgres_engine, tmp_path, failure_class, reason_code, flags
):
    engine = migrated_postgres_engine
    # Restart-safe with retry budget left: only the failure class keeps it from CFP.
    label = f"rem-r02-{failure_class.lower()}"
    with Control(engine, tmp_path, label=label, restart_safe=True) as control:
        control.counters(outstanding=1, active=1)
        assert control.control("pause").status_code == 202
        failed = _container_failure(control, failure_class, reason_code, **flags)
        assert failed.status_code == 200, failed.text
        cleaned = control.cleanup()
        assert cleaned.status_code == 200, cleaned.text
        job = _job(engine, control.job.job_id)
        assert (job["state"], job["retry_count"], job["recovery_intent"]) == ("FAILED", 0, None)
        assert job["terminal_at"] is not None
        rows = control.authority_rows()
        assert (rows["attempt"]["state"], rows["allocation"]["state"]) == ("FAILED", "RELEASED")
        with engine.connect() as connection:
            schedules = connection.execute(
                select(func.count())
                .select_from(s.retry_schedules)
                .where(s.retry_schedules.c.job_id == control.job.job_id)
            ).scalar_one()
        assert schedules == 0
        assert control.counter_values()["GLOBAL"] == (0, 0)
