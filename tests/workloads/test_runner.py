import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest

from nexa.worker.protocol import ProtocolError
from nexa.workloads import trusted_runner
from nexa.workloads.trusted_runner import RunnerState, RunnerSupervisor

CALLBACK = "018f0d60-7b6a-7a21-9d82-1aa39c4f30b7"
COMPLETION = "018f0d60-7b6a-7a22-9d82-1aa39c4f30b7"
RESULT = "018f0d60-7b6a-7a23-9d82-1aa39c4f30b7"
ARTIFACT = "018f0d60-7b6a-7a24-9d82-1aa39c4f30b7"
TENANT = "018f0d60-7b6a-7a25-9d82-1aa39c4f30b7"
JOB = "018f0d60-7b6a-7a26-9d82-1aa39c4f30b7"
SESSION = "018f0d60-7b6a-7a27-9d82-1aa39c4f30b7"
SPEC_CHECKSUM = "sha256:" + "f" * 64


def launch_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "adapter_id": "cpu.iterative",
        "startup_nonce": COMPLETION,
        "input_path": "/input/input.json",
        "output_path": "/output/result.json",
        "result_logical_name": "result.json",
        "media_type": "application/vnd.nexa.cpu-iterative-result+json",
        "iterations": 3,
        "seed": 7,
        "modulus": 101,
        "spec_checksum": SPEC_CHECKSUM,
        "provenance": result_provenance(),
    }


def test_runner_rejects_supervisor_registration_from_wrong_uid() -> None:
    class Peer:
        def getsockopt(self, level: int, option: int, size: int) -> bytes:
            del level, option, size
            return struct.pack("3i", 4321, 1000, 1000)

    with pytest.raises(PermissionError, match="UID 1001"):
        trusted_runner.validate_supervisor_peer(Peer())  # type: ignore[arg-type]


def test_runner_controls_registered_supervisor_without_spawning_as_root() -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: 0.0,
    )
    runner_side, supervisor_side = socket.socketpair()
    observed: list[bytes] = []

    def emulate_supervisor() -> None:
        supervisor_side.sendall(b"READY\n")
        observed.append(supervisor_side.recv(64))
        supervisor_side.sendall(b"STARTED 4321\n")
        supervisor_side.sendall(b"EXIT 0\n")

    peer = threading.Thread(target=emulate_supervisor)
    peer.start()
    try:
        runner.attach_supervisor_connection(runner_side, supervisor_pid=1234)
        runner.accept_authority_deadline(runner.clock() + 10_000.0)
        assert runner.launch_workload(("unused",)) == 4321
        peer.join(timeout=2)
        assert observed == [b"START\n"]
    finally:
        supervisor_side.close()
        runner.close()


def test_authority_before_registration_launches_once_after_supervisor_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1.0]
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: now[0],
        launch_spec=launch_spec(),
    )

    def forbid_fallback(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("production launch must wait for supervisor registration")

    monkeypatch.setattr(runner, "prepare_workload", forbid_fallback)
    authority = {
        "schema_version": 1,
        "control_sequence": 1,
        "type": "SET_AUTHORITY_DEADLINE",
        "payload": {
            "source_callback_id": CALLBACK,
            "deadline_monotonic_ns": 10_000_000_000,
        },
    }
    assert runner.apply_control(authority) == "ACCEPTED"
    assert runner.compute_allowed is True
    assert runner._workload_started is False

    runner_side, supervisor_side = socket.socketpair()
    observed: list[bytes] = []

    def emulate_supervisor() -> None:
        supervisor_side.sendall(b"READY\n")
        observed.append(supervisor_side.recv(64))
        supervisor_side.sendall(b"STARTED 4321\n")

    peer = threading.Thread(target=emulate_supervisor)
    peer.start()
    try:
        runner.attach_supervisor_connection(runner_side, supervisor_pid=1234)
        peer.join(timeout=2)
        runner._launch_from_spec()
        assert observed == [b"START\n"]
        assert runner.workload_process_group == 4321
        assert [item["type"] for item in runner.pending_messages].count("STARTED") == 1
    finally:
        supervisor_side.close()
        runner.close()


def test_authority_expiring_before_registration_never_launches() -> None:
    now = [1.0]
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: now[0],
        launch_spec=launch_spec(),
    )
    runner.accept_authority_deadline(2.0)
    now[0] = 3.0
    runner_side, supervisor_side = socket.socketpair()
    observed: list[bytes] = []

    def emulate_supervisor() -> None:
        supervisor_side.sendall(b"READY\n")
        supervisor_side.settimeout(0.3)
        with suppress(TimeoutError):
            observed.append(supervisor_side.recv(64))

    peer = threading.Thread(target=emulate_supervisor)
    peer.start()
    try:
        runner.attach_supervisor_connection(runner_side, supervisor_pid=1234)
        peer.join(timeout=2)
        assert observed == []
        assert runner.state is RunnerState.STOPPED
        assert all(message["type"] != "STARTED" for message in runner.pending_messages)
    finally:
        supervisor_side.close()
        runner.close()


