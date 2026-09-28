"""B16-R24/R26: the worker forwards a runner's typed OOM and INVALID_INPUT failures."""

import pytest

from tests.worker.test_checkpoint_flow_b14 import started


def _runner_failed(harness, payload):
    envelope = {
        "schema_version": 1,
        "message_sequence": 99,
        "type": "FAILED",
        "payload": {"exit_code": 137, "runtime_limit_reached": False, **payload},
    }
    harness.journal.update_runner_state(
        harness.attempt_id, lambda local: {**local, "pending_execution_message": envelope}
    )
    harness.agent._result_once()
    return harness.api.failures


@pytest.mark.parametrize(
    ("payload", "expected", "oom_observed"),
    [
        (
            {"failure_class": "OOM", "reason_code": "CONTAINER_OOM", "oom_killed": True},
            ("OOM", "CONTAINER_OOM"),
            True,
        ),
        (
            {
                "failure_class": "INVALID_INPUT",
                "reason_code": "INVALID_INPUT",
                "oom_killed": False,
                "exit_code": 65,
            },
            ("INVALID_INPUT", "INVALID_INPUT"),
            False,
        ),
        # An OOM class without the runner's cgroup evidence, or evidence on another
        # class, is not trusted: it stays internal and the observation claims no OOM.
        (
            {"failure_class": "OOM", "reason_code": "CONTAINER_OOM", "oom_killed": False},
            ("INTERNAL", "WORKLOAD_EXIT_NONZERO"),
            False,
        ),
        (
            {"failure_class": "INTERNAL", "reason_code": "INVALID_RESULT", "oom_killed": True},
            ("INTERNAL", "WORKLOAD_EXIT_NONZERO"),
            False,
        ),
        (
            {"failure_class": "OOM", "reason_code": "SOMETHING_ELSE", "oom_killed": True},
            ("INTERNAL", "WORKLOAD_EXIT_NONZERO"),
            False,
        ),
    ],
)
def test_runner_failure_is_forwarded_with_a_matching_observation(
    tmp_path, payload, expected, oom_observed
):
    harness = started(tmp_path)
    failures = _runner_failed(harness, payload)
    assert [(f["failure_class"], f["reason_code"]) for f in failures] == [expected]
    observation = failures[0]["observation"]
    assert observation["observation_type"] == "CONTAINER"
    # The runner is still alive: there is no Docker exit to report.
    assert observation["exit_code"] is None
    assert observation["oom_killed"] is oom_observed
    assert observation["runtime_limit_reached"] is False
