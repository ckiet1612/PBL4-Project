"""B14 trusted-runner checkpoint handshake without Docker."""

import hashlib
import json
import os
import socket
import stat
import threading
from contextlib import suppress
from pathlib import Path

import pytest
import rfc8785

from nexa.application.checkpoint_validation import validate_checkpoint_manifest, validate_cpu_state
from nexa.worker.protocol import ProtocolError, validate_control_envelope
from nexa.workloads.cpu_state import CpuState, encode_state
from nexa.workloads.trusted_runner import RunnerState, RunnerSupervisor, _validate_launch_spec
from tests.workloads.test_runner import (
    ARTIFACT,
    CALLBACK,
    COMPLETION,
    RESULT,
    SPEC_CHECKSUM,
    launch_spec,
    result_provenance,
)

CHECKPOINT = "018f0d60-7b6a-7a31-9d82-1aa39c4f30b7"
CHECKPOINT_2 = "018f0d60-7b6a-7a32-9d82-1aa39c4f30b7"
CALLBACK_2 = "018f0d60-7b6a-7a33-9d82-1aa39c4f30b7"
RESTORED = "018f0d60-7b6a-7a34-9d82-1aa39c4f30b7"
ITERATIONS = 50
MODULUS = 1_000_003
COMPATIBILITY = {
    "architecture": "linux/arm64",
    "device_type": "CPU",
    "framework": "PYTHON",
    "framework_version": "1.0.0",
    "cuda_version": None,
    "minimum_driver_version": None,
    "gpu_compute_capability": None,
    "checkpointable": True,
    "restart_safe": False,
}


def _state_bytes(step: int, accumulator: int) -> bytes:
    provenance = result_provenance()
    return encode_state(
        CpuState(
            step=step,
            accumulator=accumulator,
            input_checksum=str(provenance["input_checksum"]),
            spec_checksum=SPEC_CHECKSUM,
        )
    )


def _restore(step: int = 10, accumulator: int = 77, raw: bytes | None = None) -> dict:
    body = raw if raw is not None else _state_bytes(step, accumulator)
    return {
        "path": "/input/restore-state.json",
        "checkpoint_id": RESTORED,
        "checkpoint_sequence": 1,
        "step": step,
        "accumulator": accumulator,
        "state_checksum": "sha256:" + hashlib.sha256(body).hexdigest(),
    }


def checkpoint_spec(restore: dict | None = None) -> dict[str, object]:
    return {
        **launch_spec(),
        "schema_version": 2,
        "iterations": ITERATIONS,
        "modulus": MODULUS,
        "checkpoint": {"state_path": "/output/state.json", "compatibility": COMPATIBILITY},
        "restore": restore,
    }


def _runner(tmp_path: Path, spec: dict | None = None, **kwargs) -> RunnerSupervisor:
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        state_path=tmp_path / "run" / "runner-state.json",
        launch_spec=checkpoint_spec() if spec is None else spec,
        fs_root=tmp_path,
        **kwargs,
    )
    return runner


def _started(runner: RunnerSupervisor) -> RunnerSupervisor:
    runner.accept_authority_deadline(runner.clock() + 10_000.0)
    runner.mark_workload_started()
    return runner


def _write_state(tmp_path: Path, step: int = 20, accumulator: int = 555) -> bytes:
    raw = _state_bytes(step, accumulator)
    (tmp_path / "output" / "state.json").write_bytes(raw)
    return raw


def _request(runner, sequence, *, checkpoint_sequence=1, checkpoint=CHECKPOINT, **payload):
    body = {
        "reason": "INTERVAL",
        "reservation_callback_id": CALLBACK,
        "checkpoint_id": checkpoint,
        "checkpoint_sequence": checkpoint_sequence,
        "checkpoint_deadline_monotonic_ns": int((runner.clock() + 30) * 1_000_000_000),
        **payload,
    }
    return {
        "schema_version": 1,
        "control_sequence": sequence,
        "type": "REQUEST_CHECKPOINT",
        "payload": body,
    }


def _bind(sequence, files, *, bindings=None, checkpoint=CHECKPOINT, callback=CALLBACK):
    descriptor = files["payload"]["artifacts"][0]
    return {
        "schema_version": 1,
        "control_sequence": sequence,
        "type": "BIND_ARTIFACT_BATCH",
        "payload": {
            "purpose": "CHECKPOINT",
            "reservation_callback_id": callback,
            "reserved_id": checkpoint,
            "source_message_sequence": files["message_sequence"],
            "bindings": bindings or [{**descriptor, "artifact_id": ARTIFACT}],
        },
    }


