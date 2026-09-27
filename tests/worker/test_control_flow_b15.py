"""B15 worker control: pause checkpoint and stop, cancel revocation, R05 and R10.

Reuses the B14 harness: the production agent loops against the real trusted
runner; only Docker and the HTTP API are faked.
"""

import contextlib
import json
from dataclasses import asdict

import pytest

from nexa.worker.client import WorkerApiError
from nexa.workloads.trusted_runner import RunnerState
from tests.worker.test_checkpoint_flow_b14 import (
    INPUT,
    SECOND,
    Api,
    Backend,
    Harness,
    calls,
    claim_context,
)
from tests.workloads.test_runner_checkpoint_b14 import _request, _runner, _started, _write_state


class ControlApi(Api):
    """Adds renewal with a configurable desired state and a reconciliation page."""

    def __init__(self, context, **kwargs):
        super().__init__(context, **kwargs)
        self.desired = "RUNNING"
        self.renew_error = None
        self.fail_error = None
        self.renewals = []
        self.page = {"items": [], "page": {"next_cursor": None}}

    def renew(self, attempt, callback, body):
        self.renewals.append(callback)
        if self.renew_error is not None:
            raise WorkerApiError(409, self.renew_error)
        return {
            "server_time": "2026-09-26T00:00:00.000Z",
            "lease_expires_at": "2026-09-26T00:00:45.000Z",
            "desired_state": self.desired,
            "renew_interval_seconds": 5,
            "safety_margin_seconds": 5,
        }

    def fail(self, attempt, callback, body):
        if self.fail_error is not None:
            self.failures.append(body)
            raise WorkerApiError(409, self.fail_error)
        return super().fail(attempt, callback, body)

    def reconciliation(self, worker_id, incarnation_id, cursor=None):
        return self.page


class ListingBackend(Backend):
    """Docker fake whose `ps` lists the created workload container until removal."""

    def run(self, argv, timeout_seconds):
        if argv[1] == "ps":
            listed = self.created and self.container_id not in self.removed
            return 0, (self.container_id + "\n").encode() if listed else b"", b""
        return super().run(argv, timeout_seconds)


def harness_for(tmp_path, **kwargs):
    context = claim_context(**kwargs)
    return Harness(
        tmp_path,
        context,
        api=ControlApi(context),
        backend=ListingBackend(tmp_path / "fs" / "output"),
    )


def running(tmp_path, **kwargs):
    harness = harness_for(tmp_path, **kwargs)
    harness.dispatch()
    harness.run_workload(20, 555)
    harness.cycle(2)
    return harness


def controls(harness, type_):
    state = harness.flow_state()
    return [
        entry["frame"]["payload"]
        for entry in state.get("result_controls", {}).values()
        if entry["frame"]["type"] == type_
    ]


def failures(harness):
    for body in harness.api.failures:
        observation = body["observation"]
        if observation["observation_type"] == "CONTAINER":
            # The server rejects a failure class its observation contradicts (409).
            timeout = (body["failure_class"], body["reason_code"]) == (
                "TIMEOUT",
                "RUNTIME_LIMIT_REACHED",
            )
            assert observation["runtime_limit_reached"] == timeout, body
            assert observation["oom_killed"] == (body["failure_class"] == "OOM"), body
    return [(f["failure_class"], f["reason_code"]) for f in harness.api.failures]


def pending(harness, operation):
    return [
        record
        for record in harness.agent.state.operations.values()
        if record["operation"] == operation
    ]


# --- pause ------------------------------------------------------------------------


def test_renewed_pause_checkpoints_then_stops_and_cleans_up_without_failure(tmp_path):
    harness = running(tmp_path)
    harness.api.desired = "PAUSED"
    harness.agent.renew_once()
    harness.cycle(10)
    # Exactly one reservation, requested for PAUSE before the interval was due.
    assert len(calls(harness.api, "reserve_checkpoint")) == 1
    assert [p["reason"] for p in controls(harness, "REQUEST_CHECKPOINT")] == ["PAUSE"]
    assert len(harness.api.published) == 1
    assert [p["reason"] for p in controls(harness, "REQUEST_STOP")] == ["PAUSE"]
    assert failures(harness) == []
    (cleanup,) = harness.api.cleanups
    assert cleanup["proof"]["container"]["container_id"] == harness.backend.container_id
    assert harness.attempt_id not in harness.agent._adopted
    assert harness.agent.state.operations == {}
    assert calls(harness.api, "reserve_result") == []


