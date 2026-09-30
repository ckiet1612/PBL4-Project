"""Remediation B15-R10/R14: the runner names every stop the worker may not have read.

R10: a stop at the 30-second startup limit reports ``STARTUP_LIMIT``, distinct from a
runtime-limit stop, so the worker can report STARTUP_TIMEOUT (openapi ~1339).
R14: a stop whose STOPPED frame no worker kept ends the container with a status that
names the stop reason instead of exit 0 without a frame, and a watchdog stop that
cannot be confirmed fails closed within a bound instead of leaving the runner
STOPPING until reconciliation removes the container.
"""

import threading
import time

import pytest

from nexa.worker import protocol
from nexa.worker.protocol import ProtocolError, validate_control_envelope, validate_envelope
from nexa.workloads import trusted_runner
from nexa.workloads.trusted_runner import RunnerState, RunnerSupervisor
from tests.workloads.test_runner import (
    _ack,
    _connect,
    _control_socket_path,
    _receive_stopped,
    _remove_control_socket_directories,
    _serving,
    launch_spec,
)


@pytest.fixture(autouse=True)
def _control_socket_cleanup():
    yield
    _remove_control_socket_directories()


def _runner(clock, **kwargs):
    return RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=kwargs.pop("runtime_limit_seconds", 300),
        stop_grace_seconds=5,
        clock=clock,
        **kwargs,
    )


def _stopped(clock, reason):
    runner = _runner(clock)
    runner.accept_authority_deadline(clock() + 1_000)
    runner.mark_workload_started()
    runner.request_stop(reason, now=clock())
    runner.stop_workload(now=clock(), grace_seconds=0)
    assert runner.state is RunnerState.STOPPED
    return runner


def _expire_startup(runner, now):
    """Pass the startup limit with no worker connected, then the stop-frame linger.

    Either the watchdog stops the runner and the control loop lingers for a worker
    from the stop it observes, or the loop stops it at its own startup timeout and
    ends at once; the frozen clock moves past the linger until the loop has ended.
    """
    server, _ = _serving(runner, timeout_seconds=30)
    now[0] = 30.0
    for _ in range(10):
        server.join(timeout=0.3)
        if not server.is_alive():
            break
        now[0] += trusted_runner.STOP_FRAME_LINGER_SECONDS
    assert runner.state is RunnerState.STOPPED
    return server


def _stop_reason(runner):
    stopped = runner.pending_messages[-1]
    assert stopped["type"] == "STOPPED"
    return stopped["payload"]["reason"]


def test_startup_limit_stop_is_distinct_from_a_runtime_limit_stop():
    now = [0.0]
    startup = _runner(lambda: now[0], launch_spec=launch_spec())
    startup.accept_authority_deadline(40.0)
    now[0] = 30.0
    startup.enforce_deadlines()
    assert startup.state is RunnerState.STOPPED
    assert _stop_reason(startup) == "STARTUP_LIMIT"

    runtime = _runner(lambda: now[0], runtime_limit_seconds=1)
    now[0] = 0.0
    runtime.accept_authority_deadline(100.0)
    runtime.mark_workload_started(now=0.0)
    runtime.enforce_deadlines(now=1.01)
    assert _stop_reason(runtime) == "RUNTIME_LIMIT"


def test_runner_that_never_receives_authority_stops_for_its_startup_limit():
    # No worker ever accepted a deadline: serve_control ends the WAITING_AUTHORITY
    # runner at its startup limit, not a runtime limit it never started.
    now = [0.0]
    runner = _runner(lambda: now[0])
    server = _expire_startup(runner, now)
    assert not server.is_alive()
    assert _stop_reason(runner) == "STARTUP_LIMIT"


def test_startup_limit_is_a_runner_stop_reason_but_not_a_worker_stop_control():
    def stopped(reason):
        return {
            "schema_version": 1,
            "message_sequence": 1,
            "type": "STOPPED",
            "payload": {"reason": reason, "exit_code": -1, "stopped_monotonic_ns": 1},
        }

    assert validate_envelope(stopped("STARTUP_LIMIT"))["payload"]["reason"] == "STARTUP_LIMIT"
    # Frames of runners from before this change keep parsing.
    assert validate_envelope(stopped("RUNTIME_LIMIT"))["payload"]["reason"] == "RUNTIME_LIMIT"
    with pytest.raises(ProtocolError, match="stop reason"):
        validate_control_envelope(
            {
                "schema_version": 1,
                "control_sequence": 1,
                "type": "REQUEST_STOP",
                "payload": {"reason": "STARTUP_LIMIT", "grace_deadline_monotonic_ns": 1},
            }
        )


def test_every_stop_reason_has_a_distinct_exit_status_outside_other_runner_statuses():
    codes = protocol.STOP_EXIT_CODES
    assert set(codes) == {
        "PAUSE",
        "CANCEL",
        "LEASE_DEADLINE",
        "FAILURE",
        "RUNTIME_LIMIT",
        "SHUTDOWN",
        "STARTUP_LIMIT",
    }
    assert len(set(codes.values())) == len(codes)
    # 0 (kept frame), 78 (unreadable launch spec), 124 (fail closed) and signal
    # statuses (128+n) keep their meaning.
    assert all(1 <= code < 124 and code != 78 for code in codes.values())