def _binding_set_checksum(bindings) -> str:
    return "sha256:" + hashlib.sha256(rfc8785.dumps(bindings)).hexdigest()


def _finalize(
    sequence, checksum, *, checkpoint=CHECKPOINT, checkpoint_sequence=1, callback=CALLBACK
):
    return {
        "schema_version": 1,
        "control_sequence": sequence,
        "type": "FINALIZE_CHECKPOINT_MANIFEST",
        "payload": {
            "reservation_callback_id": callback,
            "checkpoint_id": checkpoint,
            "checkpoint_sequence": checkpoint_sequence,
            "binding_set_checksum": checksum,
        },
    }


def _last(runner, message_type):
    return [m for m in runner.pending_messages if m["type"] == message_type][-1]


def test_checkpoint_controls_are_closed_contract_payloads() -> None:
    runner_clock = RunnerSupervisor(
        startup_limit_seconds=30, runtime_limit_seconds=300, stop_grace_seconds=5
    )
    request = _request(runner_clock, 1)
    assert validate_control_envelope(request) == request
    finalize = _finalize(2, "sha256:" + "a" * 64)
    assert validate_control_envelope(finalize) == finalize
    invalid = [
        {**request, "payload": {**request["payload"], "extra": 1}},
        {**request, "payload": {**request["payload"], "reason": "CHECKPOINT_FOR_PAUSE"}},
        {**request, "payload": {**request["payload"], "checkpoint_sequence": 0}},
        {**request, "payload": {**request["payload"], "checkpoint_id": "not-a-uuid"}},
        {**finalize, "payload": {**finalize["payload"], "binding_set_checksum": "sha256:x"}},
        {
            **finalize,
            "payload": {k: v for k, v in finalize["payload"].items() if k != "checkpoint_id"},
        },
    ]
    for envelope in invalid:
        with pytest.raises(ProtocolError):
            validate_control_envelope(envelope)


def test_checkpoint_cycle_produces_a_manifest_the_server_accepts(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path))
    state = _write_state(tmp_path)

    assert runner.apply_control(_request(runner, 1)) == "ACCEPTED"
    files = _last(runner, "CHECKPOINT_FILES_READY")
    assert files["payload"]["reservation_callback_id"] == CALLBACK
    assert files["payload"]["checkpoint_id"] == CHECKPOINT
    assert files["payload"]["checkpoint_sequence"] == 1
    descriptor = files["payload"]["artifacts"][0]
    staged = tmp_path / "output" / descriptor["staging_name"]
    assert staged.read_bytes() == state
    assert descriptor == {
        "staging_name": staged.name,
        "logical_name": "state.json",
        "kind": "CHECKPOINT_FILE",
        "media_type": "application/json",
        "size_bytes": len(state),
        "checksum": "sha256:" + hashlib.sha256(state).hexdigest(),
    }
    # The workload keeps mutating its live snapshot; staged bytes are closed.
    assert stat.S_IMODE(os.stat(staged).st_mode) == 0o440
    _write_state(tmp_path, step=30, accumulator=9)
    assert staged.read_bytes() == state
    assert runner.state is RunnerState.RUNNING

    bind = _bind(2, files)
    assert runner.apply_control(bind) == "ACCEPTED"
    checksum = _binding_set_checksum(bind["payload"]["bindings"])
    assert runner.apply_control(_finalize(3, checksum)) == "ACCEPTED"
    ready = _last(runner, "CHECKPOINT_READY")
    manifest_descriptor = ready["payload"]["manifest"]
    assert ready["payload"]["checkpoint_id"] == CHECKPOINT
    assert manifest_descriptor["kind"] == "CHECKPOINT_MANIFEST"
    manifest_path = tmp_path / "output" / manifest_descriptor["staging_name"]
    assert stat.S_IMODE(os.stat(manifest_path).st_mode) == 0o440
    raw = manifest_path.read_bytes()
    assert manifest_descriptor["size_bytes"] == len(raw)
    assert manifest_descriptor["checksum"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    manifest = json.loads(raw)
    assert raw == rfc8785.dumps(manifest)
    entries = validate_checkpoint_manifest(
        manifest,
        raw=raw,
        artifact={
            "kind": "CHECKPOINT_MANIFEST",
            "media_type": "application/json",
            "size_bytes": len(raw),
            "checksum": manifest_descriptor["checksum"],
        },
        checkpoint_id=CHECKPOINT,
        sequence=1,
        provenance=result_provenance(),
        compatibility=COMPATIBILITY,
        parameters={"iterations": ITERATIONS, "seed": 7, "modulus": MODULUS},
    )
    assert entries[0]["artifact_id"] == ARTIFACT
    assert manifest["cursor"] == {"step": 20, "epoch": 0, "item_cursor": 20, "accumulator": 555}
    assert validate_cpu_state(state, manifest=manifest)["step"] == 20
    count = len(runner.pending_messages)
    assert runner.apply_control(_finalize(3, checksum)) == "DUPLICATE"
    assert runner.apply_control(bind) == "DUPLICATE"
    assert len(runner.pending_messages) == count
    assert runner.state is RunnerState.RUNNING


def test_checkpoint_handshake_survives_runner_state_reload(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path))
    _write_state(tmp_path)
    request = _request(runner, 1)
    assert runner.apply_control(request) == "ACCEPTED"
    files = _last(runner, "CHECKPOINT_FILES_READY")
    bind = _bind(2, files)
    assert runner.apply_control(bind) == "ACCEPTED"

    assert os.stat(tmp_path / "run" / "runner-state.json").st_mode & 0o077 == 0
    reloaded = _runner(tmp_path)
    assert reloaded.apply_control(bind) == "DUPLICATE"
    # The worker replays journaled controls byte-for-byte after a reconnect.
    assert reloaded.apply_control(request) == "DUPLICATE"
    checksum = _binding_set_checksum(bind["payload"]["bindings"])
    assert reloaded.apply_control(_finalize(3, checksum)) == "ACCEPTED"
    assert _last(reloaded, "CHECKPOINT_READY")["payload"]["checkpoint_id"] == CHECKPOINT