@pytest.mark.parametrize(
    ("registration_at", "expected_start"),
    [(29.999, True), (30.0, False), (31.0, False)],
)
def test_supervisor_registration_respects_persisted_startup_deadline(
    tmp_path: Path,
    registration_at: float,
    expected_start: bool,
) -> None:
    now = [0.0]
    state_path = tmp_path / "runner-state.json"
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: now[0],
        launch_spec=launch_spec(),
        state_path=state_path,
    )
    runner.accept_authority_deadline(40.0)
    runner.close()
    now[0] = registration_at
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: now[0],
        launch_spec=launch_spec(),
        state_path=state_path,
    )
    runner_side, supervisor_side = socket.socketpair()
    observed: list[bytes] = []
    errors: list[BaseException] = []

    def emulate_supervisor() -> None:
        try:
            supervisor_side.sendall(b"READY\n")
            supervisor_side.settimeout(0.3)
            with suppress(TimeoutError):
                command = supervisor_side.recv(64)
                observed.append(command)
                if command == b"START\n":
                    supervisor_side.sendall(b"STARTED 4321\n")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    peer = threading.Thread(target=emulate_supervisor)
    peer.start()
    try:
        runner.attach_supervisor_connection(runner_side, supervisor_pid=1234)
        peer.join(timeout=2)
        assert errors == []
        if expected_start:
            assert observed == [b"START\n"]
            assert runner.state is RunnerState.RUNNING
            assert [item["type"] for item in runner.pending_messages].count("STARTED") == 1
        else:
            assert observed == []
            assert runner.state is RunnerState.STOPPED
            assert all(item["type"] != "STARTED" for item in runner.pending_messages)
            assert runner.pending_messages[-1]["payload"]["reason"] == "RUNTIME_LIMIT"

        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["state"] == runner.state.value
        assert persisted["runtime_started_at"] == (registration_at if expected_start else None)
    finally:
        supervisor_side.close()
        runner.close()


def test_watchdog_keeps_startup_deadline_after_authority_is_accepted() -> None:
    now = [0.0]
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: now[0],
        launch_spec=launch_spec(),
    )
    runner.accept_authority_deadline(40.0)

    now[0] = 30.0
    runner.enforce_deadlines()

    assert runner.state is RunnerState.STOPPED
    assert runner.pending_messages[-1]["payload"]["reason"] == "RUNTIME_LIMIT"
    assert all(item["type"] != "STARTED" for item in runner.pending_messages)


def test_start_sent_before_deadline_does_not_block_watchdog_on_confirmation() -> None:
    now = [0.0]
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: now[0],
        launch_spec=launch_spec(),
    )
    runner.accept_authority_deadline(100.0)
    now[0] = 29.999
    runner_side, supervisor_side = socket.socketpair()
    start_received = threading.Event()
    release_started = threading.Event()
    observed: list[bytes] = []
    errors: list[BaseException] = []

    def emulate_supervisor() -> None:
        try:
            supervisor_side.sendall(b"READY\n")
            observed.append(supervisor_side.recv(64))
            start_received.set()
            assert release_started.wait(timeout=2)
            supervisor_side.sendall(b"STARTED 4321\n")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    peer = threading.Thread(target=emulate_supervisor)
    registration = threading.Thread(
        target=runner.attach_supervisor_connection,
        args=(runner_side,),
        kwargs={"supervisor_pid": 1234},
    )
    peer.start()
    registration.start()
    watchdog = threading.Thread(target=runner.enforce_deadlines, kwargs={"now": 30.0})
    try:
        assert start_received.wait(timeout=1)
        watchdog.start()
        watchdog.join(timeout=0.2)
        assert not watchdog.is_alive()
        assert runner.state is RunnerState.RUNNING
        assert runner.runtime_started_at == 29.999
    finally:
        release_started.set()
        registration.join(timeout=2)
        watchdog.join(timeout=2)
        peer.join(timeout=2)
        supervisor_side.close()
        runner.close()

    assert errors == []
    assert observed == [b"START\n"]
    assert [item["type"] for item in runner.pending_messages].count("STARTED") == 1


