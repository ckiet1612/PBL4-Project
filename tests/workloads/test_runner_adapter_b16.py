"""B16 trusted-runner launch spec v3: training checkpoint/result handshake without Docker."""

from __future__ import annotations

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

from nexa.domain.workload_adapters import PYTORCH_CIFAR10
from nexa.workloads import adapter_launch, training_state
from nexa.workloads.canonical_json import canonical_json
from nexa.workloads.trusted_runner import RunnerState, RunnerSupervisor
from tests.workloads import b16_helpers as h
from tests.workloads.test_runner import ARTIFACT, CALLBACK, COMPLETION, RESULT
from tests.workloads.test_runner_checkpoint_b14 import CHECKPOINT, CHECKPOINT_2, _finalize, _last

CHECKPOINT_FILES = [rule.logical_name for rule in PYTORCH_CIFAR10.checkpoint_files]


def _runner(tmp_path: Path, spec: dict | None = None) -> RunnerSupervisor:
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)
    return RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=300,
        stop_grace_seconds=5,
        state_path=tmp_path / "run" / "runner-state.json",
        launch_spec=h.training_spec() if spec is None else spec,
        fs_root=tmp_path,
    )


def _started(runner: RunnerSupervisor) -> RunnerSupervisor:
    runner.accept_authority_deadline(runner.clock() + 10_000.0)
    runner.mark_workload_started()
    return runner


def _write_snapshot(tmp_path: Path, step: int, parameters: dict = h.PARAMETERS) -> dict:
    raw = h.snapshot_bytes(h.training_document(step, parameters), fill=step)
    (tmp_path / "output" / "state.safetensors").write_bytes(raw)
    return training_state.split_snapshot(raw)[1]


def _request(runner, sequence, *, checkpoint_sequence=1, checkpoint=CHECKPOINT):
    return {
        "schema_version": 1,
        "control_sequence": sequence,
        "type": "REQUEST_CHECKPOINT",
        "payload": {
            "reason": "INTERVAL",
            "reservation_callback_id": CALLBACK,
            "checkpoint_id": checkpoint,
            "checkpoint_sequence": checkpoint_sequence,
            "checkpoint_deadline_monotonic_ns": int((runner.clock() + 30) * 1_000_000_000),
        },
    }


def _artifact_id(index: int) -> str:
    return f"018f0d60-7b6a-7a3{index}-9d82-1aa39c4f30c0"


def _bind(sequence, batch, *, purpose, reserved_id):
    return {
        "schema_version": 1,
        "control_sequence": sequence,
        "type": "BIND_ARTIFACT_BATCH",
        "payload": {
            "purpose": purpose,
            "reservation_callback_id": CALLBACK,
            "reserved_id": reserved_id,
            "source_message_sequence": batch["message_sequence"],
            "bindings": [
                {**descriptor, "artifact_id": _artifact_id(index)}
                for index, descriptor in enumerate(batch["payload"]["artifacts"])
            ],
        },
    }


def _checksum(bindings) -> str:
    return "sha256:" + hashlib.sha256(rfc8785.dumps(bindings)).hexdigest()


def _checkpoint_cycle(runner, tmp_path, *, sequence=1, checkpoint=CHECKPOINT, control=1):
    assert (
        runner.apply_control(
            _request(runner, control, checkpoint_sequence=sequence, checkpoint=checkpoint)
        )
        == "ACCEPTED"
    )
    files = _last(runner, "CHECKPOINT_FILES_READY")
    bind = _bind(control + 1, files, purpose="CHECKPOINT", reserved_id=checkpoint)
    assert runner.apply_control(bind) == "ACCEPTED"
    finalize = _finalize(
        control + 2,
        _checksum(bind["payload"]["bindings"]),
        checkpoint=checkpoint,
        checkpoint_sequence=sequence,
    )
    assert runner.apply_control(finalize) == "ACCEPTED"
    ready = _last(runner, "CHECKPOINT_READY")
    raw = (tmp_path / "output" / ready["payload"]["manifest"]["staging_name"]).read_bytes()
    return files, bind["payload"]["bindings"], raw


