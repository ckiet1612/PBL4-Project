"""Launch spec v3 of the PyTorch adapters (stdlib only).

The worker executor writes this closed document to the control directory and the in-image
trusted runner re-validates it before it launches the adapter workload. CPU iterative keeps
its v1/v2 launch spec in ``trusted_runner``; v3 is only for adapters whose descriptor owns
several checkpoint/result files. Paths are fixed per adapter; nothing here is client input.
"""

from __future__ import annotations

import json
import math
import re
import uuid

from nexa.domain import workload_adapters

from . import chunk_manifest, inference_state, pytorch_arch, training_state
from .canonical_json import canonical_json

SCHEMA_VERSION = 3
OUTPUT_DIR = "/output"
RESTORE_DIR = "/input/restore"
# B16-R21: read-only recognized chunk files the workload carries forward.
RECOGNIZED_DIR = "/input/recognized"
TRAINING_STATE_PATH = "/output/state.safetensors"
INFERENCE_STATE_PATH = "/output/inference-state.json"
MAX_THREADS = training_state.MAX_THREADS
_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_FIELDS = {
    "schema_version",
    "adapter_id",
    "adapter_version",
    "startup_nonce",
    "parameters",
    "threads",
    "spec_checksum",
    "inputs",
    "output_dir",
    "provenance",
    "checkpoint",
    "restore",
}
# B16-R21: optional, only for a chunked adapter with recognized chunks to carry forward.
RECOGNIZED_KEY = "recognized_chunks"
PROVENANCE_FIELDS = (
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
)
COMPATIBILITY_FIELDS = (
    "architecture",
    "device_type",
    "framework",
    "framework_version",
    "cuda_version",
    "minimum_driver_version",
    "gpu_compute_capability",
    "checkpointable",
    "restart_safe",
)
TRAINING_PARAMETERS = ("epochs", "batch_size", "learning_rate", "seed", "subset_size")
INFERENCE_PARAMETERS = ("chunk_size", "batch_size", "output_format")
TRAINING_CURSOR_FIELDS = ("step", "epoch", "item_cursor", "sampler_state_checksum")
INFERENCE_CURSOR_FIELDS = ("step", "epoch", "item_cursor")
# B16-R17: at most this many unconfirmed chunk files wait in /output; the workload blocks
# until the runner has uploaded and unlinked older ones.
INFERENCE_WINDOW = 8
METRICS_FORMAT = "pytorch-cifar10-metrics-v1"
MAX_METRICS_BYTES = 64 * 1024
# Scalar metrics.json fields the result manifest repeats; the file itself stays the record.
MANIFEST_METRICS = (
    "model_checksum",
    "epochs",
    "steps",
    "subset_size",
    "batch_size",
    "eval_items",
    "train_loss",
    "eval_loss",
    "eval_accuracy",
    "model_l2_norm",
    "optimizer_momentum_l2_norm",
)
_METRICS_FIELDS = {
    "schema_version",
    "format",
    "architecture_id",
    "input_checksum",
    "spec_checksum",
    "parameter_l2_norms",
    "epoch_sample_order_digests",
    *MANIFEST_METRICS,
}


class LaunchSpecError(ValueError):
    """The launch spec is outside the closed v3 document."""


def _fail(message: str) -> LaunchSpecError:
    return LaunchSpecError(message)