def test_sequential_checkpoints_advance_and_release_previous_staging(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path))
    _write_state(tmp_path)
    assert runner.apply_control(_request(runner, 1)) == "ACCEPTED"
    files = _last(runner, "CHECKPOINT_FILES_READY")
    bind = _bind(2, files)
    assert runner.apply_control(bind) == "ACCEPTED"
    checksum = _binding_set_checksum(bind["payload"]["bindings"])
    assert runner.apply_control(_finalize(3, checksum)) == "ACCEPTED"
    first_names = {
        files["payload"]["artifacts"][0]["staging_name"],
        _last(runner, "CHECKPOINT_READY")["payload"]["manifest"]["staging_name"],
    }

    _write_state(tmp_path, step=40, accumulator=1)
    second = _request(
        runner,
        4,
        checkpoint=CHECKPOINT_2,
        checkpoint_sequence=2,
        reservation_callback_id=CALLBACK_2,
    )
    assert runner.apply_control(second) == "ACCEPTED"
    names = {path.name for path in (tmp_path / "output").iterdir()}
    assert not names & first_names
    assert _last(runner, "CHECKPOINT_FILES_READY")["payload"]["checkpoint_id"] == CHECKPOINT_2


def _stage(runner: RunnerSupervisor, tmp_path: Path):
    result = tmp_path / "output" / "result.json"
    result.write_bytes(b'{"value":1}')
    return runner.stage_result(
        result,
        completion_token=COMPLETION,
        logical_name="result.json",
        media_type="application/json",
        provenance=result_provenance(),
    )


def test_result_prepare_is_deferred_until_the_open_checkpoint_is_ready(tmp_path: Path) -> None:
    # Messages are strictly ordered and the server refuses a result reservation
    # while the attempt is CHECKPOINTING, so RESULT_PREPARE must follow READY.
    runner = _started(_runner(tmp_path))
    _write_state(tmp_path, step=ITERATIONS, accumulator=3)
    assert runner.apply_control(_request(runner, 1)) == "ACCEPTED"
    files = _last(runner, "CHECKPOINT_FILES_READY")
    assert _stage(runner, tmp_path) is None
    assert runner.state is RunnerState.RESULT_HANDSHAKE
    assert all(m["type"] != "RESULT_PREPARE" for m in runner.pending_messages)
    # A monitor replay of the same completion stays deferred and emits nothing.
    count = len(runner.pending_messages)
    assert _stage(runner, tmp_path) is None
    assert len(runner.pending_messages) == count

    bind = _bind(2, files)
    assert runner.apply_control(bind) == "ACCEPTED"
    reloaded = _runner(tmp_path)
    checksum = _binding_set_checksum(bind["payload"]["bindings"])
    assert reloaded.apply_control(_finalize(3, checksum)) == "ACCEPTED"
    ready = _last(reloaded, "CHECKPOINT_READY")
    prepare = _last(reloaded, "RESULT_PREPARE")
    assert prepare["message_sequence"] == ready["message_sequence"] + 1
    assert prepare["payload"] == {"completion_token": COMPLETION}
    assert reloaded.apply_control(_finalize(3, checksum)) == "DUPLICATE"
    assert [m["type"] for m in reloaded.pending_messages].count("RESULT_PREPARE") == 1
    assert _stage(reloaded, tmp_path) == prepare