def test_an_open_interval_cycle_becomes_the_pause_checkpoint(tmp_path):
    harness = running(tmp_path)
    harness.advance(6)
    harness.agent._result_once()
    assert [p["reason"] for p in controls(harness, "REQUEST_CHECKPOINT")] == ["INTERVAL"]
    harness.api.desired = "PAUSED"
    harness.agent.renew_once()
    harness.cycle(10)
    # The pause made no second reservation: the in-flight cycle was finished instead.
    assert len(calls(harness.api, "reserve_checkpoint")) == 1
    assert len(harness.api.published) == 1
    assert [p["reason"] for p in controls(harness, "REQUEST_STOP")] == ["PAUSE"]
    assert failures(harness) == []
    assert len(harness.api.cleanups) == 1


def test_checkpoint_committed_before_the_pause_was_observed_ends_the_pause(tmp_path):
    harness = running(tmp_path)
    harness.advance(6)
    harness.cycle(8)
    assert len(harness.api.published) == 1
    # The server paused the job before that publish, so it moved the Attempt to
    # STOPPING; a further reservation is a state conflict.
    harness.api.reserve_status = 409
    harness.api.desired = "PAUSED"
    harness.agent.renew_once()
    harness.cycle(8)
    assert [p["reason"] for p in controls(harness, "REQUEST_STOP")] == ["PAUSE"]
    assert failures(harness) == []
    assert len(harness.api.cleanups) == 1


def test_rejected_pause_checkpoint_keeps_running_after_the_pause_is_aborted(tmp_path):
    harness = running(tmp_path)
    harness.api.publish_status = 422
    harness.api.desired = "PAUSED"
    harness.agent.renew_once()
    harness.cycle(10)
    # The server aborted the pause; no further pause cycle until renewal says so.
    assert len(calls(harness.api, "reserve_checkpoint")) == 1
    assert controls(harness, "REQUEST_STOP") == []
    # A PAUSED answer that raced the aborting publish does not re-open a pause.
    harness.agent.renew_once()
    harness.cycle(4)
    assert len(calls(harness.api, "reserve_checkpoint")) == 1
    harness.api.desired = "RUNNING"
    harness.agent.renew_once()
    harness.advance(60)
    harness.cycle(4)
    assert failures(harness) == []
    assert harness.api.cleanups == []
    assert harness.attempt_id in harness.agent._adopted
    assert harness.flow_state()["control_desired"] == "RUNNING"


def test_pause_without_a_checkpoint_fails_runner_unavailable_after_40_seconds(tmp_path):
    harness = running(tmp_path, checkpointable=False)
    harness.api.desired = "PAUSED"
    harness.agent.renew_once()
    harness.cycle(2)
    harness.advance(39)
    harness.cycle(2)
    assert failures(harness) == []
    harness.advance(2)
    harness.cycle(2)
    assert failures(harness) == [("INFRASTRUCTURE", "RUNNER_UNAVAILABLE")]
    assert len(harness.api.cleanups) == 1


def test_checkpoint_for_pause_attempt_pauses_without_renewal_and_never_reserves_a_result(
    tmp_path,
):
    harness = harness_for(tmp_path, restart_safe=True)
    harness.context["execution_intent"] = "CHECKPOINT_FOR_PAUSE"
    harness.dispatch()
    assert harness.flow_state()["control_desired"] == "PAUSED"
    harness.run_workload(20, 555)
    harness.stage_result(999)
    harness.cycle(12)
    assert [p["reason"] for p in controls(harness, "REQUEST_CHECKPOINT")] == ["PAUSE"]
    assert [p["reason"] for p in controls(harness, "REQUEST_STOP")] == ["PAUSE"]
    assert calls(harness.api, "reserve_result") == []
    assert failures(harness) == []
    assert len(harness.api.cleanups) == 1