def test_training_checkpoint_stages_four_closed_files_and_a_canonical_manifest(
    tmp_path: Path,
) -> None:
    runner = _started(_runner(tmp_path))
    expected_files = _write_snapshot(tmp_path, 5)
    before = len(runner.pending_messages)

    files, bindings, raw = _checkpoint_cycle(runner, tmp_path)

    messages = runner.pending_messages[before:]
    kinds = [message["type"] for message in messages]
    assert kinds.index("PROGRESS") < kinds.index("CHECKPOINT_FILES_READY")
    progress = messages[kinds.index("PROGRESS")]["payload"]
    assert (progress["step"], progress["epoch"], progress["item_cursor"]) == (5, 1, 64)
    assert progress["fraction"] == 5 / 8
    artifacts = files["payload"]["artifacts"]
    assert [item["logical_name"] for item in artifacts] == CHECKPOINT_FILES
    assert [item["media_type"] for item in artifacts] == [
        rule.media_type for rule in PYTORCH_CIFAR10.checkpoint_files
    ]
    for item in artifacts:
        staged = tmp_path / "output" / item["staging_name"]
        assert item["staging_name"] == f"checkpoint-1-{item['logical_name']}"
        assert stat.S_IMODE(staged.stat().st_mode) == 0o440
        assert staged.read_bytes() == expected_files[item["logical_name"]]
        assert item["checksum"] == "sha256:" + hashlib.sha256(staged.read_bytes()).hexdigest()

    manifest = json.loads(raw)
    assert rfc8785.dumps(manifest) == raw
    body = {key: value for key, value in manifest.items() if key != "manifest_checksum"}
    digest = hashlib.sha256(canonical_json(body)).hexdigest()
    assert manifest["manifest_checksum"] == "sha256:" + digest
    document = training_state.parse_training_state(expected_files["training-state.json"])
    assert manifest["cursor"] == training_state.runtime_cursor(document)
    assert manifest["state_components"] == list(PYTORCH_CIFAR10.state_components)
    assert len(manifest["state_components"]) == 6
    assert manifest["provenance"] == h.PROVENANCE
    assert manifest["compatibility"] == h.compatibility()
    assert [item["logical_name"] for item in manifest["files"]] == CHECKPOINT_FILES
    assert manifest["files"] == [
        {key: value for key, value in binding.items() if key not in {"staging_name", "kind"}}
        for binding in bindings
    ]


def test_next_checkpoint_replaces_staged_files_and_never_regresses(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path))
    _write_snapshot(tmp_path, 3)
    _checkpoint_cycle(runner, tmp_path)
    _write_snapshot(tmp_path, 6)
    _checkpoint_cycle(runner, tmp_path, sequence=2, checkpoint=CHECKPOINT_2, control=4)
    staged = sorted(path.name for path in (tmp_path / "output").glob("checkpoint-*"))
    assert staged == sorted(
        [f"checkpoint-2-{name}" for name in CHECKPOINT_FILES] + ["checkpoint-2-manifest.json"]
    )
    _write_snapshot(tmp_path, 5)
    assert runner.apply_control(_request(runner, 7, checkpoint_sequence=3)) == "INVALID"
    assert runner.state is RunnerState.STOPPED


@pytest.mark.parametrize(
    "parameters",
    [
        {**h.PARAMETERS, "seed": 8},
        {**h.PARAMETERS, "learning_rate": 0.5},
        {**h.PARAMETERS, "batch_size": 32},
    ],
)
def test_snapshot_of_another_run_is_never_staged(tmp_path: Path, parameters: dict) -> None:
    runner = _started(_runner(tmp_path))
    _write_snapshot(tmp_path, 1, parameters)
    assert runner.apply_control(_request(runner, 1)) == "INVALID"
    assert all(m["type"] != "CHECKPOINT_FILES_READY" for m in runner.pending_messages)
    assert not list((tmp_path / "output").glob("checkpoint-*"))


def test_corrupt_or_oversized_snapshot_is_rejected(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path))
    raw = bytearray(h.snapshot_bytes(h.training_document(2)))
    raw[20] ^= 0xFF
    (tmp_path / "output" / "state.safetensors").write_bytes(bytes(raw))
    assert runner.apply_control(_request(runner, 1)) == "INVALID"

    other = _started(_runner(tmp_path / "big"))
    (tmp_path / "big" / "output" / "state.safetensors").write_bytes(b"\0" * (4 * 1024 * 1024 + 1))
    assert other.apply_control(_request(other, 1)) == "INVALID"


