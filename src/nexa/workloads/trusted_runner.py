"""In-image runner state machine independent of worker liveness."""

import argparse
import hashlib
import json
import math
import os
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from nexa.worker.protocol import (
    FrameDecoder,
    ProtocolError,
    SequenceState,
    canonical_envelope_effect,
    encode_frame,
    validate_ack,
    validate_control_envelope,
    validate_envelope,
)
from nexa.workloads.cpu_state import CpuState, CpuStateError, decode_state, read_state_file

CHECKPOINT_STATE_PATH = "/output/state.json"
RESTORE_STATE_PATH = "/input/restore-state.json"
_LAUNCH_SPEC_FIELDS = {
    "schema_version",
    "adapter_id",
    "startup_nonce",
    "input_path",
    "output_path",
    "result_logical_name",
    "media_type",
    "iterations",
    "seed",
    "modulus",
    "spec_checksum",
    "provenance",
}
_COMPATIBILITY_FIELDS = {
    "architecture",
    "device_type",
    "framework",
    "framework_version",
    "cuda_version",
    "minimum_driver_version",
    "gpu_compute_capability",
    "checkpointable",
    "restart_safe",
}
_RESTORE_FIELDS = {
    "path",
    "checkpoint_id",
    "checkpoint_sequence",
    "step",
    "accumulator",
    "state_checksum",
}


class RunnerState(StrEnum):
    WAITING_AUTHORITY = "WAITING_AUTHORITY"
    RUNNING = "RUNNING"
    RESULT_HANDSHAKE = "RESULT_HANDSHAKE"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class _StartupDeadlineExpired(RuntimeError):
    pass