def test_checkpoint_request_after_unreserved_result_checkpoints_the_final_state(
    tmp_path: Path,
) -> None:
    # The worker reserved the checkpoint before it saw RESULT_PREPARE; it defers
    # that frame until publish, so the runner must still complete this cycle.
    runner = _started(_runner(tmp_path))
    _write_state(tmp_path, step=ITERATIONS, accumulator=3)
    prepare = _stage(runner, tmp_path)
    assert prepare["type"] == "RESULT_PREPARE"
    assert runner.apply_control(_request(runner, 1)) == "ACCEPTED"
    assert runner.state is RunnerState.RESULT_HANDSHAKE
    files = _last(runner, "CHECKPOINT_FILES_READY")
    assert files["message_sequence"] == prepare["message_sequence"] + 1
    bind = _bind(2, files)
    assert runner.apply_control(bind) == "ACCEPTED"
    checksum = _binding_set_checksum(bind["payload"]["bindings"])
    assert runner.apply_control(_finalize(3, checksum)) == "ACCEPTED"
    ready = _last(runner, "CHECKPOINT_READY")
    assert ready["message_sequence"] == files["message_sequence"] + 1
    raw = (tmp_path / "output" / ready["payload"]["manifest"]["staging_name"]).read_bytes()
    assert json.loads(raw)["cursor"]["step"] == ITERATIONS
    assert [m["type"] for m in runner.pending_messages].count("RESULT_PREPARE") == 1


def test_checkpoint_request_after_result_reservation_fails_closed(tmp_path: Path) -> None:
    # A reserved result proves the server holds no checkpoint reservation.
    runner = _started(_runner(tmp_path))
    _write_state(tmp_path, step=ITERATIONS, accumulator=3)
    assert _stage(runner, tmp_path)["type"] == "RESULT_PREPARE"
    prepare = {
        "schema_version": 1,
        "control_sequence": 1,
        "type": "PREPARE_RESULT",
        "payload": {
            "completion_token": COMPLETION,
            "reservation_callback_id": CALLBACK_2,
            "result_id": RESULT,
        },
    }
    assert runner.apply_control(prepare) == "ACCEPTED"
    assert runner.apply_control(_request(runner, 2)) == "INVALID"
    assert runner.state is RunnerState.STOPPED
    assert all(m["type"] != "CHECKPOINT_FILES_READY" for m in runner.pending_messages)


@pytest.mark.parametrize(
    "mutate",
    [
        "pause",
        "past_deadline",
        "missing_state",
        "symlink_state",
        "tampered_state",
        "foreign_state",
        "step_beyond_iterations",
        "not_started",
        "legacy_spec",
    ],
)
def test_checkpoint_request_fails_closed(tmp_path: Path, mutate: str) -> None:
    spec = launch_spec() if mutate == "legacy_spec" else None
    runner = _runner(tmp_path, spec)
    if mutate == "not_started":
        runner.accept_authority_deadline(runner.clock() + 10_000.0)
    else:
        _started(runner)
    state_path = tmp_path / "output" / "state.json"
    _write_state(tmp_path)
    request = _request(runner, 1)
    if mutate == "pause":
        request["payload"]["reason"] = "PAUSE"
    elif mutate == "past_deadline":
        request["payload"]["checkpoint_deadline_monotonic_ns"] = int(
            (runner.clock() - 1) * 1_000_000_000
        )
    elif mutate == "missing_state":
        state_path.unlink()
    elif mutate == "symlink_state":
        target = tmp_path / "elsewhere.json"
        target.write_bytes(state_path.read_bytes())
        state_path.unlink()
        state_path.symlink_to(target)
    elif mutate == "tampered_state":
        state_path.write_bytes(state_path.read_bytes().replace(b'"step":20', b'"step": 20'))
    elif mutate == "foreign_state":
        state_path.write_bytes(
            encode_state(
                CpuState(
                    step=1,
                    accumulator=1,
                    input_checksum="sha256:" + "1" * 64,
                    spec_checksum=SPEC_CHECKSUM,
                )
            )
        )
    elif mutate == "step_beyond_iterations":
        state_path.write_bytes(_state_bytes(ITERATIONS + 1, 1))

    assert runner.apply_control(request) == "INVALID"
    assert runner.state is RunnerState.STOPPED
    assert all(m["type"] != "CHECKPOINT_FILES_READY" for m in runner.pending_messages)