def test_snapshot_symlink_is_never_followed(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path))
    target = tmp_path / "elsewhere.safetensors"
    target.write_bytes(h.snapshot_bytes(h.training_document(2)))
    os.symlink(target, tmp_path / "output" / "state.safetensors")
    assert runner.apply_control(_request(runner, 1)) == "INVALID"


def _write_restore(tmp_path: Path, step: int) -> dict:
    restore = h.restore_block(step)
    directory = tmp_path / "input" / "restore"
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in h.checkpoint_files(step).items():
        (directory / name).write_bytes(body)
    return restore


def test_restore_files_are_rechecked_before_launch(tmp_path: Path) -> None:
    restore = _write_restore(tmp_path, 5)
    runner = _runner(tmp_path, h.training_spec(restore=restore))
    assert runner._restore_state_is_valid() is True
    assert runner._launch_command()[-2:] == ("--resume-dir", "/input/restore")

    model = tmp_path / "input" / "restore" / "model.safetensors"
    body = bytearray(model.read_bytes())
    body[-1] ^= 0x01
    model.write_bytes(bytes(body))
    assert runner._restore_state_is_valid() is False


def test_restore_cursor_must_match_the_restored_state(tmp_path: Path) -> None:
    restore = _write_restore(tmp_path, 5)
    other = h.restore_block(6)
    # Files of step 5 declared as the checkpoint of step 6: checksums match, cursor does not.
    runner = _runner(tmp_path, h.training_spec(restore={**restore, "cursor": other["cursor"]}))
    assert runner._restore_state_is_valid() is False


def test_invalid_restore_fails_incompatible_without_starting_compute(tmp_path: Path) -> None:
    restore = _write_restore(tmp_path, 5)
    (tmp_path / "input" / "restore" / "rng.safetensors").unlink()
    runner = _runner(tmp_path, h.training_spec(restore=restore))
    runner.accept_authority_deadline(runner.clock() + 10_000.0)
    runner_side, supervisor_side = socket.socketpair()
    observed: list[bytes] = []

    def emulate_supervisor() -> None:
        supervisor_side.sendall(b"READY\n")
        supervisor_side.settimeout(0.5)
        with suppress(OSError):
            observed.append(supervisor_side.recv(64))

    peer = threading.Thread(target=emulate_supervisor)
    peer.start()
    try:
        runner.attach_supervisor_connection(runner_side, supervisor_pid=1234)
        runner._launch_from_spec()
        peer.join(timeout=2)
        failed = _last(runner, "FAILED")["payload"]
        assert (failed["failure_class"], failed["reason_code"]) == (
            "INCOMPATIBLE",
            "CHECKPOINT_RESTORE_UNAVAILABLE",
        )
        assert b"START\n" not in observed
        assert all(m["type"] != "STARTED" for m in runner.pending_messages)
    finally:
        supervisor_side.close()
        runner.close()


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [
        (65, ("INVALID_INPUT", "INVALID_INPUT")),
        (1, ("INTERNAL", "WORKLOAD_EXIT_NONZERO")),
        (137, ("INTERNAL", "WORKLOAD_EXIT_NONZERO")),
    ],
)
def test_workload_exit_codes_map_to_failure_classes(
    tmp_path: Path, exit_code: int, expected: tuple[str, str]
) -> None:
    runner = _started(_runner(tmp_path))
    runner._finish_adapter_workload(exit_code)
    failed = _last(runner, "FAILED")["payload"]
    assert (failed["failure_class"], failed["reason_code"]) == expected
    assert failed["exit_code"] == exit_code
    assert all(m["type"] != "RESULT_PREPARE" for m in runner.pending_messages)


def _write_result(tmp_path: Path, *, metrics: dict | None = None) -> tuple[bytes, bytes]:
    model = h.model_bytes(fill=3)
    raw = canonical_json(metrics if metrics is not None else h.metrics_document(model))
    (tmp_path / "output" / "model.safetensors").write_bytes(model)
    (tmp_path / "output" / "metrics.json").write_bytes(raw)
    return model, raw