def test_start_is_sent_before_start_transition_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    state_path = tmp_path / "runner-state.json"
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: now[0],
        launch_spec=launch_spec(),
        state_path=state_path,
    )
    runner.accept_authority_deadline(100.0)
    now[0] = 29.999
    runner_side, supervisor_side = socket.socketpair()
    real_atomic_write = trusted_runner._atomic_write
    observed: list[bytes] = []

    def verify_start_precedes_persist(path: Path, payload: bytes, *, mode: int) -> None:
        decoded = json.loads(payload)
        if decoded["runtime_started_at"] is not None and not observed:
            supervisor_side.settimeout(0.2)
            observed.append(supervisor_side.recv(64))
            supervisor_side.sendall(b"STARTED 4321\n")
        real_atomic_write(path, payload, mode=mode)

    monkeypatch.setattr(trusted_runner, "_atomic_write", verify_start_precedes_persist)
    supervisor_side.sendall(b"READY\n")
    try:
        runner.attach_supervisor_connection(runner_side, supervisor_pid=1234)
        assert observed == [b"START\n"]
        assert runner.state is RunnerState.RUNNING
        assert runner.runtime_started_at == 29.999
    finally:
        supervisor_side.close()
        runner.close()


def test_failed_start_send_does_not_reuse_closed_supervisor_for_stop() -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: 0.0,
    )
    runner.accept_authority_deadline(100.0)
    runner_side, supervisor_side = socket.socketpair()
    supervisor_side.sendall(b"READY\n")
    runner.attach_supervisor_connection(runner_side, supervisor_pid=1234)
    supervisor_side.shutdown(socket.SHUT_RDWR)
    supervisor_side.close()

    with pytest.raises(OSError):
        runner.launch_workload(("unused",))

    runner.request_stop("FAILURE", now=0.0)
    runner.stop_workload(now=0.0, grace_seconds=0)
    assert runner.state is RunnerState.STOPPED
    assert runner.pending_messages[-1]["payload"]["reason"] == "FAILURE"
    runner.close()


def test_missing_started_ack_keeps_supervisor_connection_open_for_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: 0.0,
        launch_spec=launch_spec(),
    )
    runner.accept_authority_deadline(100.0)
    monkeypatch.setattr(runner._supervisor_started, "wait", lambda timeout: False)
    monkeypatch.setattr(
        trusted_runner,
        "validate_supervisor_peer",
        lambda connection: (1234, 1001, 1000),
    )
    socket_path = Path("/tmp") / f"nexa-{os.getpid()}-{time.time_ns()}.sock"
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            trusted_runner._serve_supervisor_registration(runner, str(socket_path))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    registration = threading.Thread(target=serve)
    registration.start()

    observed: list[bytes] = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as supervisor:
        deadline = time.monotonic() + 1
        while True:
            try:
                supervisor.connect(str(socket_path))
                break
            except (ConnectionRefusedError, FileNotFoundError):
                if time.monotonic() >= deadline:
                    raise AssertionError(errors) from None
                time.sleep(0.01)
        supervisor.settimeout(1)
        supervisor.sendall(b"READY\n")
        with supervisor.makefile("rb") as reader:
            observed.append(reader.readline())
            try:
                observed.append(reader.readline())
            except TimeoutError:
                observed.append(b"<timeout>")
        if observed[-1] == b"TERM 0.000000000\n":
            supervisor.sendall(b"EXIT 143\n")

    registration.join(timeout=2)
    assert not registration.is_alive()
    assert errors == [], observed
    assert observed == [b"START\n", b"TERM 0.000000000\n"]
    assert runner.state is RunnerState.STOPPED
    assert runner.pending_messages[-1]["payload"]["reason"] == "FAILURE"
    runner.close()