def test_restore_floor_rejects_state_regression(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path, checkpoint_spec(_restore(step=30, accumulator=1))))
    _write_state(tmp_path, step=29, accumulator=1)
    assert runner.apply_control(_request(runner, 1)) == "INVALID"
    assert runner.state is RunnerState.STOPPED


@pytest.mark.parametrize("defect", ["size", "artifact_purpose", "reserved_id", "source"])
def test_checkpoint_binding_mismatch_is_terminal(tmp_path: Path, defect: str) -> None:
    runner = _started(_runner(tmp_path))
    _write_state(tmp_path)
    assert runner.apply_control(_request(runner, 1)) == "ACCEPTED"
    files = _last(runner, "CHECKPOINT_FILES_READY")
    bind = _bind(2, files)
    payload = bind["payload"]
    if defect == "size":
        payload["bindings"] = [{**payload["bindings"][0], "size_bytes": 1}]
    elif defect == "artifact_purpose":
        payload["purpose"] = "RESULT"
    elif defect == "reserved_id":
        payload["reserved_id"] = CHECKPOINT_2
    else:
        payload["source_message_sequence"] = files["message_sequence"] + 7
    assert runner.apply_control(bind) == "INVALID"
    assert runner.state is RunnerState.STOPPED
    # The rejected bind never committed sequence 2; a stopped runner refuses it.
    assert runner.apply_control(_finalize(2, "sha256:" + "a" * 64)) == "INVALID"
    assert all(m["type"] != "CHECKPOINT_READY" for m in runner.pending_messages)


def test_finalize_requires_exact_identity_and_binding_checksum(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path))
    _write_state(tmp_path)
    assert runner.apply_control(_request(runner, 1)) == "ACCEPTED"
    files = _last(runner, "CHECKPOINT_FILES_READY")
    assert runner.apply_control(_bind(2, files)) == "ACCEPTED"
    assert runner.apply_control(_finalize(3, "sha256:" + "a" * 64)) == "INVALID"
    assert runner.state is RunnerState.STOPPED


def test_second_request_before_the_first_completes_is_rejected(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path))
    _write_state(tmp_path)
    assert runner.apply_control(_request(runner, 1)) == "ACCEPTED"
    second = _request(
        runner,
        2,
        checkpoint=CHECKPOINT_2,
        checkpoint_sequence=2,
        reservation_callback_id=CALLBACK_2,
    )
    assert runner.apply_control(second) == "INVALID"


def test_launch_spec_v1_is_unchanged_and_v2_is_closed() -> None:
    legacy = launch_spec()
    assert _validate_launch_spec(legacy) == legacy
    assert _validate_launch_spec(checkpoint_spec()) == checkpoint_spec()
    assert _validate_launch_spec(checkpoint_spec(_restore())) == checkpoint_spec(_restore())
    broken = [
        {k: v for k, v in checkpoint_spec().items() if k != "restore"},
        {
            **checkpoint_spec(),
            "checkpoint": {"state_path": "/tmp/state.json", "compatibility": COMPATIBILITY},
        },
        {
            **checkpoint_spec(),
            "checkpoint": {
                "state_path": "/output/state.json",
                "compatibility": {**COMPATIBILITY, "extra": 1},
            },
        },
        {
            **checkpoint_spec(),
            "checkpoint": {
                "state_path": "/output/state.json",
                "compatibility": {**COMPATIBILITY, "checkpointable": False},
            },
        },
        checkpoint_spec({**_restore(), "path": "/output/state.json"}),
        checkpoint_spec({**_restore(), "step": ITERATIONS + 1}),
        checkpoint_spec({**_restore(), "accumulator": MODULUS}),
        checkpoint_spec({**_restore(), "extra": 1}),
        {**legacy, "checkpoint": checkpoint_spec()["checkpoint"]},
        {**checkpoint_spec(), "schema_version": 3},
    ]
    for spec in broken:
        with pytest.raises(ProtocolError):
            _validate_launch_spec(spec)