def test_successful_exit_stages_model_and_metrics_with_scalar_manifest_metrics(
    tmp_path: Path,
) -> None:
    runner = _started(_runner(tmp_path))
    model, metrics = _write_result(tmp_path)
    runner._finish_adapter_workload(0)
    progress = _last(runner, "PROGRESS")["payload"]
    assert (progress["fraction"], progress["step"], progress["epoch"], progress["item_cursor"]) == (
        1.0,
        8,
        2,
        0,
    )
    prepare = _last(runner, "RESULT_PREPARE")
    token = prepare["payload"]["completion_token"]
    assert (
        runner.apply_control(
            {
                "schema_version": 1,
                "control_sequence": 1,
                "type": "PREPARE_RESULT",
                "payload": {
                    "completion_token": token,
                    "reservation_callback_id": CALLBACK,
                    "result_id": RESULT,
                },
            }
        )
        == "ACCEPTED"
    )
    batch = _last(runner, "RESULT_FILE_BATCH")
    artifacts = batch["payload"]["artifacts"]
    assert [(a["logical_name"], a["kind"], a["media_type"]) for a in artifacts] == [
        (rule.logical_name, "RESULT_FILE", rule.media_type) for rule in PYTORCH_CIFAR10.result_files
    ]
    assert [a["checksum"] for a in artifacts] == [
        "sha256:" + hashlib.sha256(model).hexdigest(),
        "sha256:" + hashlib.sha256(metrics).hexdigest(),
    ]
    bind = _bind(2, batch, purpose="RESULT", reserved_id=RESULT)
    assert runner.apply_control(bind) == "ACCEPTED"
    finalize = {
        "schema_version": 1,
        "control_sequence": 3,
        "type": "FINALIZE_RESULT_MANIFEST",
        "payload": {
            "completion_token": token,
            "reservation_callback_id": CALLBACK,
            "result_id": RESULT,
            "binding_set_checksum": _checksum(bind["payload"]["bindings"]),
        },
    }
    assert runner.apply_control(finalize) == "ACCEPTED"
    ready = _last(runner, "RESULT_READY")
    raw = (tmp_path / "output" / ready["payload"]["manifest"]["staging_name"]).read_bytes()
    manifest = json.loads(raw)
    assert rfc8785.dumps(manifest) == raw
    assert manifest["metrics"] == adapter_launch.manifest_metrics(h.metrics_document(model))
    assert manifest["provenance"] == h.PROVENANCE
    assert [item["logical_name"] for item in manifest["files"]] == [
        "model.safetensors",
        "metrics.json",
    ]
    assert token != COMPLETION
    assert ARTIFACT not in raw.decode()


@pytest.mark.parametrize("defect", ["metrics", "model", "missing", "symlink"])
def test_invalid_result_files_fail_internal(tmp_path: Path, defect: str) -> None:
    runner = _started(_runner(tmp_path))
    model, _ = _write_result(tmp_path)
    output = tmp_path / "output"
    if defect == "metrics":
        _write_result(tmp_path, metrics={**h.metrics_document(model), "steps": 7})
    elif defect == "model":
        # Metrics name the original model; the file on disk differs.
        (output / "model.safetensors").write_bytes(h.model_bytes(fill=4))
    elif defect == "missing":
        (output / "metrics.json").unlink()
    else:
        (output / "elsewhere.json").write_bytes((output / "metrics.json").read_bytes())
        (output / "metrics.json").unlink()
        os.symlink(output / "elsewhere.json", output / "metrics.json")
    runner._finish_adapter_workload(0)
    failed = _last(runner, "FAILED")["payload"]
    assert (failed["failure_class"], failed["reason_code"]) == ("INTERNAL", "INVALID_RESULT")
    assert all(m["type"] != "RESULT_PREPARE" for m in runner.pending_messages)


def test_runner_state_with_a_training_checkpoint_survives_reload(tmp_path: Path) -> None:
    runner = _started(_runner(tmp_path))
    _write_snapshot(tmp_path, 4)
    assert runner.apply_control(_request(runner, 1)) == "ACCEPTED"
    reloaded = _runner(tmp_path)
    assert reloaded._checkpoint == runner._checkpoint
    assert [item["logical_name"] for item in reloaded._checkpoint["descriptors"]] == (
        CHECKPOINT_FILES
    )