def test_checkpoint_for_pause_before_the_first_state_write_pauses(tmp_path):
    # B15-R20: the pause opens on the first progress frame, which the runner
    # emits right after launch, before the workload's first state write.
    harness = harness_for(tmp_path, restart_safe=True)
    harness.context["execution_intent"] = "CHECKPOINT_FOR_PAUSE"
    harness.dispatch()
    harness.runner.mark_workload_started()
    harness.runner.emit_progress(fraction=0.0, step=0)
    harness.cycle(6)
    assert [p["reason"] for p in controls(harness, "REQUEST_CHECKPOINT")] == ["PAUSE"]
    harness.runner.enforce_deadlines()
    harness.cycle(4)
    assert failures(harness) == []
    assert not harness.api.published
    assert harness.runner.state is RunnerState.RUNNING
    harness.advance(5)
    harness.write_state(3, 11)
    harness.runner.enforce_deadlines()
    harness.cycle(12)
    assert len(harness.api.published) == 1
    assert [p["reason"] for p in controls(harness, "REQUEST_STOP")] == ["PAUSE"]
    assert failures(harness) == []
    assert len(harness.api.cleanups) == 1


def test_rejected_checkpoint_of_a_checkpoint_for_pause_attempt_fails_it_at_once(tmp_path):
    # B15-R28: the server cannot abort this pause (SM:24 needs a policy that
    # allows it; the Attempt may only checkpoint and stop), and the manifest
    # defect is deterministic, so waiting out the pause deadline and failing
    # INFRASTRUCTURE would only consume a retry that repeats it.
    harness = harness_for(tmp_path, restart_safe=True)
    harness.context["execution_intent"] = "CHECKPOINT_FOR_PAUSE"
    harness.api.publish_status = 422
    harness.dispatch()
    harness.run_workload(20, 555)
    harness.cycle(12)
    assert len(calls(harness.api, "reserve_checkpoint")) == 1
    assert failures(harness) == [("INTERNAL", "CHECKPOINT_PROTOCOL_ERROR")]
    assert calls(harness.api, "reserve_result") == []
    assert len(harness.api.cleanups) == 1


# --- cancel -----------------------------------------------------------------------


def test_cancel_revocation_stops_the_container_and_discards_the_rejected_renewal(tmp_path):
    harness = running(tmp_path)
    harness.api.renew_error = "stale_authority"
    with pytest.raises(WorkerApiError):
        harness.agent.renew_once()
    assert len(pending(harness, "renew")) == 1
    record = harness.journal.load(harness.attempt_id)
    harness.api.page = {
        "items": [
            {
                "authority": asdict(harness.authority),
                "authority_state": "REVOKED",
                "claim_state": "STARTED",
                "startup_nonce": record.startup_nonce,
                "expected_container": {
                    "container_id": record.container.container_id,
                    "runtime_identity_digest": record.container.runtime_identity_digest,
                },
                "allocation": {"state": "QUARANTINED"},
                "desired_state": "CANCELLED",
            }
        ],
        "page": {"next_cursor": None},
    }
    result = harness.agent.reconcile_once()
    assert result.complete is True
    assert harness.backend.stopped == [harness.backend.container_id]
    assert len(harness.api.cleanups) == 1
    assert failures(harness) == []
    assert harness.agent.state.operations == {}
    assert harness.attempt_id not in harness.agent._adopted


# --- B14-R05 / B14-R10 ------------------------------------------------------------


def test_partial_input_download_left_by_a_crash_is_fetched_again(tmp_path):
    harness = harness_for(tmp_path)
    partial = tmp_path / "staging" / "downloads" / harness.attempt_id / "input.json"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(INPUT[:5])
    harness.dispatch()
    assert failures(harness) == []
    assert calls(harness.api, "download") == [
        ("download", harness.context["input_artifacts"][0]["artifact_id"])
    ]
    assert partial.read_bytes() == INPUT


def test_checkpoint_tick_racing_a_stopping_runner_is_labelled_by_the_stop(tmp_path):
    harness = running(tmp_path)
    # The runner stopped on its own; its STOPPED frame has not been read yet.
    now = harness.runner.clock()
    harness.runner.request_stop("RUNTIME_LIMIT", now=now)
    harness.runner.stop_workload(now=now, grace_seconds=0)
    harness.advance(6)
    harness.agent._result_once()
    assert failures(harness) == []
    harness.cycle(4)
    assert failures(harness) == [("TIMEOUT", "RUNTIME_LIMIT_REACHED")]