def test_launch_command_adds_state_and_restore_only_for_checkpoint_specs(tmp_path: Path) -> None:
    legacy = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        launch_spec={**launch_spec(), "iterations": ITERATIONS, "modulus": MODULUS},
    )
    base = legacy._launch_command()
    assert "--state-output" not in base and "--resume-state" not in base
    fresh = _runner(tmp_path)._launch_command()
    assert fresh[: len(base)] == base
    assert fresh[-2:] == ("--state-output", "/output/state.json")
    restored = _runner(tmp_path / "r", checkpoint_spec(_restore()))._launch_command()
    assert restored[-4:] == (
        "--state-output",
        "/output/state.json",
        "--resume-state",
        "/input/restore-state.json",
    )


def _launch_with_supervisor(runner: RunnerSupervisor) -> list[bytes]:
    runner.accept_authority_deadline(runner.clock() + 10_000.0)
    runner_side, supervisor_side = socket.socketpair()
    observed: list[bytes] = []

    def emulate_supervisor() -> None:
        supervisor_side.sendall(b"READY\n")
        supervisor_side.settimeout(0.5)
        with suppress(TimeoutError):
            data = supervisor_side.recv(64)
            observed.append(data)
            if data == b"START\n":
                supervisor_side.sendall(b"STARTED 4321\n")

    peer = threading.Thread(target=emulate_supervisor)
    peer.start()
    try:
        runner.attach_supervisor_connection(runner_side, supervisor_pid=1234)
        peer.join(timeout=2)
    finally:
        supervisor_side.close()
        runner.close()
    return observed


def test_restore_state_is_verified_before_the_workload_starts(tmp_path: Path) -> None:
    raw = _state_bytes(10, 77)
    (tmp_path / "input").mkdir()
    (tmp_path / "input" / "restore-state.json").write_bytes(raw)
    runner = _runner(tmp_path, checkpoint_spec(_restore(10, 77, raw)))
    assert _launch_with_supervisor(runner) == [b"START\n"]
    assert any(m["type"] == "STARTED" for m in runner.pending_messages)


@pytest.mark.parametrize("restored_step", [None, 0, 10, ITERATIONS])
def test_first_progress_reports_the_restored_cursor(
    tmp_path: Path, restored_step: int | None
) -> None:
    restore = None
    if restored_step is not None:
        raw = _state_bytes(restored_step, 77)
        (tmp_path / "input").mkdir()
        (tmp_path / "input" / "restore-state.json").write_bytes(raw)
        restore = _restore(restored_step, 77, raw)
    runner = _runner(tmp_path, checkpoint_spec(restore))
    assert _launch_with_supervisor(runner) == [b"START\n"]
    types = [m["type"] for m in runner.pending_messages]
    assert types.index("PROGRESS") == types.index("STARTED") + 1
    progress = _last(runner, "PROGRESS")["payload"]
    step = restored_step or 0
    assert (progress["progress_sequence"], progress["step"]) == (1, step)
    assert progress["fraction"] == step / ITERATIONS


@pytest.mark.parametrize("defect", ["checksum", "missing", "cursor"])
def test_invalid_restore_state_fails_incompatible_without_starting(
    tmp_path: Path, defect: str
) -> None:
    raw = _state_bytes(10, 77)
    (tmp_path / "input").mkdir()
    target = tmp_path / "input" / "restore-state.json"
    restore = _restore(10, 77, raw)
    if defect == "checksum":
        target.write_bytes(_state_bytes(10, 78))
    elif defect == "cursor":
        other = _state_bytes(11, 77)
        target.write_bytes(other)
        restore["state_checksum"] = "sha256:" + hashlib.sha256(other).hexdigest()
    runner = _runner(tmp_path, checkpoint_spec(restore))
    assert _launch_with_supervisor(runner) == []
    failed = _last(runner, "FAILED")["payload"]
    assert failed["failure_class"] == "INCOMPATIBLE"
    # The only restore reason the worker may forward (server allowlist).
    assert failed["reason_code"] == "CHECKPOINT_RESTORE_UNAVAILABLE"
    assert all(m["type"] != "STARTED" for m in runner.pending_messages)
    assert runner.state is RunnerState.STOPPED
