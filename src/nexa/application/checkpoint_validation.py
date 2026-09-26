"""Closed checkpoint manifest checks shared by publish and restore selection."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any
from uuid import UUID

import rfc8785

from nexa.application.errors import ApplicationError

CHECKPOINT_MANIFEST_MAX_BYTES = 1024 * 1024
CPU_STATE_MAX_BYTES = 4096
_MAX_SAFE_INTEGER = 2**53 - 1
_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID7 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$")
_LOGICAL_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_REQUIRED = {
    "kind",
    "schema_version",
    "checkpoint_id",
    "checkpoint_sequence",
    "created_at",
    "provenance",
    "compatibility",
    "cursor",
    "state_components",
    "files",
    "manifest_checksum",
}
_PROVENANCE = {
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
# The schema makes the three CUDA fields optional; Nexa always emits them so
# publish and restore compare one exact, closed dictionary.
_COMPATIBILITY = {
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
_CURSOR_REQUIRED = {"step", "epoch", "item_cursor"}
_CURSOR_OPTIONAL = {"accumulator", "sampler_state_checksum"}
_STATE_COMPONENTS = {
    "ACCUMULATOR",
    "MODEL",
    "OPTIMIZER",
    "RNG_PYTHON",
    "RNG_NUMPY",
    "RNG_TORCH_CPU",
    "RNG_TORCH_CUDA",
    "SAMPLER",
    "INFERENCE_CURSOR",
}
_FILE_FIELDS = {"artifact_id", "logical_name", "media_type", "size_bytes", "checksum"}
# Template capability names map onto the manifest schema's framework enum.
_FRAMEWORKS = {"NEXA_CPU": "PYTHON", "PYTORCH": "PYTORCH"}


class CheckpointManifestError(ApplicationError):
    """Deterministic manifest defect carrying an allowlisted event reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(code="validation_failed", status=422, message=message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> None:
    raise CheckpointManifestError(reason_code, message)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _no_float(_value: str) -> Any:
    raise ValueError("floating point numbers are not allowed")


def _bounded_int(value: str) -> int:
    parsed = int(value)
    if abs(parsed) > _MAX_SAFE_INTEGER:
        raise ValueError("integer is outside the canonical JSON range")
    return parsed


def _no_constant(_value: str) -> Any:
    raise ValueError("non-finite numbers are not allowed")


def parse_strict_json(raw: bytes, *, limit: int, reason_code: str) -> Any:
    """Decode UTF-8 JSON without duplicate keys, floats, or unsafe integers."""
    if not isinstance(raw, bytes) or len(raw) > limit:
        _fail(reason_code, "Checkpoint document exceeds its size bound")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_float=_no_float,
            parse_int=_bounded_int,
            parse_constant=_no_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        _fail(reason_code, "Checkpoint document is not strict JSON")


def parse_checkpoint_manifest(raw: bytes) -> dict[str, Any]:
    manifest = parse_strict_json(
        raw, limit=CHECKPOINT_MANIFEST_MAX_BYTES, reason_code="CHECKPOINT_MANIFEST_INVALID"
    )
    if not isinstance(manifest, dict):
        _fail("CHECKPOINT_MANIFEST_INVALID", "Checkpoint manifest must be an object")
    return manifest


def checkpoint_provenance(
    *, job: dict, spec: dict, template: dict, session_id, input_checksum: str, attempt_id, fence
) -> dict[str, Any]:
    return {
        "tenant_id": str(job["tenant_id"]),
        "job_id": str(job["job_id"]),
        "session_id": str(session_id),
        "attempt_id": str(attempt_id),
        "job_fence": int(fence),
        "input_checksum": input_checksum,
        "spec_checksum": spec["spec_checksum"],
        "template_id": spec["template_id"],
        "template_version": int(spec["template_version"]),
        "adapter_id": template["adapter_id"],
        "adapter_version": template["adapter_version"],
        "image_digest": template["image_digest"],
    }


def expected_compatibility(template: dict, architecture: str | None) -> dict[str, Any]:
    """Derive the exact compatibility dictionary from the frozen TemplateVersion."""
    requirements = template.get("capability_requirements") or {}
    framework = _FRAMEWORKS.get(requirements.get("framework"))
    if (
        not isinstance(architecture, str)
        or architecture not in (requirements.get("architectures") or [])
        or framework is None
    ):
        _fail("CHECKPOINT_COMPATIBILITY_MISMATCH", "Checkpoint environment is unsupported")
    return {
        "architecture": architecture,
        "device_type": requirements.get("device"),
        "framework": framework,
        "framework_version": requirements.get("framework_version"),
        "cuda_version": requirements.get("cuda_runtime_min"),
        "minimum_driver_version": requirements.get("driver_min"),
        "gpu_compute_capability": requirements.get("compute_capability_min"),
        "checkpointable": bool(template["checkpointable"]),
        "restart_safe": bool(template["restart_safe"]),
    }


def _is_int(value: Any) -> bool:
    return type(value) is int


def _uuid7(value: Any) -> bool:
    return isinstance(value, str) and _UUID7.fullmatch(value) is not None


def _check_schema(manifest: dict[str, Any]) -> None:
    keys = set(manifest)
    if not keys >= _REQUIRED or not keys <= _REQUIRED | {"chunk_output_manifest"}:
        _fail("CHECKPOINT_MANIFEST_INVALID", "Checkpoint manifest fields are invalid")
    if "chunk_output_manifest" in manifest:
        # Only batch inference carries chunk outputs; that template is B16 scope.
        _fail("CHECKPOINT_MANIFEST_INVALID", "Checkpoint manifest must omit chunk outputs")
    if (
        manifest["kind"] != "CHECKPOINT"
        or not _is_int(manifest["schema_version"])
        or manifest["schema_version"] != 1
        or not _uuid7(manifest["checkpoint_id"])
        or not _is_int(manifest["checkpoint_sequence"])
        or manifest["checkpoint_sequence"] < 1
        or not isinstance(manifest["manifest_checksum"], str)
        or not _CHECKSUM.fullmatch(manifest["manifest_checksum"])
    ):
        _fail("CHECKPOINT_MANIFEST_INVALID", "Checkpoint manifest identity is invalid")
    created_at = manifest["created_at"]
    if not isinstance(created_at, str) or not _TIMESTAMP.fullmatch(created_at):
        _fail("CHECKPOINT_MANIFEST_INVALID", "Checkpoint timestamp is invalid")
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        _fail("CHECKPOINT_MANIFEST_INVALID", "Checkpoint timestamp is invalid")
    provenance = manifest["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE:
        _fail("CHECKPOINT_PROVENANCE_MISMATCH", "Checkpoint provenance fields are invalid")
    compatibility = manifest["compatibility"]
    if not isinstance(compatibility, dict) or set(compatibility) != _COMPATIBILITY:
        _fail("CHECKPOINT_COMPATIBILITY_MISMATCH", "Checkpoint compatibility fields are invalid")
    cursor = manifest["cursor"]
    if (
        not isinstance(cursor, dict)
        or not set(cursor) >= _CURSOR_REQUIRED
        or not set(cursor) <= _CURSOR_REQUIRED | _CURSOR_OPTIONAL
        or any(not _is_int(cursor[key]) or cursor[key] < 0 for key in _CURSOR_REQUIRED)
    ):
        _fail("CHECKPOINT_CURSOR_INVALID", "Checkpoint cursor is invalid")
    components = manifest["state_components"]
    if (
        not isinstance(components, list)
        or not components
        or any(not isinstance(item, str) or item not in _STATE_COMPONENTS for item in components)
        or len(set(components)) != len(components)
    ):
        _fail("CHECKPOINT_MANIFEST_INVALID", "Checkpoint state components are invalid")
    files = manifest["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 64:
        _fail("CHECKPOINT_FILES_INVALID", "Checkpoint requires 1..64 file entries")
    names, identifiers = set(), set()
    for entry in files:
        if (
            not isinstance(entry, dict)
            or set(entry) != _FILE_FIELDS
            or not _uuid7(entry["artifact_id"])
            or not isinstance(entry["logical_name"], str)
            or not _LOGICAL_NAME.fullmatch(entry["logical_name"])
            or not isinstance(entry["media_type"], str)
            or not 1 <= len(entry["media_type"]) <= 127
            or not _is_int(entry["size_bytes"])
            or not 0 <= entry["size_bytes"] <= 10 * 1024**3
            or not isinstance(entry["checksum"], str)
            or not _CHECKSUM.fullmatch(entry["checksum"])
        ):
            _fail("CHECKPOINT_FILES_INVALID", "Checkpoint file entry is invalid")
        if entry["logical_name"] in names or entry["artifact_id"] in identifiers:
            _fail("CHECKPOINT_FILES_INVALID", "Checkpoint file entries must be unique")
        names.add(entry["logical_name"])
        identifiers.add(entry["artifact_id"])


def _check_cpu(manifest: dict[str, Any], parameters: dict[str, Any]) -> None:
    iterations, modulus = parameters.get("iterations"), parameters.get("modulus")
    if not _is_int(iterations) or not _is_int(modulus):
        _fail("CHECKPOINT_CURSOR_INVALID", "CPU checkpoint parameters are unavailable")
    cursor = manifest["cursor"]
    accumulator = cursor.get("accumulator")
    if (
        set(cursor) != {"step", "epoch", "item_cursor", "accumulator"}
        or cursor["step"] > iterations
        or cursor["epoch"] != 0
        or cursor["item_cursor"] != cursor["step"]
        or not _is_int(accumulator)
        or not 0 <= accumulator < modulus
    ):
        _fail("CHECKPOINT_CURSOR_INVALID", "CPU checkpoint cursor is outside the job bounds")
    if manifest["state_components"] != ["ACCUMULATOR"]:
        _fail("CHECKPOINT_MANIFEST_INVALID", "CPU checkpoint must declare only ACCUMULATOR")
    files = manifest["files"]
    if (
        len(files) != 1
        or files[0]["logical_name"] != "state.json"
        or files[0]["media_type"] != "application/json"
        or not 1 <= files[0]["size_bytes"] <= CPU_STATE_MAX_BYTES
    ):
        _fail("CHECKPOINT_FILES_INVALID", "CPU checkpoint requires one bounded state.json")


def validate_checkpoint_manifest(
    manifest: Any,
    *,
    raw: bytes,
    artifact: dict,
    checkpoint_id: UUID,
    sequence: int,
    provenance: dict[str, Any],
    compatibility: dict[str, Any],
    parameters: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return file entries after checking canonical bytes, identity and origin.

    Every failure is deterministic for the given bytes and database facts, so
    publish may durably reject the reservation and restore may mark corruption.
    """
    parsed = parse_checkpoint_manifest(raw)
    if not isinstance(manifest, dict) or manifest != parsed:
        _fail("CHECKPOINT_MANIFEST_NOT_CANONICAL", "Checkpoint request differs from its bytes")
    _check_schema(parsed)
    try:
        canonical = rfc8785.dumps(parsed)
        without_checksum = rfc8785.dumps(
            {key: value for key, value in parsed.items() if key != "manifest_checksum"}
        )
    except (TypeError, ValueError, rfc8785.FloatDomainError, rfc8785.IntegerDomainError):
        _fail("CHECKPOINT_MANIFEST_NOT_CANONICAL", "Checkpoint manifest is not canonical")
    if raw != canonical:
        _fail("CHECKPOINT_MANIFEST_NOT_CANONICAL", "Checkpoint manifest is not canonical")
    digest = "sha256:" + hashlib.sha256(without_checksum).hexdigest()
    if parsed["manifest_checksum"] != digest:
        _fail("CHECKPOINT_MANIFEST_CHECKSUM_MISMATCH", "Checkpoint manifest checksum mismatch")
    if (
        artifact.get("kind") != "CHECKPOINT_MANIFEST"
        or artifact.get("media_type") != "application/json"
        or artifact.get("size_bytes") != len(raw)
        or artifact.get("checksum") != "sha256:" + hashlib.sha256(raw).hexdigest()
    ):
        _fail("CHECKPOINT_MANIFEST_CHECKSUM_MISMATCH", "Checkpoint manifest artifact mismatch")
    if parsed["checkpoint_id"] != str(checkpoint_id) or parsed["checkpoint_sequence"] != sequence:
        _fail("CHECKPOINT_IDENTITY_MISMATCH", "Checkpoint identity differs from its reservation")
    for key in sorted(_PROVENANCE):
        if parsed["provenance"][key] != provenance.get(key) or type(
            parsed["provenance"][key]
        ) is not type(provenance.get(key)):
            _fail("CHECKPOINT_PROVENANCE_MISMATCH", "Checkpoint provenance mismatch")
    for key in sorted(_COMPATIBILITY):
        if parsed["compatibility"][key] != compatibility.get(key) or type(
            parsed["compatibility"][key]
        ) is not type(compatibility.get(key)):
            _fail("CHECKPOINT_COMPATIBILITY_MISMATCH", "Checkpoint compatibility mismatch")
    # The adapter owns the checkpoint format; template IDs only name catalog rows.
    if parsed["provenance"]["adapter_id"] != "cpu.iterative":
        _fail("CHECKPOINT_TEMPLATE_UNSUPPORTED", "Checkpoint template is not supported")
    _check_cpu(parsed, parameters)
    return parsed["files"]


def check_file_artifacts(
    entries: list[dict[str, Any]], rows: dict[str, dict], *, tenant_id
) -> list[dict]:
    """Bind every entry to an exact committed same-tenant CHECKPOINT_FILE row."""
    bound = []
    for entry in entries:
        row = rows.get(entry["artifact_id"])
        if (
            row is None
            or row["tenant_id"] != tenant_id
            or row["state"] != "COMMITTED"
            or row["kind"] != "CHECKPOINT_FILE"
            or any(row[field] != entry[field] for field in ("media_type", "size_bytes", "checksum"))
        ):
            _fail("CHECKPOINT_FILES_INVALID", "Checkpoint file binding is invalid")
        bound.append(row)
    return bound


def validate_cpu_state(raw: bytes, *, manifest: dict[str, Any]) -> dict[str, Any]:
    """Re-validate the closed CPU state file against its manifest cursor."""
    state = parse_strict_json(
        raw, limit=CPU_STATE_MAX_BYTES, reason_code="CHECKPOINT_STATE_INVALID"
    )
    provenance = manifest["provenance"]
    cursor = manifest["cursor"]
    expected = {
        "schema_version": 1,
        "step": cursor["step"],
        "accumulator": cursor["accumulator"],
        "input_checksum": provenance["input_checksum"],
        "spec_checksum": provenance["spec_checksum"],
    }
    try:
        canonical = rfc8785.dumps(state)
    except (TypeError, ValueError, rfc8785.FloatDomainError, rfc8785.IntegerDomainError):
        _fail("CHECKPOINT_STATE_INVALID", "Checkpoint state is not canonical")
    if not isinstance(state, dict) or state != expected or raw != canonical:
        _fail("CHECKPOINT_STATE_INVALID", "Checkpoint state differs from its manifest")
    return state


__all__ = [
    "CHECKPOINT_MANIFEST_MAX_BYTES",
    "CPU_STATE_MAX_BYTES",
    "CheckpointManifestError",
    "check_file_artifacts",
    "checkpoint_provenance",
    "expected_compatibility",
    "parse_checkpoint_manifest",
    "parse_strict_json",
    "validate_checkpoint_manifest",
    "validate_cpu_state",
]