def test_checkpoint_frame_rejected_by_a_stopping_runner_lets_the_stop_frame_through(tmp_path):
    # B15-R14: the runner stops between sending a checkpoint frame and receiving
    # the control that frame drives; the rejected frame must not hide STOPPED.
    harness = running(tmp_path)
    harness.advance(6)
    harness.agent._result_once()
    harness.agent._ipc_once()
    assert harness.flow_state()["pending_execution_message"]["type"] in {
        "CHECKPOINT_FILES_READY",
        "CHECKPOINT_PREPARED",
    }
    now = harness.runner.clock()
    harness.runner.request_stop("RUNTIME_LIMIT", now=now)
    harness.runner.stop_workload(now=now, grace_seconds=0)
    harness.cycle(6)
    assert failures(harness) == [("TIMEOUT", "RUNTIME_LIMIT_REACHED")]
    assert len(harness.api.cleanups) == 1


class ExitingBackend(ListingBackend):
    """Listing fake whose workload container can exit with a given Docker state."""

    exited = None

    def run(self, argv, timeout_seconds):
        if argv[1] == "exec" and self.exited is not None:
            return 1, b"", b"container is not running"
        code, stdout, stderr = super().run(argv, timeout_seconds)
        if argv[1] == "inspect" and code == 0 and self.exited is not None:
            payload = json.loads(stdout)
            payload[0]["State"] = {
                "Running": False,
                "Status": "exited",
                "ExitCode": self.exited,
                "OOMKilled": False,
            }
            stdout = json.dumps(payload).encode()
        return code, stdout, stderr


def _dead_channel(_container_id):
    raise RuntimeError("Docker control relay exited with code 1: container is not running")


def test_stop_frame_seen_only_by_a_renewal_names_the_cause_after_the_runner_exits(tmp_path):
    # B15-R16 (Docker C8, run 12): the runner stopped during a checkpoint cycle.
    # Only a renewal connection, which waits for its own ACK, received STOPPED;
    # the runner then exited 0. The kept frame, not the exit code, names the cause.
    context = claim_context()
    harness = Harness(
        tmp_path,
        context,
        api=ControlApi(context),
        backend=ExitingBackend(tmp_path / "fs" / "output"),
    )
    harness.dispatch()
    harness.run_workload(20, 555)
    harness.cycle(2)
    harness.advance(6)
    harness.agent._result_once()
    harness.agent._ipc_once()
    assert harness.flow_state()["pending_execution_message"]["type"] == "CHECKPOINT_FILES_READY"
    now = harness.runner.clock()
    harness.runner.request_stop("RUNTIME_LIMIT", now=now)
    harness.runner.stop_workload(now=now, grace_seconds=0)
    harness.frames_first = True
    harness.agent.renew_once()
    kept = harness.flow_state()["pending_terminal_message"]
    assert (kept["type"], kept["payload"]["reason"]) == ("STOPPED", "RUNTIME_LIMIT")
    assert harness.runner.stop_frame_acknowledged
    harness.backend.exited = 0
    harness.agent.channel_factory = _dead_channel
    harness.cycle(4, tolerate=(RuntimeError,))
    assert failures(harness) == [("TIMEOUT", "RUNTIME_LIMIT_REACHED")]


def _pause_committed(tmp_path):
    """Pause until the pause checkpoint committed, before the PAUSE stop is sent."""
    context = claim_context()
    harness = Harness(
        tmp_path,
        context,
        api=ControlApi(context),
        backend=ExitingBackend(tmp_path / "fs" / "output"),
    )
    harness.dispatch()
    harness.run_workload(20, 555)
    harness.cycle(2)
    harness.api.desired = "PAUSED"
    harness.agent.renew_once()

    def committed():
        flow = harness.flow_state().get("checkpoint_flow") or {}
        return flow.get("pause_checkpoint") is not None

    for _ in range(20):
        harness.agent._ipc_once()
        if committed():
            break
        harness.agent._result_once()
        if committed():
            break
    assert committed()
    assert controls(harness, "REQUEST_STOP") == []
    # The server moved the Attempt to STOPPING and refuses every failure (B15-R21).
    harness.api.fail_error = "stale_authority"
    return harness


def test_container_exit_after_the_pause_checkpoint_committed_pauses(tmp_path):
    harness = _pause_committed(tmp_path)
    harness.backend.exited = 137
    harness.agent.channel_factory = _dead_channel
    harness.cycle(4, tolerate=(RuntimeError,))
    assert failures(harness) == []
    (cleanup,) = harness.api.cleanups
    assert cleanup["proof"]["container"]["container_id"] == harness.backend.container_id
    assert harness.attempt_id not in harness.agent._adopted


