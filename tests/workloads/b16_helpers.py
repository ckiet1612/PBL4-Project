"""Stdlib builders for B16 training launch specs and snapshots (no torch on the host)."""

from __future__ import annotations

import hashlib
import math

from nexa.workloads import adapter_launch, pytorch_arch, safetensors_format, training_state
from nexa.workloads.canonical_json import canonical_json

INPUT_CHECKSUM = "sha256:" + "a" * 64
SPEC_CHECKSUM = "sha256:" + "b" * 64
IMAGE_DIGEST = "sha256:" + "c" * 64
PARAMETERS = {
    "epochs": 2,
    "batch_size": 64,
    "learning_rate": 0.05,
    "seed": 7,
    "subset_size": 200,
}
PROVENANCE = {
    "tenant_id": "01900000-0000-7000-8000-000000000001",
    "job_id": "01900000-0000-7000-8000-000000000002",
    "session_id": "01900000-0000-7000-8000-000000000003",
    "attempt_id": "01900000-0000-7000-8000-000000000004",
    "job_fence": 3,
    "input_checksum": INPUT_CHECKSUM,
    "spec_checksum": SPEC_CHECKSUM,
    "template_id": "pytorch-cifar10-cnn",
    "template_version": 1,
    "adapter_id": "pytorch.cifar10",
    "adapter_version": "1.0.0",
    "image_digest": IMAGE_DIGEST,
}
NONCE = "01900000-0000-7000-8000-0000000000aa"


def per_epoch(parameters: dict = PARAMETERS) -> int:
    return training_state.batches_per_epoch(parameters["subset_size"], parameters["batch_size"])


def training_document(step: int, parameters: dict = PARAMETERS, *, threads: int = 1) -> dict:
    epochs = parameters["epochs"]
    epoch, batch_index = divmod(step, per_epoch(parameters))
    finished = epoch == epochs
    running = None
    if not finished:
        running = (
            training_state.initial_order_digest(epoch)
            if batch_index == 0
            else "sha256:" + hashlib.sha256(f"running:{step}".encode()).hexdigest()
        )
    return {
        "schema_version": 1,
        "format": training_state.FORMAT,
        "architecture_id": pytorch_arch.ARCHITECTURE_ID,
        "architecture_version": pytorch_arch.ARCHITECTURE_VERSION,
        "preprocessing": pytorch_arch.PREPROCESSING,
        "optimizer": {
            "algorithm": pytorch_arch.OPTIMIZER,
            "learning_rate": parameters["learning_rate"],
            "momentum": pytorch_arch.MOMENTUM,
        },
        "threads": threads,
        "input_checksum": INPUT_CHECKSUM,
        "spec_checksum": SPEC_CHECKSUM,
        "cursor": {"epoch": epoch, "batch_index": batch_index, "step": step},
        "sampler": {
            "algorithm": training_state.SAMPLER_ALGORITHM,
            "seed": parameters["seed"],
            "subset_size": parameters["subset_size"],
            "batch_size": parameters["batch_size"],
            "epochs": epochs,
            "permutation_checksum": None
            if finished
            else training_state.permutation_checksum(
                training_state.permutation(parameters["seed"], epoch, parameters["subset_size"])
            ),
        },
        "sample_order": {
            "completed": [
                "sha256:" + hashlib.sha256(f"epoch:{index}".encode()).hexdigest()
                for index in range(epoch)
            ],
            "running": running,
        },
    }


def _zeros(dtype: str, shape: tuple[int, ...], fill: int) -> tuple[str, tuple[int, ...], bytes]:
    size = safetensors_format.DTYPE_SIZES[dtype] * math.prod(shape)
    return dtype, shape, bytes([fill % 256]) * size


def snapshot_bytes(document: dict, *, fill: int = 0) -> bytes:
    tensors = {}
    for name, shape in pytorch_arch.PARAMETERS.items():
        tensors["model." + name] = _zeros("F32", shape, fill)
        tensors["optimizer." + pytorch_arch.OPTIMIZER_PREFIX + name] = _zeros("F32", shape, fill)
    for name, (dtype, shape) in pytorch_arch.RNG_TENSORS.items():
        tensors["rng." + name] = _zeros(dtype, shape, fill)
    metadata = {training_state.SNAPSHOT_METADATA_KEY: canonical_json(document).decode("utf-8")}
    return safetensors_format.encode(tensors, metadata)


def checkpoint_files(step: int, parameters: dict = PARAMETERS) -> dict[str, bytes]:
    _, files = training_state.split_snapshot(snapshot_bytes(training_document(step, parameters)))
    return files


def compatibility(restart_safe: bool = True) -> dict:
    return adapter_launch.compatibility("linux/amd64", "2.12.1", restart_safe)


def restore_block(step: int, *, sequence: int = 2, parameters: dict = PARAMETERS) -> dict:
    files = checkpoint_files(step, parameters)
    document = training_state.parse_training_state(files[training_state.STATE_FILE])
    return {
        "checkpoint_id": "01900000-0000-7000-8000-0000000000c1",
        "checkpoint_sequence": sequence,
        "cursor": training_state.runtime_cursor(document),
        "files": [
            {
                "logical_name": name,
                "path": adapter_launch.restore_path(name),
                "checksum": "sha256:" + hashlib.sha256(files[name]).hexdigest(),
                "size_bytes": len(files[name]),
            }
            for name in (
                "model.safetensors",
                "optimizer.safetensors",
                "rng.safetensors",
                "training-state.json",
            )
        ],
    }