class RunnerSupervisor:
    def __init__(
        self,
        *,
        startup_limit_seconds: int,
        runtime_limit_seconds: int,
        stop_grace_seconds: int,
        state_path: str | Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        log_limit_bytes: int = 1024 * 1024,
        launch_spec: dict[str, object] | None = None,
        fs_root: str | Path = "/",
    ) -> None:
        if (
            startup_limit_seconds != 30
            or not 1 <= runtime_limit_seconds <= 300
            or stop_grace_seconds != 5
        ):
            raise ValueError("runner bounds do not match B09 contract")
        if log_limit_bytes < 1:
            raise ValueError("log_limit_bytes must be positive")
        self.startup_limit_seconds = startup_limit_seconds
        self.runtime_limit_seconds = runtime_limit_seconds
        self.stop_grace_seconds = stop_grace_seconds
        self.clock = clock
        self.log_limit_bytes = log_limit_bytes
        self.launch_spec = _validate_launch_spec(launch_spec) if launch_spec is not None else None
        self.state_path = Path(state_path) if state_path is not None else None
        # Container paths in the launch spec resolve under this root (tests use a tmp dir).
        self.fs_root = Path(fs_root)
        self.created_at = clock()
        self.state = RunnerState.WAITING_AUTHORITY
        self.authority_deadline: float | None = None
        self.runtime_started_at: float | None = None
        self._control_sequences = SequenceState()
        self._pending_messages: list[dict[str, object]] = []
        self._next_message_sequence = 1
        self._progress_sequence = 0
        self._last_progress_payload: dict[str, object] | None = None
        self._last_progress_envelope: dict[str, object] | None = None
        self._result: dict[str, object] | None = None
        self._checkpoint: dict[str, object] | None = None
        self._stop_reason: str | None = None
        self.workload: subprocess.Popen[bytes] | None = None
        self.workload_process_group: int | None = None
        self._workload_control_fd: int | None = None
        self._workload_started = False
        self._supervisor_socket: socket.socket | None = None
        self._supervisor_thread: threading.Thread | None = None
        self._supervisor_started = threading.Event()
        self._supervisor_exited = threading.Event()
        self._supervisor_exit_code: int | None = None
        self._log_bytes = 0
        self._log_overflow = threading.Event()
        self._fatal_error = threading.Event()
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        if self.state_path is not None and self.state_path.exists():
            self._load_state()

    @property
    def compute_allowed(self) -> bool:
        return self.state in {RunnerState.RUNNING, RunnerState.RESULT_HANDSHAKE}

    @property
    def fatal_error(self) -> bool:
        return self._fatal_error.is_set()

    @property
    def pending_messages(self) -> list[dict[str, object]]:
        with self._lock:
            return list(self._pending_messages)

    @property
    def result_binding_set_checksum(self) -> str | None:
        if self._result is None:
            return None
        value = self._result.get("binding_set_checksum")
        return str(value) if value is not None else None

    def accept_authority_deadline(self, deadline: float) -> None:
        if not isinstance(deadline, int | float) or not math.isfinite(deadline) or deadline < 0:
            raise ValueError("authority deadline must be finite and non-negative")
        with self._lock:
            if self.state in {RunnerState.STOPPING, RunnerState.STOPPED}:
                raise ValueError("stopped runner cannot regain authority")
            now = self.clock()
            if deadline <= now:
                self.request_stop("LEASE_DEADLINE", now=now)
                raise ValueError("authority deadline must be in the future")
            if self.authority_deadline is not None and now >= self.authority_deadline:
                self.request_stop("LEASE_DEADLINE", now=now)
                raise ValueError("expired authority cannot be renewed")
            if self.authority_deadline is not None and deadline < self.authority_deadline:
                raise ValueError("authority deadline cannot move backwards")
            self.authority_deadline = float(deadline)
            if self.state == RunnerState.WAITING_AUTHORITY:
                self.state = RunnerState.RUNNING
            self._persist()

    def mark_workload_started(self, *, now: float | None = None) -> None:
        instant = self.clock() if now is None else now
        if self.runtime_started_at is None:
            self.runtime_started_at = instant
        if self.state == RunnerState.WAITING_AUTHORITY:
            raise RuntimeError("workload cannot start without authority")
        self._persist()

    def begin_result_handshake(self) -> None:
        if self.state not in {RunnerState.RUNNING, RunnerState.RESULT_HANDSHAKE}:
            raise ValueError("result handshake requires running workload")
        self.state = RunnerState.RESULT_HANDSHAKE
        self._persist()

    def request_stop(self, reason: str, *, now: float) -> None:
        del now
        if self.state != RunnerState.STOPPED:
            if self._stop_reason is None:
                self._stop_reason = reason
            self.state = RunnerState.STOPPING
            self._persist()

    def fail_closed(self) -> None:
        with self._lock:
            if self.state != RunnerState.STOPPED:
                self.state = RunnerState.STOPPING
            self._fatal_error.set()
            self._persist()

    def apply_control(self, envelope: dict[str, object]) -> str:
        validated = validate_control_envelope(envelope)
        sequence = int(validated["control_sequence"])
        effect = canonical_envelope_effect(validated)
        with self._lock:
            result = self._control_sequences.classify(sequence, effect)
            terminal = self.state in {RunnerState.STOPPING, RunnerState.STOPPED}
            if result == "INVALID" and not terminal:
                instant = self.clock()
                self.request_stop("FAILURE", now=instant)
                self.stop_workload(now=instant, grace_seconds=0)
                return result
            if result != "ACCEPTED":
                return result
            if terminal:
                return "INVALID"
            try:
                self._apply_control_effect(validated)
            except (ProtocolError, ValueError):
                instant = self.clock()
                self.request_stop("FAILURE", now=instant)
                self.stop_workload(now=instant, grace_seconds=0)
                return "INVALID"
            self._control_sequences.commit(sequence, effect)
            self._persist()
            return result

    def _apply_control_effect(self, envelope: dict[str, object]) -> None:
        payload = envelope["payload"]
        assert isinstance(payload, dict)
        message_type = envelope["type"]
        if message_type == "SET_AUTHORITY_DEADLINE":
            self.accept_authority_deadline(int(payload["deadline_monotonic_ns"]) / 1_000_000_000)
            self._launch_from_spec()
        elif message_type == "REQUEST_STOP":
            now = self.clock()
            deadline = int(payload["grace_deadline_monotonic_ns"]) / 1_000_000_000
            self.request_stop(str(payload["reason"]), now=now)
            grace = min(self.stop_grace_seconds, max(0.0, deadline - now))
            self.stop_workload(now=now, grace_seconds=grace)
        elif message_type == "PREPARE_RESULT":
            self._prepare_result(payload)
        elif message_type == "BIND_ARTIFACT_BATCH" and payload["purpose"] == "CHECKPOINT":
            self._bind_checkpoint_artifacts(payload)
        elif message_type == "BIND_ARTIFACT_BATCH":
            self._bind_result_artifacts(payload)
        elif message_type == "FINALIZE_RESULT_MANIFEST":
            self._finalize_result(payload)
        elif message_type == "REQUEST_CHECKPOINT":
            self._request_checkpoint(payload)
        elif message_type == "FINALIZE_CHECKPOINT_MANIFEST":
            self._finalize_checkpoint(payload)
        else:
            raise ProtocolError("unsupported control type")

    def emit_progress(
        self,
        *,
        fraction: float,
        step: int | None,
        epoch: int | None = None,
        item_cursor: int | None = None,
    ) -> dict[str, object]:
        with self._lock:
            if not isinstance(fraction, int | float) or not math.isfinite(fraction):
                raise ProtocolError("progress fraction must be finite")
            if not 0 <= fraction <= 1:
                raise ProtocolError("progress fraction is outside 0..1")
            for name, value in (("step", step), ("epoch", epoch), ("item_cursor", item_cursor)):
                if value is not None and (
                    not isinstance(value, int) or isinstance(value, bool) or value < 0
                ):
                    raise ProtocolError(f"progress {name} is invalid")
            snapshot = {
                "fraction": float(fraction),
                "step": step,
                "epoch": epoch,
                "item_cursor": item_cursor,
            }
            if snapshot == self._last_progress_payload and self._last_progress_envelope is not None:
                return self._last_progress_envelope
            self._progress_sequence += 1
            payload = {"progress_sequence": self._progress_sequence, **snapshot}
            envelope = self._emit("PROGRESS", payload)
            self._last_progress_payload = snapshot
            self._last_progress_envelope = envelope
            self._persist()
            return envelope

    def stage_result(
        self,
        path: str | Path,
        *,
        completion_token: str,
        logical_name: str,
        media_type: str,
        provenance: dict[str, object],
    ) -> dict[str, object] | None:
        _uuid_v7(completion_token, "completion_token")
        _validate_result_provenance(provenance)
        source = Path(path)
        metadata = source.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ProtocolError("result must be a regular non-symlink file")
        body = source.read_bytes()
        if len(body) != metadata.st_size:
            raise ProtocolError("result changed while it was staged")
        descriptor = {
            "staging_name": source.name,
            "logical_name": logical_name,
            "kind": "RESULT_FILE",
            "media_type": media_type,
            "size_bytes": len(body),
            "checksum": "sha256:" + hashlib.sha256(body).hexdigest(),
        }
        _validate_staged_descriptor(descriptor)
        with self._lock:
            if self._result is not None:
                if self._result.get("completion_token") != completion_token:
                    raise ProtocolError("result completion token conflict")
                if self._result.get("prepare_deferred"):
                    return None
                pending = self._find_pending("RESULT_PREPARE")
                if pending is None:
                    raise ProtocolError("result prepare state is inconsistent")
                return pending
            # Messages are strictly ordered and the server refuses a result
            # reservation while the attempt is CHECKPOINTING, so an open
            # checkpoint cycle must reach CHECKPOINT_READY before RESULT_PREPARE.
            deferred = self._checkpoint is not None and self._checkpoint["manifest"] is None
            self._result = {
                "completion_token": completion_token,
                "descriptor": descriptor,
                "descriptor_checksum": _checksum_json(descriptor),
                "source_path": str(source),
                "provenance": provenance,
                "reservation_callback_id": None,
                "result_id": None,
                "batch_message_sequence": None,
                "bindings": None,
                "binding_set_checksum": None,
                "manifest": None,
                "prepare_deferred": deferred,
            }
            self.begin_result_handshake()
            if deferred:
                return None
            envelope = self._emit("RESULT_PREPARE", {"completion_token": completion_token})
            self._persist()
            return envelope

    def _prepare_result(self, payload: dict[str, object]) -> None:
        if self._result is None:
            raise ProtocolError("result bytes are not staged")
        for field in ("completion_token", "reservation_callback_id", "result_id"):
            expected = self._result.get(field)
            if expected is not None and payload[field] != expected:
                raise ProtocolError(f"result {field} conflict")
        if payload["completion_token"] != self._result["completion_token"]:
            raise ProtocolError("result completion token mismatch")
        self._result["reservation_callback_id"] = payload["reservation_callback_id"]
        self._result["result_id"] = payload["result_id"]
        if self._result.get("batch_message_sequence") is None:
            envelope = self._emit(
                "RESULT_FILE_BATCH",
                {
                    "completion_token": payload["completion_token"],
                    "reservation_callback_id": payload["reservation_callback_id"],
                    "result_id": payload["result_id"],
                    "batch_index": 0,
                    "batch_count": 1,
                    "artifacts": [self._result["descriptor"]],
                },
            )
            self._result["batch_message_sequence"] = envelope["message_sequence"]

    def _bind_result_artifacts(self, payload: dict[str, object]) -> None:
        if self._result is None:
            raise ProtocolError("result is not prepared")
        if payload["purpose"] != "RESULT":
            raise ProtocolError("binding purpose is not RESULT")
        if payload["reservation_callback_id"] != self._result.get("reservation_callback_id"):
            raise ProtocolError("binding reservation callback mismatch")
        if payload["reserved_id"] != self._result.get("result_id"):
            raise ProtocolError("binding result identity mismatch")
        if payload["source_message_sequence"] != self._result.get("batch_message_sequence"):
            raise ProtocolError("binding source message mismatch")
        bindings = payload["bindings"]
        _match_bindings([self._result["descriptor"]], bindings, label="result")
        if self._result.get("bindings") is not None and self._result["bindings"] != bindings:
            raise ProtocolError("result binding conflict")
        self._result["bindings"] = bindings
        self._result["binding_set_checksum"] = _checksum_json(bindings)

    def _finalize_result(self, payload: dict[str, object]) -> None:
        if self._result is None or self._result.get("bindings") is None:
            raise ProtocolError("result bindings are incomplete")
        for field in ("completion_token", "reservation_callback_id", "result_id"):
            if payload[field] != self._result.get(field):
                raise ProtocolError(f"finalize {field} mismatch")
        if payload["binding_set_checksum"] != self._result.get("binding_set_checksum"):
            raise ProtocolError("binding set checksum mismatch")
        if self._result.get("manifest") is not None:
            return
        bindings = self._result["bindings"]
        assert isinstance(bindings, list)
        manifest_without_checksum = {
            "kind": "RESULT",
            "schema_version": 1,
            "result_id": self._result["result_id"],
            "created_at": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "provenance": self._result["provenance"],
            "status": "SUCCEEDED",
            "files": [
                {
                    key: value
                    for key, value in binding.items()
                    if key not in {"staging_name", "kind"}
                }
                for binding in bindings
            ],
            "metrics": {},
        }
        manifest = {
            **manifest_without_checksum,
            "manifest_checksum": _checksum_json(manifest_without_checksum),
        }
        source = Path(str(self._result["source_path"]))
        manifest_path = source.with_name("result-manifest.json")
        manifest_bytes = _canonical_json(manifest)
        _atomic_write(manifest_path, manifest_bytes, mode=0o440)
        descriptor = {
            "staging_name": manifest_path.name,
            "logical_name": "result.manifest.json",
            "kind": "RESULT_MANIFEST",
            "media_type": "application/json",
            "size_bytes": len(manifest_bytes),
            "checksum": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
        }
        self._result["manifest"] = descriptor
        self._emit(
            "RESULT_READY",
            {
                "completion_token": self._result["completion_token"],
                "reservation_callback_id": self._result["reservation_callback_id"],
                "result_id": self._result["result_id"],
                "manifest": descriptor,
            },
        )

    def _container_path(self, path: object) -> Path:
        return self.fs_root / str(path).lstrip("/")

    def _checkpoint_spec(self) -> dict[str, object]:
        spec = self.launch_spec
        if spec is None or spec["schema_version"] != 2:
            raise ProtocolError("launch spec does not enable checkpoints")
        return spec

    def _decode_cpu_state(self, raw: bytes) -> CpuState:
        spec = self._checkpoint_spec()
        provenance = spec["provenance"]
        assert isinstance(provenance, dict)
        return decode_state(
            raw,
            iterations=int(spec["iterations"]),
            modulus=int(spec["modulus"]),
            input_checksum=str(provenance["input_checksum"]),
            spec_checksum=str(spec["spec_checksum"]),
        )

    def _request_checkpoint(self, payload: dict[str, object]) -> None:
        spec = self._checkpoint_spec()
        if payload["reason"] == "PAUSE":
            # Checkpoint-for-pause stops the workload; that transition is B15 scope.
            raise ProtocolError("checkpoint for pause is not supported")
        if self.state not in {RunnerState.RUNNING, RunnerState.RESULT_HANDSHAKE}:
            raise ProtocolError("checkpoint requires a running workload")
        if self.runtime_started_at is None:
            raise ProtocolError("checkpoint requires a started workload")
        if int(payload["checkpoint_deadline_monotonic_ns"]) / 1_000_000_000 <= self.clock():
            raise ProtocolError("checkpoint deadline has passed")
        identity = ("reservation_callback_id", "checkpoint_id", "checkpoint_sequence")
        current = self._checkpoint
        if current is not None:
            if all(current[field] == payload[field] for field in identity):
                # The same reservation replayed on a fresh sequence reuses its staging.
                return
            if current["manifest"] is None:
                raise ProtocolError("previous checkpoint handshake is incomplete")
            if int(payload["checkpoint_sequence"]) <= int(current["checkpoint_sequence"]):
                raise ProtocolError("checkpoint sequence must advance")
        if self._result is not None and self._result["reservation_callback_id"] is not None:
            # The server refuses a checkpoint reservation beside an active result one.
            raise ProtocolError("checkpoint request arrived after the result was reserved")
        # A staged but unreserved result is fine: the worker defers the queued
        # RESULT_PREPARE until this cycle publishes, and the final state is valid.
        checkpoint = spec["checkpoint"]
        assert isinstance(checkpoint, dict)
        raw = read_state_file(self._container_path(checkpoint["state_path"]))
        state = self._decode_cpu_state(raw)
        restore = spec["restore"]
        floor = max(
            int(restore["step"]) if isinstance(restore, dict) else 0,
            int(current["step"]) if current is not None else 0,
        )
        if state.step < floor:
            raise ProtocolError("checkpoint state regressed below the committed cursor")
        staging = self._container_path(spec["output_path"]).parent
        if current is not None:
            for descriptor in (current["descriptor"], current["manifest"]):
                assert isinstance(descriptor, dict)
                with suppress(FileNotFoundError):
                    (staging / str(descriptor["staging_name"])).unlink()
        sequence = int(payload["checkpoint_sequence"])
        state_path = staging / f"checkpoint-{sequence}-state.json"
        # A closed read-only copy: the workload keeps rewriting its live snapshot.
        _atomic_write(state_path, raw, mode=0o440)
        descriptor = {
            "staging_name": state_path.name,
            "logical_name": "state.json",
            "kind": "CHECKPOINT_FILE",
            "media_type": "application/json",
            "size_bytes": len(raw),
            "checksum": "sha256:" + hashlib.sha256(raw).hexdigest(),
        }
        # Record before emit so one persisted snapshot holds both the message and its owner.
        self._checkpoint = {
            "reservation_callback_id": payload["reservation_callback_id"],
            "checkpoint_id": payload["checkpoint_id"],
            "checkpoint_sequence": sequence,
            "step": state.step,
            "accumulator": state.accumulator,
            "descriptor": descriptor,
            "batch_message_sequence": self._next_message_sequence,
            "bindings": None,
            "binding_set_checksum": None,
            "manifest": None,
        }
        self._emit(
            "CHECKPOINT_FILES_READY",
            {
                "reservation_callback_id": payload["reservation_callback_id"],
                "checkpoint_id": payload["checkpoint_id"],
                "checkpoint_sequence": sequence,
                "batch_index": 0,
                "batch_count": 1,
                "artifacts": [descriptor],
            },
        )

    def _bind_checkpoint_artifacts(self, payload: dict[str, object]) -> None:
        record = self._checkpoint
        if record is None:
            raise ProtocolError("checkpoint is not requested")
        if payload["reservation_callback_id"] != record["reservation_callback_id"]:
            raise ProtocolError("binding reservation callback mismatch")
        if payload["reserved_id"] != record["checkpoint_id"]:
            raise ProtocolError("binding checkpoint identity mismatch")
        if payload["source_message_sequence"] != record["batch_message_sequence"]:
            raise ProtocolError("binding source message mismatch")
        bindings = payload["bindings"]
        _match_bindings([record["descriptor"]], bindings, label="checkpoint")
        if record["bindings"] is not None and record["bindings"] != bindings:
            raise ProtocolError("checkpoint binding conflict")
        record["bindings"] = bindings
        record["binding_set_checksum"] = _checksum_json(bindings)

    def _finalize_checkpoint(self, payload: dict[str, object]) -> None:
        spec = self._checkpoint_spec()
        record = self._checkpoint
        if record is None or record["bindings"] is None:
            raise ProtocolError("checkpoint bindings are incomplete")
        for field in ("reservation_callback_id", "checkpoint_id", "checkpoint_sequence"):
            if payload[field] != record[field]:
                raise ProtocolError(f"finalize {field} mismatch")
        if payload["binding_set_checksum"] != record["binding_set_checksum"]:
            raise ProtocolError("binding set checksum mismatch")
        if record["manifest"] is not None:
            return
        bindings = record["bindings"]
        checkpoint = spec["checkpoint"]
        assert isinstance(bindings, list) and isinstance(checkpoint, dict)
        step = int(record["step"])
        manifest_without_checksum = {
            "kind": "CHECKPOINT",
            "schema_version": 1,
            "checkpoint_id": record["checkpoint_id"],
            "checkpoint_sequence": record["checkpoint_sequence"],
            "created_at": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "provenance": spec["provenance"],
            "compatibility": checkpoint["compatibility"],
            "cursor": {
                "step": step,
                "epoch": 0,
                "item_cursor": step,
                "accumulator": record["accumulator"],
            },
            "state_components": ["ACCUMULATOR"],
            "files": [
                {
                    key: value
                    for key, value in binding.items()
                    if key not in {"staging_name", "kind"}
                }
                for binding in bindings
            ],
        }
        manifest = {
            **manifest_without_checksum,
            "manifest_checksum": _checksum_json(manifest_without_checksum),
        }
        manifest_bytes = _canonical_json(manifest)
        manifest_path = (
            self._container_path(spec["output_path"]).parent
            / f"checkpoint-{record['checkpoint_sequence']}-manifest.json"
        )
        _atomic_write(manifest_path, manifest_bytes, mode=0o440)
        descriptor = {
            "staging_name": manifest_path.name,
            "logical_name": "checkpoint.manifest.json",
            "kind": "CHECKPOINT_MANIFEST",
            "media_type": "application/json",
            "size_bytes": len(manifest_bytes),
            "checksum": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
        }
        record["manifest"] = descriptor
        self._emit(
            "CHECKPOINT_READY",
            {
                "reservation_callback_id": record["reservation_callback_id"],
                "checkpoint_id": record["checkpoint_id"],
                "checkpoint_sequence": record["checkpoint_sequence"],
                "manifest": descriptor,
            },
        )
        result = self._result
        if result is not None and result.get("prepare_deferred"):
            result["prepare_deferred"] = False
            self._emit("RESULT_PREPARE", {"completion_token": result["completion_token"]})

    def _restore_state_is_valid(self) -> bool:
        spec = self.launch_spec
        restore = spec.get("restore") if spec is not None else None
        if not isinstance(restore, dict):
            return True
        try:
            raw = read_state_file(self._container_path(restore["path"]))
            state = self._decode_cpu_state(raw)
        except (CpuStateError, ProtocolError):
            return False
        return (
            "sha256:" + hashlib.sha256(raw).hexdigest() == restore["state_checksum"]
            and state.step == restore["step"]
            and state.accumulator == restore["accumulator"]
        )

    def _emit(self, message_type: str, payload: dict[str, object]) -> dict[str, object]:
        with self._lock:
            envelope = {
                "schema_version": 1,
                "message_sequence": self._next_message_sequence,
                "type": message_type,
                "payload": payload,
            }
            validate_envelope(envelope)
            encode_frame(envelope)
            self._next_message_sequence += 1
            self._pending_messages.append(envelope)
            self._persist()
            return envelope

    def acknowledge_message(self, ack: dict[str, object]) -> None:
        validated = validate_ack(ack)
        if not validated["accepted"]:
            return
        with self._lock:
            sequence = validated["ack_sequence"]
            self._pending_messages = [
                message
                for message in self._pending_messages
                if message.get("message_sequence") != sequence
            ]
            self._persist()

    def _find_pending(self, message_type: str) -> dict[str, object] | None:
        return next(
            (message for message in self._pending_messages if message["type"] == message_type),
            None,
        )

    def launch_workload(
        self,
        command: tuple[str, ...],
        *,
        workload_uid: int = 1001,
        workload_gid: int = 1000,
    ) -> int:
        if not self.compute_allowed:
            raise RuntimeError("authority deadline must be accepted before compute")
        if self._supervisor_socket is not None:
            if self._workload_started:
                assert self.workload_process_group is not None
                return self.workload_process_group
            started_at = self.clock()
            if started_at >= self.created_at + self.startup_limit_seconds:
                raise _StartupDeadlineExpired
            previous_runtime_started_at = self.runtime_started_at
            self._workload_started = True
            if self.runtime_started_at is None:
                self.runtime_started_at = started_at
            try:
                self._supervisor_socket.sendall(b"START\n")
            except OSError:
                self._workload_started = False
                self.runtime_started_at = previous_runtime_started_at
                raise
            self._persist()
            if not self._supervisor_started.wait(timeout=5):
                raise RuntimeError("workload supervisor did not confirm start")
            if self.workload_process_group is None:
                raise RuntimeError("workload supervisor returned an invalid process identity")
            return self.workload_process_group
        if self.workload is None:
            self.prepare_workload(
                command,
                workload_uid=workload_uid,
                workload_gid=workload_gid,
            )
        if self._workload_started:
            assert self.workload is not None
            return self.workload.pid
        if self._workload_control_fd is None or self.workload is None:
            raise RuntimeError("workload supervisor is not prepared")
        started_at = self.clock()
        if started_at >= self.created_at + self.startup_limit_seconds:
            raise _StartupDeadlineExpired
        previous_runtime_started_at = self.runtime_started_at
        self._workload_started = True
        if self.runtime_started_at is None:
            self.runtime_started_at = started_at
        try:
            os.write(self._workload_control_fd, b"START\n")
        except OSError:
            self._workload_started = False
            self.runtime_started_at = previous_runtime_started_at
            raise
        self._persist()
        return self.workload.pid

    def attach_supervisor_connection(
        self, connection: socket.socket, *, supervisor_pid: int
    ) -> None:
        if supervisor_pid < 1:
            raise ValueError("supervisor PID must be positive")
        with self._lock:
            if self._supervisor_socket is not None:
                raise RuntimeError("workload supervisor is already registered")
            connection.settimeout(5)
            greeting = bytearray()
            while b"\n" not in greeting and len(greeting) <= 64:
                chunk = connection.recv(64)
                if not chunk:
                    raise RuntimeError("workload supervisor closed during registration")
                greeting.extend(chunk)
            raw, _, remainder = greeting.partition(b"\n")
            if raw != b"READY" or remainder:
                raise RuntimeError("workload supervisor registration is invalid")
            connection.settimeout(None)
            self._supervisor_socket = connection

            def read_status() -> None:
                buffer = bytearray()
                try:
                    while True:
                        data = connection.recv(4096)
                        if not data:
                            return
                        buffer.extend(data)
                        while b"\n" in buffer:
                            line, _, tail = buffer.partition(b"\n")
                            buffer[:] = tail
                            if line.startswith(b"STARTED "):
                                pid = int(line.removeprefix(b"STARTED "))
                                if pid < 1:
                                    raise ValueError("invalid workload PID")
                                self.workload_process_group = pid
                                self._supervisor_started.set()
                            elif line.startswith(b"EXIT "):
                                self._supervisor_exit_code = int(line.removeprefix(b"EXIT "))
                                self._supervisor_exited.set()
                                return
                            elif line == b"LOG_OVERFLOW":
                                self._log_overflow.set()
                            else:
                                raise ValueError("invalid workload supervisor status")
                except (OSError, ValueError):
                    self._supervisor_exited.set()

            self._supervisor_thread = threading.Thread(target=read_status, daemon=True)
            self._supervisor_thread.start()
        self._launch_from_spec()

    def prepare_workload(
        self,
        command: tuple[str, ...],
        *,
        workload_uid: int = 1001,
        workload_gid: int = 1000,
    ) -> int:
        if self.workload is not None:
            return self.workload.pid
        if os.name != "posix":
            raise RuntimeError("workload isolation requires a POSIX runner")

        read_fd, write_fd = os.pipe()

        def drop_workload_identity() -> None:
            if os.geteuid() == 0:
                os.setgid(workload_gid)
                os.setuid(workload_uid)
            elif os.geteuid() != workload_uid:
                raise PermissionError("runner cannot create the configured workload UID")

        supervisor_command = (
            "python",
            "-m",
            "nexa.workloads.workload_supervisor",
            "--control-fd",
            str(read_fd),
            "--",
            *command,
        )
        workload_environment = os.environ.copy()
        for name in ("NEXA_CONTROL_SOCKET", "NEXA_RUNNER_STATE", "NEXA_LAUNCH_SPEC"):
            workload_environment.pop(name, None)
        try:
            self.workload = subprocess.Popen(
                supervisor_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
                preexec_fn=drop_workload_identity,
                pass_fds=(read_fd,),
                env=workload_environment,
            )
        finally:
            os.close(read_fd)
        self._workload_control_fd = write_fd
        self.workload_process_group = self.workload.pid
        for stream in (self.workload.stdout, self.workload.stderr):
            assert stream is not None
            threading.Thread(target=self._drain_log, args=(stream,), daemon=True).start()
        return self.workload.pid

    def prepare_launch_spec(self) -> None:
        if self.launch_spec is None:
            return
        self.prepare_workload(self._launch_command())

    def drop_runner_identity(self, *, runner_uid: int = 1000, runner_gid: int = 1000) -> None:
        if os.geteuid() == 0:
            os.setgid(runner_gid)
            os.setuid(runner_uid)
        if os.geteuid() != runner_uid or os.getegid() != runner_gid:
            raise RuntimeError("trusted runner identity could not be established")

    def _launch_from_spec(self) -> None:
        with self._lock:
            if (
                self.launch_spec is None
                or self._workload_started
                or self._supervisor_socket is None
                or self.state
                in {RunnerState.WAITING_AUTHORITY, RunnerState.STOPPING, RunnerState.STOPPED}
            ):
                return
            now = self.clock()
            if now >= self.created_at + self.startup_limit_seconds:
                self.request_stop("RUNTIME_LIMIT", now=now)
                self.stop_workload(now=now, grace_seconds=0)
                return
            if self.authority_deadline is None or now >= self.authority_deadline:
                self.request_stop("LEASE_DEADLINE", now=now)
                self.stop_workload(now=now, grace_seconds=0)
                return
            spec = self.launch_spec
            if not self._restore_state_is_valid():
                # Worker verified the checkpoint too; a mismatch here never reaches compute.
                self._emit(
                    "FAILED",
                    {
                        "failure_class": "INCOMPATIBLE",
                        "reason_code": "CHECKPOINT_RESTORE_UNAVAILABLE",
                        "exit_code": None,
                        "oom_killed": False,
                        "runtime_limit_reached": False,
                    },
                )
                self.request_stop("FAILURE", now=now)
                self.stop_workload(now=now, grace_seconds=0)
                return
            try:
                pid = self.launch_workload(self._launch_command())
            except _StartupDeadlineExpired:
                now = self.clock()
                self.request_stop("RUNTIME_LIMIT", now=now)
                self.stop_workload(now=now, grace_seconds=0)
                return
            self._emit(
                "STARTED",
                {
                    "startup_nonce": spec["startup_nonce"],
                    "pid": pid,
                    "started_monotonic_ns": int(self.clock() * 1_000_000_000),
                },
            )
            # A resumed Attempt continues from the verified restored cursor.
            restore = spec.get("restore")
            step = int(restore["step"]) if isinstance(restore, dict) else 0
            self.emit_progress(fraction=step / int(spec["iterations"]), step=step)

        def monitor() -> None:
            if self._supervisor_socket is not None:
                self._supervisor_exited.wait()
                exit_code = self._supervisor_exit_code
                if exit_code is None:
                    exit_code = -1
            else:
                assert self.workload is not None
                exit_code = self.workload.wait()
            if self.state in {RunnerState.STOPPING, RunnerState.STOPPED}:
                return
            if exit_code != 0:
                self._emit(
                    "FAILED",
                    {
                        "failure_class": "INTERNAL",
                        "reason_code": "WORKLOAD_EXIT_NONZERO",
                        "exit_code": exit_code,
                        "oom_killed": False,
                        "runtime_limit_reached": False,
                    },
                )
                self._persist()
                return
            self.emit_progress(fraction=1.0, step=int(spec["iterations"]))
            self.stage_result(
                str(spec["output_path"]),
                completion_token=_new_uuid_v7(),
                logical_name=str(spec["result_logical_name"]),
                media_type=str(spec["media_type"]),
                provenance=dict(spec["provenance"]),
            )

        threading.Thread(target=monitor, daemon=True).start()

    def _launch_command(self) -> tuple[str, ...]:
        if self.launch_spec is None:
            raise RuntimeError("launch spec is absent")
        spec = self.launch_spec
        command: tuple[str, ...] = (
            "python",
            "-m",
            "nexa.workloads.cpu_entrypoint",
            "--input",
            str(spec["input_path"]),
            "--output",
            str(spec["output_path"]),
            "--iterations",
            str(spec["iterations"]),
            "--seed",
            str(spec["seed"]),
            "--modulus",
            str(spec["modulus"]),
            "--spec-checksum",
            str(spec["spec_checksum"]),
        )
        if spec["schema_version"] != 2:
            return command
        checkpoint = spec["checkpoint"]
        restore = spec["restore"]
        assert isinstance(checkpoint, dict)
        command += ("--state-output", str(checkpoint["state_path"]))
        if isinstance(restore, dict):
            command += ("--resume-state", str(restore["path"]))
        return command

    def _drain_log(self, stream) -> None:  # type: ignore[no-untyped-def]
        while True:
            chunk = os.read(stream.fileno(), 64 * 1024)
            if not chunk:
                return
            with self._lock:
                self._log_bytes += len(chunk)
                if self._log_bytes > self.log_limit_bytes:
                    self._log_overflow.set()

    def stop_workload(self, *, now: float, grace_seconds: float = 5) -> int | None:
        del now
        reason = self._stop_reason or "FAILURE"
        if self._supervisor_socket is not None and self._workload_started:
            grace = min(grace_seconds, float(self.stop_grace_seconds))
            self._supervisor_socket.sendall(f"TERM {grace:.9f}\n".encode("ascii"))
            if not self._supervisor_exited.wait(timeout=grace + 1.5):
                raise RuntimeError("workload supervisor did not confirm stop")
            exit_code = self._supervisor_exit_code
            normalized_exit_code = _normalize_exit_code(exit_code)
            self._emit(
                "STOPPED",
                {
                    "reason": reason,
                    "exit_code": normalized_exit_code,
                    "stopped_monotonic_ns": int(self.clock() * 1_000_000_000),
                },
            )
            self.state = RunnerState.STOPPED
            self._persist()
            return normalized_exit_code
        if self.workload is None or self.workload_process_group is None:
            self._emit(
                "STOPPED",
                {
                    "reason": reason,
                    "exit_code": -1,
                    "stopped_monotonic_ns": int(self.clock() * 1_000_000_000),
                },
            )
            self.state = RunnerState.STOPPED
            self._persist()
            return None
        pgid = self.workload_process_group
        if self._workload_control_fd is not None:
            with suppress(BrokenPipeError, OSError):
                os.write(self._workload_control_fd, b"TERM\n")
        else:
            _signal_group(pgid, signal.SIGTERM)
        deadline = self.clock() + min(grace_seconds, self.stop_grace_seconds)
        while _process_group_exists(pgid) and self.clock() < deadline:
            time.sleep(0.01)
        if _process_group_exists(pgid):
            if self._workload_control_fd is not None:
                with suppress(BrokenPipeError, OSError):
                    os.write(self._workload_control_fd, b"KILL\n")
            else:
                _signal_group(pgid, signal.SIGKILL)
            kill_deadline = self.clock() + 1.0
            while _process_group_exists(pgid) and self.clock() < kill_deadline:
                time.sleep(0.01)
        if _process_group_exists(pgid):
            raise RuntimeError("workload process group did not stop")
        with suppress(subprocess.TimeoutExpired):
            self.workload.wait(timeout=0.1)
        exit_code = self.workload.poll()
        if self._workload_control_fd is not None:
            os.close(self._workload_control_fd)
            self._workload_control_fd = None
        normalized_exit_code = _normalize_exit_code(exit_code)
        self._emit(
            "STOPPED",
            {
                "reason": reason,
                "exit_code": normalized_exit_code,
                "stopped_monotonic_ns": int(self.clock() * 1_000_000_000),
            },
        )
        self.state = RunnerState.STOPPED
        self._persist()
        return normalized_exit_code

    def enforce_deadlines(self, *, now: float | None = None) -> None:
        instant = self.clock() if now is None else now
        if self.state == RunnerState.STOPPED:
            return
        reason: str | None = None
        if (
            self.runtime_started_at is None
            and instant >= self.created_at + self.startup_limit_seconds
        ):
            reason = "RUNTIME_LIMIT"
        elif self.authority_deadline is not None and instant >= self.authority_deadline:
            reason = "LEASE_DEADLINE"
        elif (
            self.runtime_started_at is not None
            and instant >= self.runtime_started_at + self.runtime_limit_seconds
        ):
            reason = "RUNTIME_LIMIT"
        elif self._log_overflow.is_set():
            reason = "FAILURE"
        if reason is not None:
            self.request_stop(reason, now=instant)
            self.stop_workload(now=instant, grace_seconds=self.stop_grace_seconds)

    def start_watchdog(self, *, interval_seconds: float = 0.01) -> None:
        if self._watchdog_thread is not None:
            return

        def watch() -> None:
            while not self._watchdog_stop.wait(interval_seconds):
                try:
                    self.enforce_deadlines()
                except (OSError, RuntimeError):
                    self.state = RunnerState.STOPPING
                    self._persist()
                    return
                if self.state == RunnerState.STOPPED:
                    return

        self._watchdog_thread = threading.Thread(target=watch, daemon=True)
        self._watchdog_thread.start()

    def close(self) -> None:
        self._watchdog_stop.set()
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=1)
        if self._supervisor_socket is not None:
            with suppress(OSError):
                self._supervisor_socket.close()
            self._supervisor_socket = None
        if self._supervisor_thread is not None:
            self._supervisor_thread.join(timeout=1)

    def mark_stopped(self) -> None:
        self.state = RunnerState.STOPPED
        self._persist()

    def _persist(self) -> None:
        if self.state_path is None:
            return
        with self._lock:
            payload = {
                "version": 1,
                "created_at": self.created_at,
                "state": self.state.value,
                "authority_deadline": self.authority_deadline,
                "runtime_started_at": self.runtime_started_at,
                "control_sequences": self._control_sequences.snapshot(),
                "pending_messages": self._pending_messages,
                "next_message_sequence": self._next_message_sequence,
                "progress_sequence": self._progress_sequence,
                "last_progress_payload": self._last_progress_payload,
                "last_progress_envelope": self._last_progress_envelope,
                "result": self._result,
                "checkpoint": self._checkpoint,
                "stop_reason": self._stop_reason,
            }
            _atomic_write(self.state_path, _canonical_json(payload), mode=0o600)

    def _load_state(self) -> None:
        assert self.state_path is not None
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("unsupported state")
            self.created_at = float(payload["created_at"])
            self.state = RunnerState(payload["state"])
            authority = payload.get("authority_deadline")
            runtime = payload.get("runtime_started_at")
            self.authority_deadline = float(authority) if authority is not None else None
            self.runtime_started_at = float(runtime) if runtime is not None else None
            self._control_sequences = SequenceState.from_snapshot(payload["control_sequences"])
            pending = payload["pending_messages"]
            if not isinstance(pending, list) or len(pending) > 1024:
                raise ValueError("pending messages are invalid")
            for message in pending:
                validate_envelope(message)
                encode_frame(message)
            self._pending_messages = pending
            self._next_message_sequence = int(payload["next_message_sequence"])
            self._progress_sequence = int(payload["progress_sequence"])
            self._last_progress_payload = payload.get("last_progress_payload")
            self._last_progress_envelope = payload.get("last_progress_envelope")
            self._result = payload.get("result")
            checkpoint = payload.get("checkpoint")
            if checkpoint is not None and not isinstance(checkpoint, dict):
                raise ValueError("checkpoint state is invalid")
            self._checkpoint = checkpoint
            stop_reason = payload.get("stop_reason")
            self._stop_reason = str(stop_reason) if stop_reason is not None else None
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError("durable runner state is invalid") from exc