def test_stop_without_a_worker_acknowledgment_exits_with_its_reason():
    # The B15 residual: a runner that stopped while no worker was connected
    # lingered 3 s and then exited 0 without a frame (RUNNER_PROTOCOL_ERROR).
    now = [0.0]
    runner = _stopped(lambda: now[0], "LEASE_DEADLINE")
    server, _ = _serving(runner)
    server.join(timeout=0.3)
    assert server.is_alive()
    now[0] = trusted_runner.STOP_FRAME_LINGER_SECONDS
    server.join(timeout=2)
    assert not server.is_alive()
    assert runner.stop_frame_acknowledged is False
    expected = protocol.STOP_EXIT_CODES["LEASE_DEADLINE"]
    assert trusted_runner.exit_status(runner, failed=False) == expected


def test_startup_stop_without_authority_exits_with_the_startup_reason():
    now = [0.0]
    runner = _runner(lambda: now[0])
    server = _expire_startup(runner, now)
    assert not server.is_alive()
    expected = protocol.STOP_EXIT_CODES["STARTUP_LIMIT"]
    assert trusted_runner.exit_status(runner, failed=False) == expected


def test_stop_a_worker_kept_exits_zero():
    runner = _stopped(lambda: 0.0, "RUNTIME_LIMIT")
    server, path = _serving(runner)
    with _connect(path) as keeping:
        _ack(keeping, _receive_stopped(keeping)["message_sequence"], "ACCEPTED")
        server.join(timeout=2)
    assert not server.is_alive()
    assert trusted_runner.exit_status(runner, failed=False) == 0


def test_fail_closed_without_a_stop_reason_keeps_exit_124():
    runner = _runner(lambda: 0.0)
    runner.fail_closed()
    assert trusted_runner.exit_status(runner, failed=True) == 124


def test_unconfirmed_watchdog_stop_fails_closed_within_a_bound():
    now = [0.0]
    runner = _runner(lambda: now[0], runtime_limit_seconds=1)
    runner.accept_authority_deadline(1_000.0)
    runner.mark_workload_started(now=0.0)
    attempts = []

    def unconfirmed_stop(*, now, grace_seconds=5):
        attempts.append(now)
        raise RuntimeError("workload supervisor did not confirm stop")

    runner.stop_workload = unconfirmed_stop
    path = _control_socket_path(prefix="nexa-rem-r14-")
    errors = []

    def serve():
        try:
            trusted_runner.serve_control(runner, path)
        except RuntimeError as exc:
            errors.append(exc)

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    began = time.monotonic()
    now[0] = 1.5
    server.join(timeout=3)
    elapsed = time.monotonic() - began

    # Before the fix the watchdog left the runner STOPPING and the control
    # server kept serving until reconciliation removed the container.
    assert not server.is_alive(), "runner kept serving after an unconfirmed stop"
    assert elapsed < 2
    assert attempts, "the watchdog never tried to stop the workload"
    assert [str(exc) for exc in errors] == ["trusted runner entered fail-closed termination"]
    assert runner.fatal_error is True
    assert runner.state is RunnerState.STOPPING
    # No false STOPPED frame: the stop was never confirmed.
    assert all(item["type"] != "STOPPED" for item in runner.pending_messages)
    expected = protocol.STOP_EXIT_CODES["RUNTIME_LIMIT"]
    assert trusted_runner.exit_status(runner, failed=True) == expected


class _ResetSupervisor:
    """Supervisor connection that registers, confirms nothing and resets after ``TERM``.

    Linux reports a peer that closed with unread data (the runner's ``TERM``) as
    ``ECONNRESET`` rather than end of file; macOS reports end of file.
    """

    def __init__(self):
        self.greeted = False
        self.terminated = threading.Event()
        self.sent = []

    def settimeout(self, timeout):
        del timeout

    def sendall(self, data):
        self.sent.append(bytes(data))
        if data.startswith(b"TERM "):
            self.terminated.set()

    def recv(self, size):
        del size
        if not self.greeted:
            self.greeted = True
            return b"READY\n"
        if self.terminated.wait(timeout=10):
            raise ConnectionResetError(104, "Connection reset by peer")
        return b""

    def close(self):
        pass


def test_supervisor_reset_after_term_is_not_a_confirmed_stop(monkeypatch):
    # REM-R01: a reset supervisor connection set the exit event without an EXIT
    # report, so stop_workload confirmed a stop it never observed and emitted STOPPED
    # (a 1-in-25 flake of test_supervisor_disconnect_after_start_sets_fatal_... on Linux).
    runner = _runner(lambda: 0.0, launch_spec=launch_spec())
    runner.accept_authority_deadline(100.0)
    monkeypatch.setattr(runner._supervisor_started, "wait", lambda timeout: False)
    supervisor = _ResetSupervisor()
    with pytest.raises(RuntimeError, match="did not confirm start"):
        runner.attach_supervisor_connection(supervisor, supervisor_pid=1234)
    assert b"START\n" in supervisor.sent
    runner.request_stop("FAILURE", now=0.0)

    with pytest.raises(RuntimeError, match="exit report"):
        runner.stop_workload(now=0.0, grace_seconds=0)

    assert supervisor.terminated.is_set()
    assert all(item["type"] != "STOPPED" for item in runner.pending_messages)
    assert runner.state is not RunnerState.STOPPED