def test_supervisor_disconnect_after_start_sets_fatal_without_false_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: 0.0,
        launch_spec=launch_spec(),
    )
    runner.accept_authority_deadline(100.0)
    monkeypatch.setattr(runner._supervisor_started, "wait", lambda timeout: False)
    monkeypatch.setattr(
        trusted_runner,
        "validate_supervisor_peer",
        lambda connection: (1234, 1001, 1000),
    )
    socket_path = Path("/tmp") / f"nexa-{os.getpid()}-{time.time_ns()}.sock"
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            trusted_runner._serve_supervisor_registration(runner, str(socket_path))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    registration = threading.Thread(target=serve)
    registration.start()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as supervisor:
        deadline = time.monotonic() + 1
        while True:
            try:
                supervisor.connect(str(socket_path))
                break
            except (ConnectionRefusedError, FileNotFoundError):
                if time.monotonic() >= deadline:
                    raise AssertionError(errors) from None
                time.sleep(0.01)
        supervisor.settimeout(1)
        supervisor.sendall(b"READY\n")
        with supervisor.makefile("rb") as reader:
            assert reader.readline() == b"START\n"

    registration.join(timeout=2)
    assert not registration.is_alive()
    assert errors == []
    assert runner.fatal_error is True
    assert runner.state is RunnerState.STOPPING
    assert all(item["type"] != "STOPPED" for item in runner.pending_messages)
    runner.close()


def test_fatal_runner_error_aborts_control_connection() -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: 0.0,
    )
    runner.fail_closed()
    runner_side, controller_side = socket.socketpair()
    try:
        with pytest.raises(RuntimeError, match="fail-closed"):
            trusted_runner._serve_connection(runner, runner_side)
    finally:
        controller_side.close()
        runner.close()


def test_registered_supervisor_receives_stop_before_runner_reports_stopped() -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: 0.0,
    )
    runner_side, supervisor_side = socket.socketpair()
    observed: list[bytes] = []

    def emulate_supervisor() -> None:
        supervisor_side.sendall(b"READY\n")
        observed.append(supervisor_side.recv(64))
        supervisor_side.sendall(b"STARTED 4321\n")
        observed.append(supervisor_side.recv(64))
        supervisor_side.sendall(b"EXIT 143\n")

    peer = threading.Thread(target=emulate_supervisor)
    peer.start()
    try:
        runner.attach_supervisor_connection(runner_side, supervisor_pid=1234)
        runner.accept_authority_deadline(runner.clock() + 10_000.0)
        runner.launch_workload(("unused",))
        runner.request_stop("CANCEL", now=1.0)
        assert runner.stop_workload(now=1.0, grace_seconds=5) == 143
        peer.join(timeout=2)
        assert observed == [b"START\n", b"TERM 5.000000000\n"]
        assert runner.state is RunnerState.STOPPED
    finally:
        supervisor_side.close()
        runner.close()


def result_provenance() -> dict[str, object]:
    return {
        "tenant_id": TENANT,
        "job_id": JOB,
        "session_id": SESSION,
        "attempt_id": RESULT,
        "job_fence": 1,
        "input_checksum": "sha256:" + "e" * 64,
        "spec_checksum": SPEC_CHECKSUM,
        "template_id": "cpu-iterative",
        "template_version": 1,
        "adapter_id": "cpu.iterative",
        "adapter_version": "1.0.0",
        "image_digest": "sha256:" + "d" * 64,
    }


@pytest.mark.parametrize("deadline", [99.0, 100.0])
def test_initial_authority_deadline_must_be_strictly_in_the_future(deadline: float) -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: 100.0,
    )
    control = {
        "schema_version": 1,
        "control_sequence": 1,
        "type": "SET_AUTHORITY_DEADLINE",
        "payload": {
            "source_callback_id": CALLBACK,
            "deadline_monotonic_ns": int(deadline * 1_000_000_000),
        },
    }

    assert runner.apply_control(control) == "INVALID"
    assert runner.state is RunnerState.STOPPED
    assert runner.compute_allowed is False


def assert_result_manifest_schema(manifest: dict[str, object]) -> None:
    schema_path = (
        Path(__file__).parents[2]
        / "docs"
        / "contracts"
        / "schemas"
        / "workload-manifests.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))["$defs"]
    result_schema = schema["resultManifest"]
    assert set(manifest) == set(result_schema["required"])
    provenance = manifest["provenance"]
    assert isinstance(provenance, dict)
    assert set(provenance) == set(schema["provenance"]["required"])
    files = manifest["files"]
    assert isinstance(files, list) and files
    assert all(set(item) == set(schema["fileEntry"]["required"]) for item in files)


def test_runner_waits_for_authority_before_compute() -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: 0.0,
    )
    assert runner.state is RunnerState.WAITING_AUTHORITY
    assert runner.compute_allowed is False
    runner.accept_authority_deadline(10.0)
    assert runner.compute_allowed is True