def test_runner_stop_after_the_pause_checkpoint_committed_pauses(tmp_path):
    harness = _pause_committed(tmp_path)
    now = harness.runner.clock()
    harness.runner.request_stop("RUNTIME_LIMIT", now=now)
    harness.runner.stop_workload(now=now, grace_seconds=0)
    harness.cycle(6)
    assert failures(harness) == []
    assert len(harness.api.cleanups) == 1


def test_unconfirmed_pause_stop_after_the_checkpoint_committed_is_forced(tmp_path):
    harness = _pause_committed(tmp_path)
    # The runner no longer answers, so the PAUSE stop is never confirmed.
    harness.agent.channel_factory = _dead_channel
    for _ in range(2):
        with contextlib.suppress(RuntimeError):
            harness.agent._result_once()
    assert harness.api.cleanups == []
    harness.advance(41)
    for _ in range(2):
        with contextlib.suppress(RuntimeError):
            harness.agent._result_once()
    assert failures(harness) == []
    assert len(harness.api.cleanups) == 1


def test_terminal_frame_behind_a_pending_frame_is_kept_once(tmp_path):
    harness = running(tmp_path)
    harness.advance(6)
    harness.agent._result_once()
    harness.agent._ipc_once()
    pending = harness.flow_state()["pending_execution_message"]
    base = int(pending["message_sequence"])

    def terminal(sequence, reason):
        return {
            "schema_version": 1,
            "message_sequence": sequence,
            "type": "STOPPED",
            "payload": {"reason": reason, "exit_code": 143, "stopped_monotonic_ns": 1},
        }

    record = harness.agent._record_runner_message
    assert record(harness.attempt_id, terminal(base + 2, "RUNTIME_LIMIT")) == "OUT_OF_ORDER"
    assert record(harness.attempt_id, terminal(base + 2, "RUNTIME_LIMIT")) == "OUT_OF_ORDER"
    assert record(harness.attempt_id, terminal(base + 3, "FAILURE")) == "INVALID"
    state = harness.flow_state()
    assert state["pending_execution_message"] == pending
    assert state["pending_terminal_message"]["message_sequence"] == base + 2


# --- runner deadlines -------------------------------------------------------------


def test_runner_runtime_limit_stop_is_a_timeout(tmp_path):
    harness = running(tmp_path)
    deadline = harness.runner.runtime_started_at + harness.runner.runtime_limit_seconds
    harness.runner.accept_authority_deadline(deadline + 60)
    harness.runner.enforce_deadlines(now=deadline)
    harness.cycle(4)
    assert failures(harness) == [("TIMEOUT", "RUNTIME_LIMIT_REACHED")]
    assert len(harness.api.cleanups) == 1


def test_failing_renewals_let_the_runner_stop_itself_at_its_authority_deadline(tmp_path):
    harness = running(tmp_path)
    deadline = harness.runner.authority_deadline
    assert deadline is not None
    # The API is unreachable: no renewal extends the runner's deadline.
    harness.api.renew_error = "dependency_unavailable"
    for _ in range(3):
        with contextlib.suppress(WorkerApiError):
            harness.agent.renew_once()
        harness.advance(5)
    assert len(harness.api.renewals) >= 1
    assert harness.runner.authority_deadline == deadline
    # The runner's own watchdog stops compute; no agent call is involved.
    harness.runner.enforce_deadlines(now=deadline)
    assert harness.runner.state is RunnerState.STOPPED
    (stopped,) = [m for m in harness.runner.pending_messages if m["type"] == "STOPPED"]
    assert stopped["payload"]["reason"] == "LEASE_DEADLINE"
    # Lost authority is an infrastructure fact, so the job may retry or recover.
    harness.cycle(4)
    assert failures(harness) == [("INFRASTRUCTURE", "RUNNER_UNAVAILABLE")]


