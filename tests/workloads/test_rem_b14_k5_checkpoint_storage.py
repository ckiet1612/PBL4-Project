"""Remediation B14-K5: the runner runs out of space copying checkpoint bytes.

``/output`` is the attempt's own bounded tmpfs, shared by the workload and the runner's
staging copies. Before the fix an ``OSError`` (ENOSPC/EDQUOT/EIO) raised while the
runner staged, bound or finalized a checkpoint escaped ``apply_control``: the runner
exited 124 and the worker reported retryable INFRASTRUCTURE/RUNNER_UNAVAILABLE, so a
deterministic "disk full" spent the retry budget. The runner now fails the attempt with
its own safe, non-retryable FAILED frame INTERNAL/CHECKPOINT_STORAGE_FAILED and stops
the workload; nothing it could not copy completely is ever announced.
"""

import errno
import os
from pathlib import Path

import pytest

from nexa.worker.protocol import STOP_EXIT_CODES
from nexa.workloads import trusted_runner
from nexa.workloads.trusted_runner import RunnerState
from tests.workloads import b16_helpers as h
from tests.workloads import test_runner_adapter_b16 as adapter
from tests.workloads.test_runner_checkpoint_b14 import (
    _bind,
    _binding_set_checksum,
    _finalize,
    _last,
    _request,
    _runner,
    _started,
    _write_state,
)

STORAGE_ERRNOS = [errno.ENOSPC, errno.EDQUOT, errno.EIO]
FAILED = {
    "failure_class": "INTERNAL",
    "reason_code": "CHECKPOINT_STORAGE_FAILED",
    "exit_code": None,
    "oom_killed": False,
    "runtime_limit_reached": False,
}


def _fail_writes(monkeypatch, code, *, name=lambda path: path.name.startswith("checkpoint-")):
    """Fail the runner's staging writes selected by ``name``; runner state still persists."""
    write = trusted_runner._atomic_write
    failed = []

    def failing(path, payload, *, mode):
        if name(path):
            failed.append(path.name)
            raise OSError(code, os.strerror(code))
        write(path, payload, mode=mode)

    monkeypatch.setattr(trusted_runner, "_atomic_write", failing)
    return failed


def _types(runner):
    return [message["type"] for message in runner.pending_messages]


def _assert_storage_failure(runner):
    (failed,) = [m["payload"] for m in runner.pending_messages if m["type"] == "FAILED"]
    assert failed == FAILED
    assert runner.stop_reason == "FAILURE"
    assert runner.state is RunnerState.STOPPED
    # Without a worker ACK the exit status still names a FAILURE stop, never 124.
    assert trusted_runner.exit_status(runner, failed=False) == STOP_EXIT_CODES["FAILURE"]


@pytest.mark.parametrize("code", STORAGE_ERRNOS, ids=errno.errorcode.get)
def test_staging_copy_without_space_fails_the_attempt_internal(tmp_path: Path, monkeypatch, code):
    runner = _started(_runner(tmp_path))
    _write_state(tmp_path)
    failed = _fail_writes(monkeypatch, code)

    assert runner.apply_control(_request(runner, 1)) == "INVALID"
    assert failed == ["checkpoint-1-state.json"]
    _assert_storage_failure(runner)
    assert "CHECKPOINT_FILES_READY" not in _types(runner)
    # No partial copy is left behind under the staging name or a temporary one.
    assert sorted(path.name for path in (tmp_path / "output").iterdir()) == ["state.json"]
    # The FAILED frame is persisted: a reconnecting worker still reads it.
    reloaded = _runner(tmp_path)
    assert [m["payload"] for m in reloaded.pending_messages if m["type"] == "FAILED"] == [FAILED]


def test_state_read_io_error_is_a_storage_failure(tmp_path: Path, monkeypatch):
    runner = _started(_runner(tmp_path))
    _write_state(tmp_path)

    def unreadable(path):
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(trusted_runner, "read_state_file", unreadable)
    assert runner.apply_control(_request(runner, 1)) == "INVALID"
    _assert_storage_failure(runner)


def test_manifest_write_without_space_fails_before_checkpoint_ready(tmp_path: Path, monkeypatch):
    runner = _started(_runner(tmp_path))
    _write_state(tmp_path)
    assert runner.apply_control(_request(runner, 1)) == "ACCEPTED"
    bind = _bind(2, _last(runner, "CHECKPOINT_FILES_READY"))
    assert runner.apply_control(bind) == "ACCEPTED"
    failed = _fail_writes(monkeypatch, errno.ENOSPC)

    checksum = _binding_set_checksum(bind["payload"]["bindings"])
    assert runner.apply_control(_finalize(3, checksum)) == "INVALID"
    assert failed == ["checkpoint-1-manifest.json"]
    _assert_storage_failure(runner)
    assert "CHECKPOINT_READY" not in _types(runner)


def test_waiting_request_that_cannot_stage_fails_the_attempt(tmp_path: Path, monkeypatch):
    # B15-R20: a request before the first snapshot waits; its later staging copy runs
    # from the deadline watchdog, not from a control, and fails the same way.
    runner = _started(_runner(tmp_path))
    assert runner.apply_control(_request(runner, 1)) == "ACCEPTED"
    assert "CHECKPOINT_FILES_READY" not in _types(runner)
    _write_state(tmp_path)
    _fail_writes(monkeypatch, errno.ENOSPC)

    runner.enforce_deadlines()
    _assert_storage_failure(runner)
    assert "CHECKPOINT_FILES_READY" not in _types(runner)


def test_training_checkpoint_never_announces_a_partial_copy(tmp_path: Path, monkeypatch):
    runner = adapter._started(adapter._runner(tmp_path))
    adapter._write_snapshot(tmp_path, h.per_epoch())
    second = f"checkpoint-1-{adapter.CHECKPOINT_FILES[1]}"
    failed = _fail_writes(monkeypatch, errno.ENOSPC, name=lambda path: path.name == second)

    assert runner.apply_control(adapter._request(runner, 1)) == "INVALID"
    assert failed == [second]
    _assert_storage_failure(runner)
    assert "CHECKPOINT_FILES_READY" not in _types(runner)


def test_other_os_errors_are_not_reported_as_storage_failures(tmp_path: Path, monkeypatch):
    # Only a full or failing device is a storage failure; any other OSError keeps the
    # existing fail-closed runner exit (the worker then classifies the container exit).
    runner = _started(_runner(tmp_path))
    _write_state(tmp_path)
    _fail_writes(monkeypatch, errno.EACCES)

    with pytest.raises(PermissionError):
        runner.apply_control(_request(runner, 1))
    assert "FAILED" not in _types(runner)