def serve_control(
    runner: RunnerSupervisor,
    socket_path: str,
    *,
    supervisor_socket_path: str | None = None,
    timeout_seconds: int = 30,
) -> None:
    """Serve replayable private worker connections until the runner stops."""
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(FileNotFoundError):
        path.unlink()
    runner.start_watchdog()
    registration_thread: threading.Thread | None = None
    if supervisor_socket_path is not None:
        registration_thread = threading.Thread(
            target=_serve_supervisor_registration,
            args=(runner, supervisor_socket_path),
            daemon=True,
        )
        registration_thread.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            previous_umask = os.umask(0o177)
            try:
                server.bind(str(path))
            finally:
                os.umask(previous_umask)
            server.listen(4)
            server.settimeout(0.1)
            accepted_at = runner.clock()
            while runner.state != RunnerState.STOPPED:
                if runner.fatal_error:
                    raise RuntimeError("trusted runner entered fail-closed termination")
                if (
                    runner.clock() - accepted_at >= timeout_seconds
                    and runner.state == RunnerState.WAITING_AUTHORITY
                ):
                    runner.enforce_deadlines(now=runner.created_at + runner.startup_limit_seconds)
                    break
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                accepted_at = runner.clock()
                _serve_connection(runner, connection)
    finally:
        runner.close()
        if registration_thread is not None:
            registration_thread.join(timeout=1)
        with suppress(FileNotFoundError):
            path.unlink()


