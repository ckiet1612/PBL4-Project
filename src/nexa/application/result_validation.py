"""Closed per-adapter result manifest checks before a fenced Result can be recognized."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime
from uuid import UUID

import rfc8785

from nexa.application.errors import ApplicationError
from nexa.application.json_codec import JsonRequestError, json_wire_value
from nexa.domain import workload_adapters
from nexa.workloads import adapter_launch, chunk_manifest, inference_state, training_state

_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$")
_MANIFEST_FIELDS = {
    "kind",
    "schema_version",
    "result_id",
    "created_at",
    "provenance",
    "status",
    "files",
    "metrics",
    "manifest_checksum",
}
_FILE_FIELDS = {"artifact_id", "logical_name", "media_type", "size_bytes", "checksum"}
_CHUNK_REFERENCE = {"artifact_id", "checksum"}
_UUID7 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
# Closed scalar metrics of one finished batch-inference run (derived from summary.json).
INFERENCE_METRICS = (
    "item_count",
    "chunk_count",
    "chunk_size",
    "batch_size",
    "output_format",
    "model_checksum",
)
# Fixed-architecture model.safetensors is about 0.5 MiB; the worker reader bound is tighter.
_MODEL_MAX_BYTES = 1024 * 1024


def _invalid(message: str) -> None:
    raise ApplicationError(code="validation_failed", status=422, message=message)


def _check_identity(
    manifest: dict, *, expected_result_id: UUID, provenance: dict, chunked: bool = False
) -> None:
    fields = _MANIFEST_FIELDS | {"chunk_output_manifest"} if chunked else _MANIFEST_FIELDS
    if not isinstance(manifest, dict) or set(manifest) != fields:
        _invalid("Result manifest fields are invalid")
    if (
        manifest["kind"] != "RESULT"
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["status"] != "SUCCEEDED"
        or manifest["result_id"] != str(expected_result_id)
        or manifest["provenance"] != provenance
    ):
        _invalid("Result identity or provenance is invalid")
    timestamp = manifest["created_at"]
    if not isinstance(timestamp, str) or not _TIMESTAMP.fullmatch(timestamp):
        _invalid("Result timestamp is invalid")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        _invalid("Result timestamp is invalid")


def _check_bytes(manifest: dict, *, raw: bytes, artifact: dict, files: list[dict]) -> None:
    try:
        if any(UUID(output["artifact_id"]).version != 7 for output in files):
            _invalid("Result file artifact ID is invalid")
        canonical_without_checksum = rfc8785.dumps(
            {key: value for key, value in manifest.items() if key != "manifest_checksum"}
        )
        canonical = rfc8785.dumps(manifest)
    except (TypeError, ValueError, KeyError, rfc8785.FloatDomainError, rfc8785.IntegerDomainError):
        _invalid("Result manifest is not canonical JSON")
    if (
        manifest["manifest_checksum"]
        != "sha256:" + hashlib.sha256(canonical_without_checksum).hexdigest()
        or raw != canonical
        or artifact["size_bytes"] != len(raw)
        or artifact["checksum"] != "sha256:" + hashlib.sha256(raw).hexdigest()
        or artifact["kind"] != "RESULT_MANIFEST"
        or artifact["media_type"] != "application/json"
    ):
        _invalid("Result manifest bytes or checksum mismatch")


def validate_cpu_result_manifest(
    manifest: dict,
    *,
    raw: bytes,
    artifact: dict,
    expected_result_id: UUID,
    provenance: dict,
) -> dict:
    """Return the sole CPU file binding after checking canonical bytes and origin."""
    _check_identity(manifest, expected_result_id=expected_result_id, provenance=provenance)
    metrics = manifest["metrics"]
    if (
        not isinstance(metrics, dict)
        or len(metrics) > 128
        or any(
            not isinstance(key, str)
            or not isinstance(value, (str, bool, int, float, type(None)))
            or (isinstance(value, float) and not math.isfinite(value))
            for key, value in metrics.items()
        )
    ):
        _invalid("Result metrics are invalid")
    files = manifest["files"]
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        _invalid("CPU result requires exactly one file binding")
    output = files[0]
    if (
        set(output) != _FILE_FIELDS
        or output["logical_name"] != "result.json"
        or output["media_type"] != "application/vnd.nexa.cpu-iterative-result+json"
        or type(output["size_bytes"]) is not int
        or not 0 <= output["size_bytes"] <= 10 * 1024**3
        or not isinstance(output["checksum"], str)
        or not _CHECKSUM.fullmatch(output["checksum"])
    ):
        _invalid("CPU result file binding is invalid")
    _check_bytes(manifest, raw=raw, artifact=artifact, files=[output])
    return output


def _finite(value: object, low: float, high: float) -> bool:
    return type(value) is float and math.isfinite(value) and low <= value <= high


def validate_training_result_manifest(
    manifest: dict,
    *,
    raw: bytes,
    artifact: dict,
    expected_result_id: UUID,
    provenance: dict,
    parameters: dict,
) -> list[dict]:
    """Return the model then metrics bindings of one finished training run.

    Tensor bytes stay opaque: the runner validated model.safetensors and the full
    metrics.json before staging, and the server binds both by size and checksum.
    """
    adapter = workload_adapters.PYTORCH_CIFAR10
    _check_identity(manifest, expected_result_id=expected_result_id, provenance=provenance)
    files = manifest["files"]
    bounds = {
        "model.safetensors": _MODEL_MAX_BYTES,
        "metrics.json": adapter_launch.MAX_METRICS_BYTES,
    }
    if (
        not isinstance(files, list)
        or not all(isinstance(output, dict) and set(output) == _FILE_FIELDS for output in files)
        or [(output["logical_name"], output["media_type"]) for output in files]
        != [(rule.logical_name, rule.media_type) for rule in adapter.result_files]
        or any(
            type(output["size_bytes"]) is not int
            or not 1 <= output["size_bytes"] <= bounds[output["logical_name"]]
            or not isinstance(output["checksum"], str)
            or not _CHECKSUM.fullmatch(output["checksum"])
            for output in files
        )
    ):
        _invalid("Training result file bindings are invalid")
    metrics = manifest["metrics"]
    try:
        adapter_launch.validate_parameters(adapter, parameters)
        per_epoch = training_state.batches_per_epoch(
            parameters["subset_size"], parameters["batch_size"]
        )
    except (adapter_launch.LaunchSpecError, KeyError, TypeError):
        _invalid("Training result parameters are unavailable")
    if (
        not isinstance(metrics, dict)
        or set(metrics) != set(adapter_launch.MANIFEST_METRICS)
        or metrics["model_checksum"] != files[0]["checksum"]
        or any(
            type(metrics[name]) is not int or metrics[name] != expected
            for name, expected in (
                ("epochs", parameters["epochs"]),
                ("steps", parameters["epochs"] * per_epoch),
                ("subset_size", parameters["subset_size"]),
                ("batch_size", parameters["batch_size"]),
            )
        )
        or type(metrics["eval_items"]) is not int
        or not 1 <= metrics["eval_items"] <= training_state.MAX_SUBSET
        or not all(
            _finite(metrics[name], 0.0, 1e6)
            for name in ("train_loss", "eval_loss", "model_l2_norm", "optimizer_momentum_l2_norm")
        )
        or not _finite(metrics["eval_accuracy"], 0.0, 1.0)
    ):
        _invalid("Training result metrics are invalid")
    _check_bytes(manifest, raw=raw, artifact=artifact, files=files)
    return files


def validate_inference_result_manifest(
    manifest: dict,
    *,
    raw: bytes,
    artifact: dict,
    expected_result_id: UUID,
    provenance: dict,
    parameters: dict,
    model_checksum: str | None,
) -> list[dict]:
    """Return the summary.json binding; chunk coverage is checked from the referenced bytes."""
    adapter = workload_adapters.BATCH_INFERENCE
    _check_identity(
        manifest, expected_result_id=expected_result_id, provenance=provenance, chunked=True
    )
    reference = manifest["chunk_output_manifest"]
    if (
        not isinstance(reference, dict)
        or set(reference) != _CHUNK_REFERENCE
        or not isinstance(reference["artifact_id"], str)
        or not _UUID7.fullmatch(reference["artifact_id"])
        or not isinstance(reference["checksum"], str)
        or not _CHECKSUM.fullmatch(reference["checksum"])
    ):
        _invalid("Inference result chunk reference is invalid")
    files = manifest["files"]
    if (
        not isinstance(files, list)
        or not all(isinstance(output, dict) and set(output) == _FILE_FIELDS for output in files)
        or [(output["logical_name"], output["media_type"]) for output in files]
        != [(rule.logical_name, rule.media_type) for rule in adapter.result_files]
        or any(
            type(output["size_bytes"]) is not int
            or not 1 <= output["size_bytes"] <= inference_state.MAX_DOCUMENT_BYTES
            or not isinstance(output["checksum"], str)
            or not _CHECKSUM.fullmatch(output["checksum"])
            for output in files
        )
    ):
        _invalid("Inference result file bindings are invalid")
    try:
        adapter_launch.validate_parameters(adapter, parameters)
    except (adapter_launch.LaunchSpecError, KeyError, TypeError):
        _invalid("Inference result parameters are unavailable")
    metrics = manifest["metrics"]
    if (
        not isinstance(metrics, dict)
        or set(metrics) != set(INFERENCE_METRICS)
        or metrics["model_checksum"] != model_checksum
        or any(metrics[name] != parameters[name] for name in adapter_launch.INFERENCE_PARAMETERS)
        or type(metrics["item_count"]) is not int
        or not 1 <= metrics["item_count"] <= inference_state.MAX_ITEMS
        or type(metrics["chunk_count"]) is not int
        or type(metrics["chunk_size"]) is not int
        or type(metrics["batch_size"]) is not int
        or metrics["chunk_count"]
        != inference_state.chunk_count(metrics["item_count"], metrics["chunk_size"])
        or metrics["chunk_count"] > chunk_manifest.MAX_CHUNKS
    ):
        _invalid("Inference result metrics are invalid")
    _check_bytes(manifest, raw=raw, artifact=artifact, files=files)
    return files


def validate_inference_summary(
    raw: bytes, *, manifest: dict, parameters: dict, model_checksum: str | None
) -> dict:
    """The committed summary.json agrees with the result metrics and the immutable job."""
    try:
        document = inference_state.parse_summary(raw)
    except inference_state.InferenceStateError:
        _invalid("Inference summary is not the closed format")
    provenance, metrics = manifest["provenance"], manifest["metrics"]
    if (
        any(document[key] != parameters.get(key) for key in adapter_launch.INFERENCE_PARAMETERS)
        or document["input_checksum"] != provenance["input_checksum"]
        or document["spec_checksum"] != provenance["spec_checksum"]
        or document["model_checksum"] != model_checksum
        or document["item_count"] != metrics["item_count"]
        or document["chunk_count"] != metrics["chunk_count"]
    ):
        _invalid("Inference summary differs from its job or metrics")
    return document


def validate_result_manifest(
    manifest: dict,
    *,
    raw: bytes,
    artifact: dict,
    expected_result_id: UUID,
    provenance: dict,
    parameters: dict,
    model_checksum: str | None = None,
) -> list[dict]:
    """Dispatch by the adapter of the job's frozen TemplateVersion, never by the manifest."""
    options = {
        "raw": raw,
        "artifact": artifact,
        "expected_result_id": expected_result_id,
        "provenance": provenance,
    }
    if provenance["adapter_id"] == workload_adapters.CPU_ITERATIVE.adapter_id:
        return [validate_cpu_result_manifest(manifest, **options)]
    adapter = workload_adapters.descriptor(provenance["adapter_id"], provenance["adapter_version"])
    if adapter is workload_adapters.PYTORCH_CIFAR10:
        # The HTTP codec decodes numbers as exact Decimal of a binary64; compare as JSON floats.
        try:
            manifest = json_wire_value(manifest)
        except JsonRequestError:
            _invalid("Result manifest is not canonical JSON")
        return validate_training_result_manifest(manifest, parameters=parameters, **options)
    if adapter is workload_adapters.BATCH_INFERENCE:
        return validate_inference_result_manifest(
            manifest, parameters=parameters, model_checksum=model_checksum, **options
        )
    _invalid("Result adapter is not supported")
