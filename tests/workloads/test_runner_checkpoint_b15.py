"""B15-R20: a checkpoint requested before the workload's first state write waits."""

from pathlib import Path

from nexa.workloads.trusted_runner import RunnerState
from tests.workloads.test_runner_checkpoint_b14 import (
    CALLBACK_2,
    CHECKPOINT,
    CHECKPOINT_2,
    _bind,
    _binding_set_checksum,
    _finalize,
    _last,
    _request,
    _runner,
    _stage,
    _started,
    _write_state,
)


def _files_ready(runner):
    return [m for m in runner.pending_messages if m["type"] == "CHECKPOINT_FILES_READY"]


def test_pause_request_before_the_first_state_write_waits_for_it(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path))

    assert runner.apply_control(_request(runner, 1, reason="PAUSE")) == "ACCEPTED"
    assert runner.state is RunnerState.RUNNING
    runner.enforce_deadlines()
    assert runner.state is RunnerState.RUNNING
    assert _files_ready(runner) == []

    state = _write_state(tmp_path, step=3, accumulator=11)
    runner.enforce_deadlines()
    files = _last(runner, "CHECKPOINT_FILES_READY")
    assert files["payload"]["checkpoint_id"] == CHECKPOINT
    descriptor = files["payload"]["artifacts"][0]
    assert (tmp_path / "output" / descriptor["staging_name"]).read_bytes() == state
    runner.enforce_deadlines()
    assert len(_files_ready(runner)) == 1

    bind = _bind(2, files)
    assert runner.apply_control(bind) == "ACCEPTED"
    checksum = _binding_set_checksum(bind["payload"]["bindings"])
    assert runner.apply_control(_finalize(3, checksum)) == "ACCEPTED"
    assert _last(runner, "CHECKPOINT_READY")["payload"]["checkpoint_id"] == CHECKPOINT
    assert runner.state is RunnerState.RUNNING


def test_waiting_request_replays_and_survives_reload(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path))
    request = _request(runner, 1, reason="PAUSE")
    assert runner.apply_control(request) == "ACCEPTED"
    # A restarted worker resends the frozen control on a fresh sequence.
    assert runner.apply_control({**request, "control_sequence": 2}) == "ACCEPTED"
    assert runner.state is RunnerState.RUNNING

    reloaded = _runner(tmp_path)
    assert reloaded.state is RunnerState.RUNNING
    _write_state(tmp_path)
    reloaded.enforce_deadlines()
    assert _last(reloaded, "CHECKPOINT_FILES_READY")["payload"]["checkpoint_id"] == CHECKPOINT


def test_another_request_while_one_waits_is_rejected(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path))
    assert runner.apply_control(_request(runner, 1)) == "ACCEPTED"
    other = _request(
        runner,
        2,
        checkpoint=CHECKPOINT_2,
        checkpoint_sequence=2,
        reservation_callback_id=CALLBACK_2,
    )
    assert runner.apply_control(other) == "INVALID"
    assert runner.state is RunnerState.STOPPED


def test_state_that_never_appears_fails_at_the_checkpoint_deadline(tmp_path: Path) -> None:
    now = [100.0]
    runner = _started(_runner(tmp_path, clock=lambda: now[0]))
    request = _request(runner, 1, reason="PAUSE")
    request["payload"]["checkpoint_deadline_monotonic_ns"] = int(110 * 1_000_000_000)
    assert runner.apply_control(request) == "ACCEPTED"
    now[0] = 109.0
    runner.enforce_deadlines()
    assert runner.state is RunnerState.RUNNING
    now[0] = 110.0
    runner.enforce_deadlines()
    assert runner.state is RunnerState.STOPPED
    assert _files_ready(runner) == []


def test_invalid_state_that_appears_later_still_fails_closed(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path))
    assert runner.apply_control(_request(runner, 1)) == "ACCEPTED"
    (tmp_path / "output" / "state.json").write_bytes(b"{}")
    runner.enforce_deadlines()
    assert runner.state is RunnerState.STOPPED
    assert _files_ready(runner) == []


def test_result_staged_while_a_request_waits_is_deferred(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path))
    assert runner.apply_control(_request(runner, 1, reason="PAUSE")) == "ACCEPTED"
    _write_state(tmp_path, step=50, accumulator=1)
    _stage(runner, tmp_path)
    assert all(m["type"] != "RESULT_PREPARE" for m in runner.pending_messages)
    runner.enforce_deadlines()
    files = _last(runner, "CHECKPOINT_FILES_READY")
    bind = _bind(2, files)
    assert runner.apply_control(bind) == "ACCEPTED"
    checksum = _binding_set_checksum(bind["payload"]["bindings"])
    assert runner.apply_control(_finalize(3, checksum)) == "ACCEPTED"
    assert _last(runner, "RESULT_PREPARE") is not None