def test_runner_stop_is_independent_of_result_handshake() -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: 0.0,
    )
    runner.accept_authority_deadline(10.0)
    runner.begin_result_handshake()
    runner.request_stop("LEASE_DEADLINE", now=11.0)
    assert runner.state is RunnerState.STOPPING


def test_runner_rejects_control_sequence_conflict() -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: 0.0,
    )
    assert (
        runner.apply_control(
            {
                "schema_version": 1,
                "control_sequence": 1,
                "type": "SET_AUTHORITY_DEADLINE",
                "payload": {"source_callback_id": CALLBACK, "deadline_monotonic_ns": 10},
            }
        )
        == "ACCEPTED"
    )
    assert (
        runner.apply_control(
            {
                "schema_version": 1,
                "control_sequence": 1,
                "type": "SET_AUTHORITY_DEADLINE",
                "payload": {"source_callback_id": CALLBACK, "deadline_monotonic_ns": 10},
            }
        )
        == "DUPLICATE"
    )


def test_runner_never_allows_stop_grace_beyond_contract_bound() -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: 0.0,
    )
    runner.accept_authority_deadline(10.0)
    runner.request_stop("RUNTIME_LIMIT", now=10.0)
    assert runner.stop_grace_seconds == 5


@pytest.mark.parametrize(
    ("now", "deadline", "expected_grace"),
    [(10.0, 9.0, 0.0), (10.0, 10.0, 0.0), (10.0, 12.0, 2.0)],
)
def test_request_stop_uses_remaining_absolute_grace(
    now: float, deadline: float, expected_grace: float
) -> None:
    class RecordingRunner(RunnerSupervisor):
        observed_grace: float | None = None

        def stop_workload(self, *, now: float, grace_seconds: float = 5) -> int | None:
            del now
            self.observed_grace = grace_seconds
            self.mark_stopped()
            return 0

    runner = RecordingRunner(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: now,
    )
    runner.accept_authority_deadline(100.0)
    assert (
        runner.apply_control(
            {
                "schema_version": 1,
                "control_sequence": 1,
                "type": "REQUEST_STOP",
                "payload": {
                    "reason": "CANCEL",
                    "grace_deadline_monotonic_ns": int(deadline * 1_000_000_000),
                },
            }
        )
        == "ACCEPTED"
    )
    assert runner.observed_grace == expected_grace
    assert runner.state is RunnerState.STOPPED
    assert (
        runner.apply_control(
            {
                "schema_version": 1,
                "control_sequence": 1,
                "type": "REQUEST_STOP",
                "payload": {
                    "reason": "CANCEL",
                    "grace_deadline_monotonic_ns": int(deadline * 1_000_000_000),
                },
            }
        )
        == "DUPLICATE"
    )


def test_expired_authority_cannot_be_renewed_between_watchdog_ticks() -> None:
    now = [9.0]
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: now[0],
    )
    runner.accept_authority_deadline(10.0)
    now[0] = 11.0

    with pytest.raises(ValueError, match="expired"):
        runner.accept_authority_deadline(20.0)

    assert runner.state is RunnerState.STOPPING
    assert runner.compute_allowed is False


def test_malformed_control_retry_is_never_recorded_as_duplicate() -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30, runtime_limit_seconds=300, stop_grace_seconds=5
    )
    malformed = {
        "schema_version": 1,
        "control_sequence": 1,
        "type": "SET_AUTHORITY_DEADLINE",
        "payload": {"source_callback_id": CALLBACK},
    }
    with pytest.raises(ProtocolError):
        runner.apply_control(malformed)
    with pytest.raises(ProtocolError):
        runner.apply_control(malformed)


def test_control_sequence_conflict_fails_closed() -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        clock=lambda: 0.0,
    )
    assert (
        runner.apply_control(
            {
                "schema_version": 1,
                "control_sequence": 1,
                "type": "SET_AUTHORITY_DEADLINE",
                "payload": {"source_callback_id": CALLBACK, "deadline_monotonic_ns": 10},
            }
        )
        == "ACCEPTED"
    )
    assert (
        runner.apply_control(
            {
                "schema_version": 1,
                "control_sequence": 1,
                "type": "SET_AUTHORITY_DEADLINE",
                "payload": {"source_callback_id": CALLBACK, "deadline_monotonic_ns": 11},
            }
        )
        == "INVALID"
    )
    assert runner.state is RunnerState.STOPPED