def training_spec(*, checkpoint: bool = True, restore: dict | None = None, **overrides) -> dict:
    spec = {
        "schema_version": 3,
        "adapter_id": "pytorch.cifar10",
        "adapter_version": "1.0.0",
        "startup_nonce": NONCE,
        "parameters": dict(PARAMETERS),
        "threads": 1,
        "spec_checksum": SPEC_CHECKSUM,
        "inputs": {"dataset": "/input/dataset.arrow"},
        "output_dir": "/output",
        "provenance": dict(PROVENANCE),
        "checkpoint": {
            "state_path": adapter_launch.TRAINING_STATE_PATH,
            "compatibility": compatibility(),
        }
        if checkpoint
        else None,
        "restore": restore,
    }
    spec.update(overrides)
    return spec


def model_bytes(fill: int = 0) -> bytes:
    tensors = {name: _zeros("F32", shape, fill) for name, shape in pytorch_arch.PARAMETERS.items()}
    return safetensors_format.encode(tensors, dict(pytorch_arch.MODEL_METADATA))


def metrics_document(model: bytes, parameters: dict = PARAMETERS) -> dict:
    return {
        "schema_version": 1,
        "format": adapter_launch.METRICS_FORMAT,
        "architecture_id": pytorch_arch.ARCHITECTURE_ID,
        "input_checksum": INPUT_CHECKSUM,
        "spec_checksum": SPEC_CHECKSUM,
        "model_checksum": "sha256:" + hashlib.sha256(model).hexdigest(),
        "epochs": parameters["epochs"],
        "steps": parameters["epochs"] * per_epoch(parameters),
        "subset_size": parameters["subset_size"],
        "batch_size": parameters["batch_size"],
        "eval_items": 1000,
        "train_loss": 2.25,
        "eval_loss": 2.2875,
        "eval_accuracy": 0.137,
        "model_l2_norm": 12.5,
        "optimizer_momentum_l2_norm": 0.75,
        "parameter_l2_norms": {name: 1.5 for name in pytorch_arch.PARAMETERS},
        "epoch_sample_order_digests": [
            "sha256:" + hashlib.sha256(f"epoch:{index}".encode()).hexdigest()
            for index in range(parameters["epochs"])
        ],
    }


INFERENCE_PARAMETERS = {"chunk_size": 300, "batch_size": 128, "output_format": "JSONL"}
MODEL_CHECKSUM = "sha256:" + "d" * 64
INFERENCE_PROVENANCE = {
    **PROVENANCE,
    "template_id": "batch-inference",
    "adapter_id": "batch.inference",
}


def inference_document(
    next_chunk: int, *, item_count: int = 1000, parameters: dict = INFERENCE_PARAMETERS
) -> dict:
    """A closed inference-state.json document whose counts match its cursor."""
    from nexa.workloads import inference_state

    chunk_size = parameters["chunk_size"]
    items = min(item_count, next_chunk * chunk_size)
    counts = [items // pytorch_arch.NUM_CLASSES] * pytorch_arch.NUM_CLASSES
    counts[0] += items - sum(counts)
    return {
        "schema_version": 1,
        "format": inference_state.STATE_FORMAT,
        "architecture_id": pytorch_arch.ARCHITECTURE_ID,
        "input_checksum": INPUT_CHECKSUM,
        "model_checksum": MODEL_CHECKSUM,
        "spec_checksum": SPEC_CHECKSUM,
        "threads": 1,
        "chunk_size": chunk_size,
        "batch_size": parameters["batch_size"],
        "output_format": parameters["output_format"],
        "item_count": item_count,
        "next_chunk": next_chunk,
        "prediction_counts": counts,
    }


def chunk_body(index: int, fill: int = 0) -> bytes:
    return f'{{"chunk":{index},"fill":{fill}}}\n'.encode()


def chunk_file(index: int, *, artifact_id: str, body: bytes, output_format: str = "JSONL") -> dict:
    from nexa.workloads import chunk_manifest, inference_state

    return {
        "artifact_id": artifact_id,
        "logical_name": inference_state.chunk_file_name(index, output_format),
        "media_type": chunk_manifest.CHUNK_MEDIA_TYPES[output_format],
        "size_bytes": len(body),
        "checksum": "sha256:" + hashlib.sha256(body).hexdigest(),
    }


def inference_spec(*, checkpoint: bool = True, restore: dict | None = None) -> dict:
    spec = training_spec(
        checkpoint=checkpoint,
        restore=restore,
        adapter_id="batch.inference",
        parameters=dict(INFERENCE_PARAMETERS),
        inputs={"dataset": "/input/dataset.arrow", "model": "/input/model.safetensors"},
        provenance=dict(INFERENCE_PROVENANCE),
    )
    if checkpoint:
        spec["checkpoint"]["state_path"] = adapter_launch.INFERENCE_STATE_PATH
    return spec


def inference_restore_block(state: bytes, manifest: bytes, *, next_chunk: int) -> dict:
    from nexa.workloads import chunk_manifest

    return {
        "checkpoint_id": "01900000-0000-7000-8000-0000000000c1",
        "checkpoint_sequence": 2,
        "cursor": {
            "step": next_chunk,
            "epoch": 0,
            "item_cursor": min(1000, next_chunk * INFERENCE_PARAMETERS["chunk_size"]),
        },
        "files": [
            {
                "logical_name": name,
                "path": adapter_launch.restore_path(name),
                "checksum": "sha256:" + hashlib.sha256(body).hexdigest(),
                "size_bytes": len(body),
            }
            for name, body in (
                ("inference-state.json", state),
                (chunk_manifest.LOGICAL_NAME, manifest),
            )
        ],
    }