def _serve_supervisor_registration(runner: RunnerSupervisor, socket_path: str) -> None:
    path = Path(socket_path)
    connection: socket.socket | None = None
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(FileNotFoundError):
        path.unlink()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            os.chmod(path, 0o620)
            server.listen(1)
            server.settimeout(0.1)
            while runner.state != RunnerState.STOPPED:
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                supervisor_pid, _, _ = validate_supervisor_peer(connection)
                runner.attach_supervisor_connection(connection, supervisor_pid=supervisor_pid)
                path.unlink()
                return
    except (OSError, RuntimeError, ValueError):
        instant = runner.clock()
        runner.request_stop("FAILURE", now=instant)
        try:
            runner.stop_workload(now=instant, grace_seconds=0)
        except (OSError, RuntimeError):
            runner.fail_closed()
        finally:
            if connection is not None:
                connection.close()
    finally:
        with suppress(FileNotFoundError):
            path.unlink()


def _serve_connection(runner: RunnerSupervisor, connection: socket.socket) -> None:
    with connection:
        connection.settimeout(0.1)
        decoder = FrameDecoder()
        sent: set[int] = set()
        while True:
            if runner.fatal_error:
                raise RuntimeError("trusted runner entered fail-closed termination")
            for message in runner.pending_messages:
                sequence = int(message["message_sequence"])
                if sequence not in sent:
                    connection.sendall(encode_frame(message))
                    sent.add(sequence)
            if runner.state == RunnerState.STOPPED:
                return
            try:
                data = connection.recv(64 * 1024)
            except TimeoutError:
                continue
            if not data:
                return
            for envelope in decoder.feed(data):
                if "ack_sequence" in envelope:
                    runner.acknowledge_message(envelope)
                    continue
                sequence = envelope.get("control_sequence")
                try:
                    code = runner.apply_control(envelope)
                except (ProtocolError, ValueError):
                    code = "INVALID"
                if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > 0:
                    ack = {
                        "schema_version": 1,
                        "ack_sequence": sequence,
                        "accepted": code in {"ACCEPTED", "DUPLICATE"},
                        "code": code,
                    }
                    connection.sendall(encode_frame(ack))


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def validate_supervisor_peer(connection: socket.socket) -> tuple[int, int, int]:
    option = getattr(socket, "SO_PEERCRED", 17)
    raw = connection.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    if uid != 1001:
        raise PermissionError("workload supervisor must use UID 1001")
    if gid != 1000:
        raise PermissionError("workload supervisor must use GID 1000")
    return pid, uid, gid