def test_runner_state_survives_reconnect_and_lost_ack(tmp_path: Path) -> None:
    state_path = tmp_path / "runner-state.json"
    first = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        state_path=state_path,
        clock=lambda: 0.0,
    )
    control = {
        "schema_version": 1,
        "control_sequence": 1,
        "type": "SET_AUTHORITY_DEADLINE",
        "payload": {"source_callback_id": CALLBACK, "deadline_monotonic_ns": 10},
    }
    assert first.apply_control(control) == "ACCEPTED"
    second = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        state_path=state_path,
        clock=lambda: 0.0,
    )
    assert second.apply_control(control) == "DUPLICATE"
    assert second.authority_deadline == first.authority_deadline


def test_runner_rejects_durable_pending_message_with_invalid_payload(tmp_path: Path) -> None:
    state_path = tmp_path / "runner-state.json"
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        state_path=state_path,
    )
    runner.emit_progress(fraction=0.25, step=1)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["pending_messages"][0]["payload"]["unexpected"] = None
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(RuntimeError, match="durable runner state is invalid"):
        RunnerSupervisor(
            startup_limit_seconds=30,
            runtime_limit_seconds=300,
            stop_grace_seconds=5,
            state_path=state_path,
        )


def test_watchdog_enforces_authority_and_runtime_without_recv_loop() -> None:
    authority_now = [0.0]
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=1,
        stop_grace_seconds=5,
        clock=lambda: authority_now[0],
    )
    runner.accept_authority_deadline(0.1)
    runner.mark_workload_started(now=0.0)
    authority_now[0] = 0.2
    runner.enforce_deadlines(now=0.2)
    assert runner.state is RunnerState.STOPPED
    assert runner.compute_allowed is False
    stopped = runner.pending_messages[-1]
    assert stopped["type"] == "STOPPED"
    assert stopped["payload"] == {
        "reason": "LEASE_DEADLINE",
        "exit_code": -1,
        "stopped_monotonic_ns": 200_000_000,
    }
    with pytest.raises(ValueError, match="stopped"):
        runner.accept_authority_deadline(100.0)

    runtime_now = [0.0]
    runtime = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=1,
        stop_grace_seconds=5,
        clock=lambda: runtime_now[0],
    )
    runtime.accept_authority_deadline(100.0)
    runtime.mark_workload_started(now=0.0)
    runtime_now[0] = 0.5
    runtime.accept_authority_deadline(200.0)
    runtime.enforce_deadlines(now=1.01)
    assert runtime.state is RunnerState.STOPPED
    assert runtime.pending_messages[-1]["payload"]["reason"] == "RUNTIME_LIMIT"


def test_progress_sequence_is_independent_and_monotonic() -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30, runtime_limit_seconds=300, stop_grace_seconds=5
    )
    first = runner.emit_progress(fraction=0.25, step=1)
    duplicate = runner.emit_progress(fraction=0.25, step=1)
    second = runner.emit_progress(fraction=0.5, step=2)
    assert first["payload"]["progress_sequence"] == 1
    assert duplicate == first
    assert second["payload"]["progress_sequence"] == 2
    assert second["message_sequence"] == first["message_sequence"] + 1