def test_failure_rejected_after_the_reaper_revoked_the_lease_ends_with_verified_cleanup(
    tmp_path,
):
    # B15-R17 (Docker C9, run 13): the network was lost, the runner stopped at its
    # deadline and the reaper revoked the lease. The failure recorded meanwhile is
    # stale (409) and can never be accepted; it must not block reconciliation once
    # the exact stopped container's cleanup is verified.
    harness = running(tmp_path)
    deadline = harness.runner.authority_deadline
    harness.api.renew_error = "dependency_unavailable"
    harness.runner.enforce_deadlines(now=deadline)
    harness.api.fail_error = "state_conflict"
    harness.cycle(4, tolerate=(WorkerApiError,))
    assert failures(harness)[-1] == ("INFRASTRUCTURE", "RUNNER_UNAVAILABLE")
    assert len(pending(harness, "failure")) == 1
    record = harness.journal.load(harness.attempt_id)
    harness.api.page = {
        "items": [
            {
                "authority": asdict(harness.authority),
                "authority_state": "REVOKED",
                "claim_state": "STARTED",
                "startup_nonce": record.startup_nonce,
                "expected_container": {
                    "container_id": record.container.container_id,
                    "runtime_identity_digest": record.container.runtime_identity_digest,
                },
                "allocation": {"state": "QUARANTINED"},
                "desired_state": "RUNNING",
            }
        ],
        "page": {"next_cursor": None},
    }
    result = harness.agent.reconcile_once()
    assert result.complete is True
    assert len(harness.api.cleanups) == 1
    assert harness.agent.state.operations == {}
    sent = len(harness.api.failures)
    harness.api.page = {"items": [], "page": {"next_cursor": None}}
    harness.cycle(4)
    assert harness.agent.reconcile_once().complete is True
    assert harness.agent.state.operations == {}
    assert len(harness.api.failures) == sent


def test_cleanup_replayed_before_a_stale_failure_discards_it_during_reconciliation(tmp_path):
    # The replay loop visits a journaled cleanup first; once it is verified the
    # failure after it is gone and is neither sent nor looked up (B15-R17).
    harness = running(tmp_path)
    attempt = harness.attempt_id
    harness.agent.state.begin(
        "01a0e012-0000-7000-8000-000000000001",
        operation="cleanup",
        payload={"attempt_id": attempt, "body": {"attempt_id": attempt}},
    )
    harness.agent.state.begin(
        "01a0e012-0000-7000-8000-000000000002",
        operation="failure",
        payload={"attempt_id": attempt, "body": {"authority": asdict(harness.authority)}},
    )
    harness.api.fail_error = "state_conflict"
    harness.agent.reconcile_once()
    assert len(harness.api.cleanups) == 1
    assert harness.api.failures == []
    assert harness.agent.state.operations == {}


def test_runner_rejection_that_stops_for_failure_stays_a_protocol_error(tmp_path):
    harness = running(tmp_path)
    harness.advance(6)
    # A missing state waits (B15-R20); an invalid one is rejected at once.
    (harness.fs / "output" / "state.json").write_bytes(b"{}")
    harness.cycle(4)
    assert failures(harness) == [("INTERNAL", "CHECKPOINT_PROTOCOL_ERROR")]


def test_pause_stop_frame_without_a_pause_request_is_a_failure(tmp_path):
    harness = running(tmp_path)
    now = harness.runner.clock()
    harness.runner.request_stop("PAUSE", now=now)
    harness.runner.stop_workload(now=now, grace_seconds=0)
    harness.cycle(4)
    assert failures(harness) == [("INTERNAL", "WORKLOAD_EXIT_NONZERO")]


def test_runner_takes_a_pause_checkpoint_then_stops_for_pause(tmp_path):
    runner = _started(_runner(tmp_path))
    _write_state(tmp_path)
    assert runner.apply_control(_request(runner, 1, reason="PAUSE")) == "ACCEPTED"
    assert any(m["type"] == "CHECKPOINT_FILES_READY" for m in runner.pending_messages)
    stop = {
        "schema_version": 1,
        "control_sequence": 2,
        "type": "REQUEST_STOP",
        "payload": {
            "reason": "PAUSE",
            "grace_deadline_monotonic_ns": int((runner.clock() + 5) * SECOND),
        },
    }
    assert runner.apply_control(stop) == "ACCEPTED"
    (stopped,) = [m for m in runner.pending_messages if m["type"] == "STOPPED"]
    assert stopped["payload"]["reason"] == "PAUSE"
    assert runner.state is RunnerState.STOPPED