def _int(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _checksum(value: object) -> bool:
    return isinstance(value, str) and _CHECKSUM.fullmatch(value) is not None


def _uuid7(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 7 and str(parsed) == value


def adapter_for(spec: object) -> workload_adapters.AdapterDescriptor:
    if not isinstance(spec, dict):
        raise _fail("launch spec is not an object")
    adapter = workload_adapters.descriptor(spec.get("adapter_id"), spec.get("adapter_version"))
    if adapter is None or adapter is workload_adapters.CPU_ITERATIVE:
        raise _fail("launch spec adapter is not a v3 adapter")
    return adapter


def state_path(adapter: workload_adapters.AdapterDescriptor) -> str:
    return INFERENCE_STATE_PATH if adapter.chunked else TRAINING_STATE_PATH


def input_paths(adapter: workload_adapters.AdapterDescriptor) -> dict[str, str]:
    paths = {"dataset": adapter.input_target_path}
    if adapter.model_target_path is not None:
        paths["model"] = adapter.model_target_path
    return paths


def restore_path(logical_name: str) -> str:
    return f"{RESTORE_DIR}/{logical_name}"


def recognized_paths(spec: dict) -> list[str]:
    """Container paths of the spec's recognized chunk files, in chunk order."""
    if not spec.get(RECOGNIZED_KEY):
        return []
    output_format = spec["parameters"]["output_format"]
    # The validated run starts at the restored cursor, or chunk 0 after a fallback.
    first = spec["restore"]["cursor"]["step"] if spec["restore"] is not None else 0
    return [
        f"{RECOGNIZED_DIR}/{inference_state.chunk_file_name(first + offset, output_format)}"
        for offset in range(len(spec.get(RECOGNIZED_KEY) or []))
    ]


def threads_for(cpu_millis: int) -> int:
    """Workload thread count; derived from the allocation, never from the host."""
    return min(MAX_THREADS, max(1, cpu_millis // 1000))


def validate_parameters(adapter: workload_adapters.AdapterDescriptor, value: object) -> dict:
    if adapter.chunked:
        if (
            not isinstance(value, dict)
            or set(value) != set(INFERENCE_PARAMETERS)
            or not _int(value["chunk_size"], 1, 100_000)
            or not _int(value["batch_size"], 1, 4096)
            or value["output_format"] not in workload_adapters.CHUNK_MEDIA_TYPES
        ):
            raise _fail("inference parameters are invalid")
        return value
    if (
        not isinstance(value, dict)
        or set(value) != set(TRAINING_PARAMETERS)
        or not _int(value["epochs"], 1, training_state.MAX_EPOCHS)
        or not _int(value["batch_size"], 1, training_state.MAX_BATCH_SIZE)
        or not _int(value["seed"], 0, training_state.MAX_SEED)
        or not _int(value["subset_size"], training_state.MIN_SUBSET, training_state.MAX_SUBSET)
        or type(value["learning_rate"]) is not float
        or not math.isfinite(value["learning_rate"])
        or not 0 < value["learning_rate"] <= 1
    ):
        raise _fail("training parameters are invalid")
    return value


def validate_provenance(adapter: workload_adapters.AdapterDescriptor, value: object) -> dict:
    if not isinstance(value, dict) or set(value) != set(PROVENANCE_FIELDS):
        raise _fail("provenance fields are invalid")
    if not all(_uuid7(value[field]) for field in ("tenant_id", "job_id", "session_id")):
        raise _fail("provenance identity is invalid")
    if not _uuid7(value["attempt_id"]) or not _int(value["job_fence"], 1, 2**63 - 1):
        raise _fail("provenance attempt is invalid")
    if not all(_checksum(value[field]) for field in ("input_checksum", "spec_checksum")):
        raise _fail("provenance checksums are invalid")
    if not _checksum(value["image_digest"]):
        raise _fail("provenance image digest is invalid")
    if (
        value["template_id"] != adapter.template_id
        or not _int(value["template_version"], 1, 2**31 - 1)
        or value["adapter_id"] != adapter.adapter_id
        or value["adapter_version"] != adapter.adapter_version
    ):
        raise _fail("provenance template or adapter is invalid")
    return value


def compatibility(architecture: str, framework_version: str, restart_safe: bool) -> dict:
    """Checkpoint compatibility of a CPU PyTorch launch; equals the server expectation."""
    return {
        "architecture": architecture,
        "device_type": "CPU",
        "framework": "PYTORCH",
        "framework_version": framework_version,
        "cuda_version": None,
        "minimum_driver_version": None,
        "gpu_compute_capability": None,
        "checkpointable": True,
        "restart_safe": restart_safe,
    }


def _validate_compatibility(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != set(COMPATIBILITY_FIELDS)
        or value["architecture"] not in {"linux/amd64", "linux/arm64"}
        or not isinstance(value["framework_version"], str)
        or not _SEMVER.fullmatch(value["framework_version"])
        or type(value["restart_safe"]) is not bool
        or value
        != compatibility(value["architecture"], value["framework_version"], value["restart_safe"])
    ):
        raise _fail("checkpoint compatibility is invalid")


def validate_training_cursor(cursor: object, parameters: dict) -> dict:
    per_epoch = training_state.batches_per_epoch(
        parameters["subset_size"], parameters["batch_size"]
    )
    epochs = parameters["epochs"]
    if (
        not isinstance(cursor, dict)
        or set(cursor) != set(TRAINING_CURSOR_FIELDS)
        or not _int(cursor["epoch"], 0, epochs)
        or not _int(cursor["step"], 0, epochs * per_epoch)
        or not _int(cursor["item_cursor"], 0, parameters["subset_size"])
        or not _checksum(cursor["sampler_state_checksum"])
    ):
        raise _fail("training cursor is invalid")
    batch_index = cursor["step"] - cursor["epoch"] * per_epoch
    item_cursor = (
        0
        if cursor["epoch"] == epochs
        else min(parameters["subset_size"], batch_index * parameters["batch_size"])
    )
    if (
        not 0 <= batch_index < per_epoch and not (cursor["epoch"] == epochs and batch_index == 0)
    ) or cursor["item_cursor"] != item_cursor:
        raise _fail("training cursor is inconsistent")
    return cursor


def validate_inference_cursor(cursor: object) -> dict:
    """Shape of a committed inference cursor; its extent is checked against the state."""
    if (
        not isinstance(cursor, dict)
        or set(cursor) != set(INFERENCE_CURSOR_FIELDS)
        or not _int(cursor["epoch"], 0, 0)
        or not _int(cursor["step"], 1, chunk_manifest.MAX_CHUNKS)
        or not _int(cursor["item_cursor"], cursor["step"], inference_state.MAX_ITEMS)
    ):
        raise _fail("inference cursor is invalid")
    return cursor


def _validate_restore(adapter, value: object, parameters: dict) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"checkpoint_id", "checkpoint_sequence", "cursor", "files"}
        or not _uuid7(value["checkpoint_id"])
        or not _int(value["checkpoint_sequence"], 1, 2**53 - 1)
    ):
        raise _fail("restore is invalid")
    names = [rule.logical_name for rule in adapter.checkpoint_files]
    if adapter.chunked:
        # B16-R18: an inference restore also carries the checkpoint's chunk-output manifest.
        validate_inference_cursor(value["cursor"])
        names.append(chunk_manifest.LOGICAL_NAME)
    else:
        validate_training_cursor(value["cursor"], parameters)
    files = value["files"]
    if (
        not isinstance(files, list)
        or [item.get("logical_name") if isinstance(item, dict) else None for item in files] != names
    ):
        raise _fail("restore files are not the adapter checkpoint files")
    for item in files:
        if (
            set(item) != {"logical_name", "path", "checksum", "size_bytes"}
            or item["path"] != restore_path(item["logical_name"])
            or not _checksum(item["checksum"])
            or not _int(item["size_bytes"], 1, 16 * 1024**2)
        ):
            raise _fail("restore file entry is invalid")


def validate_launch_spec(value: object) -> dict:
    """Validate a v3 launch spec; raises ``LaunchSpecError``."""
    if not isinstance(value, dict) or set(value) - {RECOGNIZED_KEY} != _FIELDS:
        raise _fail("launch spec fields are invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise _fail("launch spec schema version is invalid")
    adapter = adapter_for(value)
    if not _uuid7(value["startup_nonce"]):
        raise _fail("launch spec startup nonce is invalid")
    parameters = validate_parameters(adapter, value["parameters"])
    if not _int(value["threads"], 1, MAX_THREADS):
        raise _fail("launch spec thread count is invalid")
    provenance = validate_provenance(adapter, value["provenance"])
    if (
        not _checksum(value["spec_checksum"])
        or value["spec_checksum"] != provenance["spec_checksum"]
    ):
        raise _fail("launch spec checksum does not match provenance")
    if value["inputs"] != input_paths(adapter) or value["output_dir"] != OUTPUT_DIR:
        raise _fail("launch spec paths are invalid")
    checkpoint = value["checkpoint"]
    if checkpoint is not None:
        if (
            not isinstance(checkpoint, dict)
            or set(checkpoint) != {"state_path", "compatibility"}
            or checkpoint["state_path"] != state_path(adapter)
        ):
            raise _fail("launch spec checkpoint is invalid")
        _validate_compatibility(checkpoint["compatibility"])
    if value["restore"] is not None:
        if checkpoint is None:
            raise _fail("restore requires a checkpoint launch")
        _validate_restore(adapter, value["restore"], parameters)
    if RECOGNIZED_KEY in value:
        _validate_recognized(adapter, value, parameters)
    return value


def _validate_recognized(adapter, value: dict, parameters: dict) -> None:
    """The recognized chunks run contiguously from the restored cursor (or chunk 0)."""
    recognized = value[RECOGNIZED_KEY]
    if not adapter.chunked or not isinstance(recognized, list) or not recognized:
        # Absence, never an empty list, means there is nothing to carry forward.
        raise _fail("launch spec recognized chunks are invalid")
    first = value["restore"]["cursor"]["step"] if value["restore"] is not None else 0
    try:
        chunk_manifest.validate_recognized(
            recognized, first=first, chunk_size=parameters["chunk_size"]
        )
    except chunk_manifest.ChunkManifestError as exc:
        raise _fail("launch spec recognized chunks are invalid") from exc


def workload_command(spec: dict) -> tuple[str, ...]:
    """Fixed argv of the adapter workload; values come only from the validated spec."""
    adapter = adapter_for(spec)
    parameters = spec["parameters"]
    inputs = spec["inputs"]
    if adapter.chunked:
        command: tuple[str, ...] = (
            "python",
            "-m",
            "nexa.workloads.batch_inference",
            "--dataset",
            inputs["dataset"],
            "--model",
            inputs["model"],
            "--output-dir",
            spec["output_dir"],
            "--chunk-size",
            str(parameters["chunk_size"]),
            "--batch-size",
            str(parameters["batch_size"]),
            "--output-format",
            parameters["output_format"],
            "--threads",
            str(spec["threads"]),
            "--spec-checksum",
            spec["spec_checksum"],
        )
        if spec["checkpoint"] is not None:
            command += ("--window", str(INFERENCE_WINDOW))
        if spec["restore"] is not None:
            command += ("--resume-state", restore_path(adapter.checkpoint_files[0].logical_name))
        if RECOGNIZED_KEY in spec:
            command += (
                "--recognized-dir",
                RECOGNIZED_DIR,
                "--recognized-count",
                str(len(spec[RECOGNIZED_KEY])),
            )
        return command
    command = (
        "python",
        "-m",
        "nexa.workloads.pytorch_cifar10",
        "--dataset",
        inputs["dataset"],
        "--output-dir",
        spec["output_dir"],
        "--epochs",
        str(parameters["epochs"]),
        "--batch-size",
        str(parameters["batch_size"]),
        # repr round-trips the exact binary64 through argparse's float().
        "--learning-rate",
        repr(parameters["learning_rate"]),
        "--seed",
        str(parameters["seed"]),
        "--subset-size",
        str(parameters["subset_size"]),
        "--threads",
        str(spec["threads"]),
        "--spec-checksum",
        spec["spec_checksum"],
    )
    if spec["checkpoint"] is not None:
        command += ("--state-output", spec["checkpoint"]["state_path"])
    if spec["restore"] is not None:
        command += ("--resume-dir", RESTORE_DIR)
    return command


def check_training_state(
    document: dict,
    *,
    parameters: dict,
    threads: int,
    input_checksum: str,
    spec_checksum: str,
) -> None:
    """A validated training state belongs to exactly this job spec and input."""
    sampler = document["sampler"]
    if (
        document["input_checksum"] != input_checksum
        or document["spec_checksum"] != spec_checksum
        or document["threads"] != threads
        or document["optimizer"]["learning_rate"] != parameters["learning_rate"]
        or sampler["seed"] != parameters["seed"]
        or sampler["subset_size"] != parameters["subset_size"]
        or sampler["batch_size"] != parameters["batch_size"]
        or sampler["epochs"] != parameters["epochs"]
    ):
        raise _fail("training state does not belong to this job")


def check_inference_state(
    document: dict,
    *,
    parameters: dict,
    threads: int,
    input_checksum: str,
    spec_checksum: str,
    model_checksum: str,
) -> None:
    """A validated inference state or summary belongs to exactly this job, input and model."""
    if (
        any(document[name] != parameters[name] for name in INFERENCE_PARAMETERS)
        or document["threads"] != threads
        or document["input_checksum"] != input_checksum
        or document["spec_checksum"] != spec_checksum
        or document["model_checksum"] != model_checksum
    ):
        raise _fail("inference state does not belong to this job")
    if (
        inference_state.chunk_count(document["item_count"], document["chunk_size"])
        > chunk_manifest.MAX_CHUNKS
    ):
        raise _fail("inference output has more chunks than a manifest can list")


def _reject_constant(value: str) -> object:
    raise LaunchSpecError(f"invalid JSON constant {value}")


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise LaunchSpecError("duplicate metrics key")
        document[key] = value
    return document


def _finite(value: object, low: float, high: float) -> bool:
    return type(value) is float and math.isfinite(value) and low <= value <= high


def parse_training_metrics(
    raw: bytes,
    *,
    parameters: dict,
    input_checksum: str,
    spec_checksum: str,
    model_checksum: str,
) -> dict:
    """Closed canonical ``metrics.json`` of one finished training run of this job."""
    if len(raw) > MAX_METRICS_BYTES:
        raise _fail("metrics are too large")
    try:
        metrics = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail("metrics are not strict JSON") from exc
    if not isinstance(metrics, dict) or set(metrics) != _METRICS_FIELDS:
        raise _fail("metrics fields are not closed")
    try:
        canonical = canonical_json(metrics) == raw
    except (TypeError, ValueError):
        canonical = False
    per_epoch = training_state.batches_per_epoch(
        parameters["subset_size"], parameters["batch_size"]
    )
    digests = metrics["epoch_sample_order_digests"]
    norms = metrics["parameter_l2_norms"]
    if (
        not canonical
        or metrics["schema_version"] != 1
        or metrics["format"] != METRICS_FORMAT
        or metrics["architecture_id"] != pytorch_arch.ARCHITECTURE_ID
        or metrics["input_checksum"] != input_checksum
        or metrics["spec_checksum"] != spec_checksum
        or metrics["model_checksum"] != model_checksum
        or metrics["epochs"] != parameters["epochs"]
        or metrics["subset_size"] != parameters["subset_size"]
        or metrics["batch_size"] != parameters["batch_size"]
        or metrics["steps"] != parameters["epochs"] * per_epoch
        or not _int(metrics["eval_items"], 1, training_state.MAX_SUBSET)
        or not all(
            _finite(metrics[name], 0.0, 1e6)
            for name in ("train_loss", "eval_loss", "model_l2_norm", "optimizer_momentum_l2_norm")
        )
        or not _finite(metrics["eval_accuracy"], 0.0, 1.0)
        or not isinstance(digests, list)
        or len(digests) != parameters["epochs"]
        or not all(_checksum(item) for item in digests)
        or not isinstance(norms, dict)
        or set(norms) != set(pytorch_arch.PARAMETERS)
        or not all(_finite(value, 0.0, 1e6) for value in norms.values())
    ):
        raise _fail("metrics do not describe this finished run")
    return metrics


def manifest_metrics(metrics: dict) -> dict:
    return {name: metrics[name] for name in MANIFEST_METRICS}


__all__ = [
    "COMPATIBILITY_FIELDS",
    "INFERENCE_CURSOR_FIELDS",
    "INFERENCE_WINDOW",
    "MANIFEST_METRICS",
    "INFERENCE_STATE_PATH",
    "OUTPUT_DIR",
    "PROVENANCE_FIELDS",
    "RECOGNIZED_DIR",
    "RECOGNIZED_KEY",
    "RESTORE_DIR",
    "SCHEMA_VERSION",
    "TRAINING_STATE_PATH",
    "LaunchSpecError",
    "adapter_for",
    "check_inference_state",
    "check_training_state",
    "compatibility",
    "input_paths",
    "manifest_metrics",
    "parse_training_metrics",
    "recognized_paths",
    "restore_path",
    "state_path",
    "threads_for",
    "validate_launch_spec",
    "validate_parameters",
    "validate_provenance",
    "validate_inference_cursor",
    "validate_training_cursor",
    "workload_command",
]