def test_result_handshake_validates_bindings_and_replays_once(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_bytes(b'{"value":1}')
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        state_path=tmp_path / "state.json",
    )
    runner.accept_authority_deadline(runner.clock() + 10_000.0)
    prepare = runner.stage_result(
        result_path,
        completion_token=COMPLETION,
        logical_name="result.json",
        media_type="application/json",
        provenance=result_provenance(),
    )
    assert prepare["type"] == "RESULT_PREPARE"
    assert (
        runner.apply_control(
            {
                "schema_version": 1,
                "control_sequence": 1,
                "type": "PREPARE_RESULT",
                "payload": {
                    "completion_token": COMPLETION,
                    "reservation_callback_id": CALLBACK,
                    "result_id": RESULT,
                },
            }
        )
        == "ACCEPTED"
    )
    batch = runner.pending_messages[-1]
    assert batch["type"] == "RESULT_FILE_BATCH"
    descriptor = batch["payload"]["artifacts"][0]
    assert (
        descriptor["checksum"] == "sha256:" + hashlib.sha256(result_path.read_bytes()).hexdigest()
    )
    binding = {**descriptor, "artifact_id": ARTIFACT}
    bind = {
        "schema_version": 1,
        "control_sequence": 2,
        "type": "BIND_ARTIFACT_BATCH",
        "payload": {
            "purpose": "RESULT",
            "reservation_callback_id": CALLBACK,
            "reserved_id": RESULT,
            "source_message_sequence": batch["message_sequence"],
            "bindings": [binding],
        },
    }
    assert runner.apply_control(bind) == "ACCEPTED"
    checksum = runner.result_binding_set_checksum
    assert checksum is not None
    finalize = {
        "schema_version": 1,
        "control_sequence": 3,
        "type": "FINALIZE_RESULT_MANIFEST",
        "payload": {
            "completion_token": COMPLETION,
            "reservation_callback_id": CALLBACK,
            "result_id": RESULT,
            "binding_set_checksum": checksum,
        },
    }
    assert runner.apply_control(finalize) == "ACCEPTED"
    ready = runner.pending_messages[-1]
    assert ready["type"] == "RESULT_READY"
    manifest_path = tmp_path / "result-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    assert_result_manifest_schema(manifest)
    without_checksum = {key: value for key, value in manifest.items() if key != "manifest_checksum"}
    expected_manifest_checksum = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(without_checksum, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert manifest["manifest_checksum"] == expected_manifest_checksum
    assert ready["payload"]["manifest"]["checksum"] == (
        "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    )
    count = len(runner.pending_messages)
    assert runner.apply_control(finalize) == "DUPLICATE"
    assert len(runner.pending_messages) == count


def test_invalid_result_binding_is_terminal_and_blocks_finalize(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_bytes(b'{"value":1}')
    runner = RunnerSupervisor(
        startup_limit_seconds=30, runtime_limit_seconds=300, stop_grace_seconds=5
    )
    runner.accept_authority_deadline(runner.clock() + 10_000.0)
    runner.stage_result(
        result_path,
        completion_token=COMPLETION,
        logical_name="result.json",
        media_type="application/json",
        provenance=result_provenance(),
    )
    assert (
        runner.apply_control(
            {
                "schema_version": 1,
                "control_sequence": 1,
                "type": "PREPARE_RESULT",
                "payload": {
                    "completion_token": COMPLETION,
                    "reservation_callback_id": CALLBACK,
                    "result_id": RESULT,
                },
            }
        )
        == "ACCEPTED"
    )
    batch = runner.pending_messages[-1]
    descriptor = batch["payload"]["artifacts"][0]
    binding = {**descriptor, "artifact_id": ARTIFACT}
    invalid = {
        "schema_version": 1,
        "control_sequence": 2,
        "type": "BIND_ARTIFACT_BATCH",
        "payload": {
            "purpose": "RESULT",
            "reservation_callback_id": CALLBACK,
            "reserved_id": RESULT,
            "source_message_sequence": batch["message_sequence"],
            "bindings": [{**binding, "size_bytes": 999}],
        },
    }
    corrected = {
        **invalid,
        "payload": {**invalid["payload"], "bindings": [binding]},
    }

    assert runner.apply_control(invalid) == "INVALID"
    assert runner.state is RunnerState.STOPPED
    assert runner.apply_control(corrected) == "INVALID"
    assert all(message["type"] != "RESULT_READY" for message in runner.pending_messages)


def test_concurrent_emit_allocates_unique_message_sequences() -> None:
    runner = RunnerSupervisor(
        startup_limit_seconds=30, runtime_limit_seconds=300, stop_grace_seconds=5
    )
    entered = 0
    entered_lock = threading.Lock()
    both_entered = threading.Event()

    class RacingSequence(int):
        def __add__(self, other: object) -> int:
            nonlocal entered
            with entered_lock:
                entered += 1
                if entered == 2:
                    both_entered.set()
            both_entered.wait(timeout=0.2)
            return int(self) + int(other)  # type: ignore[arg-type]

    runner._next_message_sequence = RacingSequence(1)
    errors: list[BaseException] = []

    def emit(step: int) -> None:
        try:
            runner._emit(
                "PROGRESS",
                {
                    "progress_sequence": step,
                    "fraction": step / 10,
                    "step": step,
                    "epoch": None,
                    "item_cursor": None,
                },
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=emit, args=(step,)) for step in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert errors == []
    assert sorted(message["message_sequence"] for message in runner.pending_messages) == [1, 2]


def test_persist_serializes_snapshot_and_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "runner-state.json"
    result_path = tmp_path / "result.json"
    result_path.write_bytes(b'{"value":1}')
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        state_path=state_path,
    )
    runner.accept_authority_deadline(runner.clock() + 10_000.0)
    real_atomic_write = trusted_runner._atomic_write
    matching_writes = 0
    stale_snapshot_blocked = threading.Event()
    release_stale_snapshot = threading.Event()

    def blocking_atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
        nonlocal matching_writes
        decoded = json.loads(payload)
        pending_types = [item["type"] for item in decoded["pending_messages"]]
        if threading.current_thread().name == "stager" and pending_types == ["RESULT_PREPARE"]:
            matching_writes += 1
            if matching_writes == 2:
                stale_snapshot_blocked.set()
                assert release_stale_snapshot.wait(timeout=2)
        real_atomic_write(path, payload, mode=mode)

    monkeypatch.setattr(trusted_runner, "_atomic_write", blocking_atomic_write)
    errors: list[BaseException] = []

    def stage() -> None:
        try:
            runner.stage_result(
                result_path,
                completion_token=COMPLETION,
                logical_name="result.json",
                media_type="application/json",
                provenance=result_provenance(),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    controller_entered = threading.Event()
    controller_done = threading.Event()

    def control_and_ack() -> None:
        try:
            controller_entered.set()
            assert (
                runner.apply_control(
                    {
                        "schema_version": 1,
                        "control_sequence": 1,
                        "type": "PREPARE_RESULT",
                        "payload": {
                            "completion_token": COMPLETION,
                            "reservation_callback_id": CALLBACK,
                            "result_id": RESULT,
                        },
                    }
                )
                == "ACCEPTED"
            )
            runner.acknowledge_message(
                {
                    "schema_version": 1,
                    "ack_sequence": 1,
                    "accepted": True,
                    "code": "ACCEPTED",
                }
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            controller_done.set()

    stager = threading.Thread(target=stage, name="stager")
    controller = threading.Thread(target=control_and_ack, name="controller")
    stager.start()
    assert stale_snapshot_blocked.wait(timeout=2)
    controller.start()
    assert controller_entered.wait(timeout=1)
    serialized = not controller_done.wait(timeout=0.1)
    release_stale_snapshot.set()
    stager.join(timeout=2)
    controller.join(timeout=2)

    assert serialized is True
    assert errors == []
    reloaded = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        state_path=state_path,
    )
    assert (
        reloaded.apply_control(
            {
                "schema_version": 1,
                "control_sequence": 1,
                "type": "PREPARE_RESULT",
                "payload": {
                    "completion_token": COMPLETION,
                    "reservation_callback_id": CALLBACK,
                    "result_id": RESULT,
                },
            }
        )
        == "DUPLICATE"
    )
    assert [item["type"] for item in reloaded.pending_messages] == ["RESULT_FILE_BATCH"]


def test_workload_does_not_inherit_runner_control_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("NEXA_CONTROL_SOCKET", "NEXA_RUNNER_STATE", "NEXA_LAUNCH_SPEC"):
        monkeypatch.setenv(name, f"secret-{name}")
    output = tmp_path / "environment.json"
    runner = RunnerSupervisor(
        startup_limit_seconds=30, runtime_limit_seconds=300, stop_grace_seconds=5
    )
    runner.accept_authority_deadline(runner.clock() + 10_000.0)
    runner.launch_workload(
        (
            sys.executable,
            "-c",
            (
                "import json,os,sys;"
                "json.dump({key:os.environ.get(key) for key in sys.argv[2:]},"
                "open(sys.argv[1],'w'))"
            ),
            str(output),
            "NEXA_CONTROL_SOCKET",
            "NEXA_RUNNER_STATE",
            "NEXA_LAUNCH_SPEC",
        ),
        workload_uid=os.geteuid(),
        workload_gid=os.getegid(),
    )
    assert runner.workload is not None
    assert runner.workload.wait(timeout=5) == 0
    assert json.loads(output.read_text()) == {
        "NEXA_CONTROL_SOCKET": None,
        "NEXA_RUNNER_STATE": None,
        "NEXA_LAUNCH_SPEC": None,
    }