def _checksum_json(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _match_bindings(descriptors: list[object], bindings: object, *, label: str) -> None:
    assert isinstance(bindings, list)
    if len(bindings) != len(descriptors):
        raise ProtocolError("binding count does not match staged descriptors")
    artifact_ids: set[str] = set()
    logical_names: set[str] = set()
    staging_names: set[str] = set()
    for descriptor, binding in zip(descriptors, bindings, strict=True):
        assert isinstance(descriptor, dict)
        assert isinstance(binding, dict)
        for field in (
            "staging_name",
            "logical_name",
            "kind",
            "media_type",
            "size_bytes",
            "checksum",
        ):
            if binding[field] != descriptor[field]:
                raise ProtocolError("binding does not match staged descriptor")
        for field, seen in (
            ("artifact_id", artifact_ids),
            ("logical_name", logical_names),
            ("staging_name", staging_names),
        ):
            value = str(binding[field])
            if value in seen:
                raise ProtocolError(f"duplicate {label} {field}")
            seen.add(value)


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            # Set the mode on the open descriptor, never by a re-resolved path.
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _uuid_v7(value: str, name: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ProtocolError(f"{name} must be a canonical UUIDv7") from exc
    if parsed.version != 7 or str(parsed) != value:
        raise ProtocolError(f"{name} must be a canonical UUIDv7")


def _new_uuid_v7() -> str:
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    random_bytes = bytearray(os.urandom(10))
    raw = bytearray(timestamp_ms.to_bytes(6, "big") + random_bytes)
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def _validate_launch_spec(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProtocolError("launch spec fields are invalid")
    version = value.get("schema_version")
    # Version 2 adds checkpoint staging and optional restore; version 1 stays exact.
    required = _LAUNCH_SPEC_FIELDS | ({"checkpoint", "restore"} if version == 2 else set())
    if set(value) != required:
        raise ProtocolError("launch spec fields are invalid")
    if version not in {1, 2} or type(version) is not int or value["adapter_id"] != "cpu.iterative":
        raise ProtocolError("launch spec adapter is invalid")
    _uuid_v7(value["startup_nonce"], "startup_nonce")
    if value["input_path"] != "/input/input.json" or value["output_path"] != "/output/result.json":
        raise ProtocolError("launch spec paths are invalid")
    if value["result_logical_name"] != "result.json":
        raise ProtocolError("launch spec logical name is invalid")
    if value["media_type"] != "application/vnd.nexa.cpu-iterative-result+json":
        raise ProtocolError("launch spec media type is invalid")
    for field, minimum, maximum in (
        ("iterations", 1, 1_000_000_000),
        ("seed", 0, 2_147_483_647),
        ("modulus", 2, 2_147_483_647),
    ):
        item = value[field]
        if not isinstance(item, int) or isinstance(item, bool) or not minimum <= item <= maximum:
            raise ProtocolError(f"launch spec {field} is invalid")
    checksum = value["spec_checksum"]
    if not isinstance(checksum, str) or not checksum.startswith("sha256:") or len(checksum) != 71:
        raise ProtocolError("launch spec checksum is invalid")
    _validate_result_provenance(value["provenance"])
    if version == 2:
        _validate_checkpoint_launch(value)
    return value


def _validate_checkpoint_launch(value: dict[str, object]) -> None:
    provenance = value["provenance"]
    assert isinstance(provenance, dict)
    if value["spec_checksum"] != provenance["spec_checksum"]:
        raise ProtocolError("launch spec checksum does not match provenance")
    checkpoint = value["checkpoint"]
    if (
        not isinstance(checkpoint, dict)
        or set(checkpoint) != {"state_path", "compatibility"}
        or checkpoint["state_path"] != CHECKPOINT_STATE_PATH
    ):
        raise ProtocolError("launch spec checkpoint is invalid")
    compatibility = checkpoint["compatibility"]
    if (
        not isinstance(compatibility, dict)
        or set(compatibility) != _COMPATIBILITY_FIELDS
        or compatibility["architecture"] not in {"linux/amd64", "linux/arm64"}
        or compatibility["device_type"] != "CPU"
        or compatibility["framework"] != "PYTHON"
        or not isinstance(compatibility["framework_version"], str)
        or not compatibility["framework_version"]
        or any(
            compatibility[field] is not None
            for field in ("cuda_version", "minimum_driver_version", "gpu_compute_capability")
        )
        or compatibility["checkpointable"] is not True
        or type(compatibility["restart_safe"]) is not bool
    ):
        raise ProtocolError("launch spec compatibility is invalid")
    restore = value["restore"]
    if restore is None:
        return
    if (
        not isinstance(restore, dict)
        or set(restore) != _RESTORE_FIELDS
        or restore["path"] != RESTORE_STATE_PATH
    ):
        raise ProtocolError("launch spec restore is invalid")
    _uuid_v7(restore["checkpoint_id"], "checkpoint_id")
    for field, minimum, maximum in (
        ("checkpoint_sequence", 1, 2**53 - 1),
        ("step", 0, int(value["iterations"])),
        ("accumulator", 0, int(value["modulus"]) - 1),
    ):
        item = restore[field]
        if type(item) is not int or not minimum <= item <= maximum:
            raise ProtocolError(f"launch spec restore {field} is invalid")
    checksum = restore["state_checksum"]
    if (
        not isinstance(checksum, str)
        or not checksum.startswith("sha256:")
        or len(checksum) != 71
        or any(char not in "0123456789abcdef" for char in checksum[7:])
    ):
        raise ProtocolError("launch spec restore checksum is invalid")


def _validate_result_provenance(value: object) -> dict[str, object]:
    required = {
        "tenant_id",
        "job_id",
        "session_id",
        "attempt_id",
        "job_fence",
        "input_checksum",
        "spec_checksum",
        "template_id",
        "template_version",
        "adapter_id",
        "adapter_version",
        "image_digest",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ProtocolError("result provenance fields are invalid")
    for field in ("tenant_id", "job_id", "session_id", "attempt_id"):
        _uuid_v7(value[field], field)
    if (
        not isinstance(value["job_fence"], int)
        or isinstance(value["job_fence"], bool)
        or value["job_fence"] < 1
    ):
        raise ProtocolError("result provenance job_fence is invalid")
    for field in ("input_checksum", "spec_checksum", "image_digest"):
        checksum = value[field]
        if (
            not isinstance(checksum, str)
            or not checksum.startswith("sha256:")
            or len(checksum) != 71
            or any(char not in "0123456789abcdef" for char in checksum[7:])
        ):
            raise ProtocolError(f"result provenance {field} is invalid")
    if value["template_id"] != "cpu-iterative" or value["template_version"] != 1:
        raise ProtocolError("result provenance template is invalid")
    if value["adapter_id"] != "cpu.iterative" or value["adapter_version"] != "1.0.0":
        raise ProtocolError("result provenance adapter is invalid")
    return value


def _validate_staged_descriptor(descriptor: dict[str, object]) -> None:
    if set(descriptor) != {
        "staging_name",
        "logical_name",
        "kind",
        "media_type",
        "size_bytes",
        "checksum",
    }:
        raise ProtocolError("staged descriptor fields are invalid")
    staging = descriptor["staging_name"]
    logical = descriptor["logical_name"]
    if not isinstance(staging, str) or "/" in staging or not staging:
        raise ProtocolError("staging name is invalid")
    if not isinstance(logical, str) or "/" in logical or not logical:
        raise ProtocolError("logical name is invalid")
    if descriptor["kind"] != "RESULT_FILE":
        raise ProtocolError("result descriptor kind is invalid")
    if not isinstance(descriptor["media_type"], str) or not descriptor["media_type"]:
        raise ProtocolError("result descriptor media type is invalid")
    if not isinstance(descriptor["size_bytes"], int) or descriptor["size_bytes"] < 0:
        raise ProtocolError("result descriptor size is invalid")


def _signal_group(process_group: int, signal_number: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process_group, signal_number)


def _normalize_exit_code(exit_code: int | None) -> int:
    if exit_code is None:
        return -1
    if exit_code < 0:
        return min(255, 128 + abs(exit_code))
    return min(255, exit_code)


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Nexa trusted runner")
    parser.add_argument(
        "--control-socket",
        default=os.environ.get("NEXA_CONTROL_SOCKET", "/run/nexa/control.sock"),
    )
    parser.add_argument(
        "--state-path",
        default=os.environ.get("NEXA_RUNNER_STATE", "/var/lib/nexa/runner-state.json"),
    )
    parser.add_argument("--runtime-limit", type=int, default=300)
    parser.add_argument("--log-limit", type=int, default=1024 * 1024)
    parser.add_argument(
        "--supervisor-socket",
        default=os.environ.get("NEXA_SUPERVISOR_SOCKET", "/run/nexa/supervisor-register.sock"),
    )
    args = parser.parse_args()
    launch_spec_path = os.environ.get("NEXA_LAUNCH_SPEC")
    launch_spec = None
    if launch_spec_path is not None:
        try:
            launch_spec = json.loads(Path(launch_spec_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 78
    runner = RunnerSupervisor(
        startup_limit_seconds=30,
        runtime_limit_seconds=args.runtime_limit,
        stop_grace_seconds=5,
        state_path=args.state_path,
        log_limit_bytes=args.log_limit,
        launch_spec=launch_spec,
    )
    try:
        runner.drop_runner_identity()
        serve_control(
            runner,
            args.control_socket,
            supervisor_socket_path=args.supervisor_socket if launch_spec is not None else None,
        )
    except (OSError, TimeoutError, RuntimeError):
        return 124
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
