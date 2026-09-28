"""B16-R26: a workload killed by its container cgroup OOM is reported as FAILED OOM."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from nexa.workloads.trusted_runner import MEMORY_EVENTS_PATH, RunnerSupervisor
from tests.workloads import b16_helpers as h
from tests.workloads.test_runner import launch_spec as cpu_launch_spec


def _memory_events(tmp_path: Path, oom_kill: int | None) -> None:
    path = tmp_path / MEMORY_EVENTS_PATH
    if oom_kill is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Field layout of cgroup v2 memory.events.
    path.write_text(
        f"low 0\nhigh 0\nmax 7\noom {oom_kill}\noom_kill {oom_kill}\noom_group_kill 0\n"
    )


def _runner(tmp_path: Path, spec: dict) -> RunnerSupervisor:
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)
    return RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        state_path=tmp_path / "run" / "runner-state.json",
        launch_spec=spec,
        fs_root=tmp_path,
    )


def _run_to_exit(
    tmp_path: Path, spec: dict, *, before: int | None, after: int | None, exit_code: int
) -> tuple[RunnerSupervisor, dict]:
    """Launch through an emulated workload supervisor; the counter moves while it runs."""
    _memory_events(tmp_path, before)
    runner = _runner(tmp_path, spec)
    runner.accept_authority_deadline(runner.clock() + 10_000.0)
    runner_side, supervisor_side = socket.socketpair()

    def emulate_supervisor() -> None:
        supervisor_side.sendall(b"READY\n")
        assert supervisor_side.recv(64) == b"START\n"
        _memory_events(tmp_path, after)
        supervisor_side.sendall(f"STARTED 4321\nEXIT {exit_code}\n".encode())

    peer = threading.Thread(target=emulate_supervisor)
    peer.start()
    try:
        runner.attach_supervisor_connection(runner_side, supervisor_pid=1234)
        runner._launch_from_spec()
        peer.join(timeout=2)
        # The monitor thread emits FAILED after the supervisor reports EXIT.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            failed = [m for m in runner.pending_messages if m["type"] == "FAILED"]
            if failed:
                return runner, failed[-1]["payload"]
            time.sleep(0.01)
        raise AssertionError("runner emitted no FAILED frame")
    finally:
        supervisor_side.close()
        runner.close()


SPECS = {"pytorch": h.training_spec, "inference": h.inference_spec, "cpu": cpu_launch_spec}
INTERNAL = ("INTERNAL", "WORKLOAD_EXIT_NONZERO")


@pytest.mark.parametrize("adapter", sorted(SPECS))
@pytest.mark.parametrize(
    ("before", "after", "exit_code", "expected"),
    [
        (0, 1, 137, ("OOM", "CONTAINER_OOM")),
        (2, 3, -9, ("OOM", "CONTAINER_OOM")),
        # A counter that was already non-zero at START never makes an OOM.
        (2, 2, 137, INTERNAL),
        # Unreadable at START or at exit: no evidence, so no OOM claim.
        (None, 1, 137, INTERNAL),
        (0, None, 137, INTERNAL),
        (0, 0, 1, INTERNAL),
    ],
)
def test_cgroup_oom_kill_rise_is_failed_oom(
    tmp_path: Path,
    adapter: str,
    before: int | None,
    after: int | None,
    exit_code: int,
    expected: tuple[str, str],
) -> None:
    runner, failed = _run_to_exit(
        tmp_path, SPECS[adapter](), before=before, after=after, exit_code=exit_code
    )
    assert (failed["failure_class"], failed["reason_code"]) == expected
    assert failed["oom_killed"] is (expected[0] == "OOM")
    assert failed["exit_code"] == (137 if exit_code == -9 else exit_code)
    assert failed["runtime_limit_reached"] is False
    assert all(m["type"] != "RESULT_PREPARE" for m in runner.pending_messages)


@pytest.mark.parametrize("adapter", ["pytorch", "inference"])
def test_adapter_invalid_input_exit_stays_invalid_input_even_with_an_oom_rise(
    tmp_path: Path, adapter: str
) -> None:
    _, failed = _run_to_exit(tmp_path, SPECS[adapter](), before=0, after=1, exit_code=65)
    assert (failed["failure_class"], failed["reason_code"]) == ("INVALID_INPUT", "INVALID_INPUT")
    assert failed["oom_killed"] is False


def test_oom_kill_baseline_survives_runner_state_reload(tmp_path: Path) -> None:
    runner, _ = _run_to_exit(tmp_path, h.training_spec(), before=4, after=5, exit_code=137)
    assert runner._oom_kill_baseline == 4
    assert _runner(tmp_path, h.training_spec())._oom_kill_baseline == 4


@pytest.mark.parametrize(
    "content",
    [b"", b"oom_kill\n", b"oom_kill x\n", b"oom_kill -1\n", b"oom 3\n", b"x" * 5000],
)
def test_malformed_memory_events_is_no_evidence(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / MEMORY_EVENTS_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    assert _runner(tmp_path, h.training_spec())._read_oom_kill_count() is None
