"""Stdlib claim contexts of the B16 PyTorch adapters, shaped like the server claim."""

from __future__ import annotations

import hashlib
from dataclasses import asdict

import rfc8785

from nexa.domain import workload_adapters
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.worker.result_flow import checksum
from nexa.workloads import adapter_launch, training_state
from tests.worker.test_models import UUIDS, authority
from tests.workloads import b16_helpers as h

ARCH = "linux/amd64"
FRAMEWORK_VERSION = "2.12.1"
DATASET = b"ARROW1-fixture-bytes"
DATASET_ID = "018f0d60-7b6a-7a30-9d82-1aa39c4f30b7"
TRAINING_LABELS = {"io.nexa.runner.checkpoint": "pytorch-cifar10-state-v1"}
REQUIREMENT = {
    "architectures": [ARCH],
    "device": "CPU",
    "framework": "PYTORCH",
    "framework_version": FRAMEWORK_VERSION,
    "cuda_runtime_min": None,
    "driver_min": None,
    "compute_capability_min": None,
}
RESOURCES = {"cpu_millis": 1000, "memory_bytes": 1024**3, "gpu_count": 0}


def dataset_view(raw: bytes = DATASET) -> dict:
    return {
        "artifact_id": DATASET_ID,
        "tenant_id": UUIDS["tenant"],
        "kind": "DATASET",
        "media_type": "application/vnd.apache.arrow.file",
        "size_bytes": len(raw),
        "checksum": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "state": "COMMITTED",
    }


def training_claim(
    *,
    checkpointable=True,
    restart_safe=True,
    restore=None,
    interval=5,
    requirement=None,
    parameters=None,
) -> dict:
    return {
        "authority": asdict(authority()),
        "job_id": UUIDS["job"],
        "logical_session_id": UUIDS["session"],
        "spec": {
            "template_id": "pytorch-cifar10-cnn",
            "template_version": 1,
            "input_artifact_id": DATASET_ID,
            "model_artifact_id": None,
            "parameters": dict(parameters or h.PARAMETERS),
            "resources": dict(RESOURCES),
            "priority": 1,
            "runtime_limit_seconds": 300,
            "checkpoint_interval_seconds": interval,
        },
        "execution_intent": "RUN",
        "restore_checkpoint": restore,
        "template_snapshot": {
            "checkpointable": checkpointable,
            "restart_safe": restart_safe,
            "capability_requirement": requirement or REQUIREMENT,
        },
        "adapter_id": "pytorch.cifar10",
        "adapter_version": "1.0.0",
        "image_digest": "sha256:" + "a" * 64,
        "startup_nonce": UUIDS["nonce"],
        "allocation": {"resources": dict(RESOURCES)},
        "input_artifacts": [dataset_view()],
    }


def job_document(context: dict, step: int) -> dict:
    """The runner snapshot document of this claim (job-bound checksums, 1 thread)."""
    document = h.training_document(step, context["spec"]["parameters"])
    document["input_checksum"] = context["input_artifacts"][0]["checksum"]
    document["spec_checksum"] = checksum(context["spec"])
    return document


def job_checkpoint_files(context: dict, step: int, *, fill: int = 0) -> dict[str, bytes]:
    raw = h.snapshot_bytes(job_document(context, step), fill=fill)
    return training_state.split_snapshot(raw)[1]


def provenance(context: dict, attempt_id: str, fence: int) -> dict:
    spec = context["spec"]
    return {
        "tenant_id": context["input_artifacts"][0]["tenant_id"],
        "job_id": context["job_id"],
        "session_id": context["logical_session_id"],
        "attempt_id": attempt_id,
        "job_fence": fence,
        "input_checksum": context["input_artifacts"][0]["checksum"],
        "spec_checksum": checksum(spec),
        "template_id": spec["template_id"],
        "template_version": spec["template_version"],
        "adapter_id": context["adapter_id"],
        "adapter_version": context["adapter_version"],
        "image_digest": context["image_digest"],
    }


def sealed_training_restore(context: dict, *, step: int = 3, sequence: int = 1, files=None):
    """A committed 4-file training checkpoint exactly as the claim freezes it."""
    files = job_checkpoint_files(context, step) if files is None else files
    document = training_state.validate_checkpoint_files(files)
    tenant = context["input_artifacts"][0]["tenant_id"]
    views, entries = [], []
    names = [name for name, _ in training_state.CHECKPOINT_FILES] + [training_state.STATE_FILE]
    for name in names:
        media = "application/json" if name.endswith(".json") else "application/octet-stream"
        view = {
            "artifact_id": str(new_uuid7()),
            "tenant_id": tenant,
            "kind": "CHECKPOINT_FILE",
            "media_type": media,
            "size_bytes": len(files[name]),
            "checksum": "sha256:" + hashlib.sha256(files[name]).hexdigest(),
            "state": "COMMITTED",
        }
        views.append(view)
        entries.append(
            {
                "logical_name": name,
                "media_type": media,
                "size_bytes": view["size_bytes"],
                "checksum": view["checksum"],
                "artifact_id": view["artifact_id"],
            }
        )
    checkpoint_id = str(new_uuid7())
    body = {
        "kind": "CHECKPOINT",
        "schema_version": 1,
        "checkpoint_id": checkpoint_id,
        "checkpoint_sequence": sequence,
        "created_at": "2026-09-28T00:00:00.000Z",
        "provenance": provenance(context, str(new_uuid7()), 1),
        "compatibility": adapter_launch.compatibility(
            ARCH, FRAMEWORK_VERSION, context["template_snapshot"]["restart_safe"]
        ),
        "cursor": training_state.runtime_cursor(document),
        "state_components": list(workload_adapters.PYTORCH_CIFAR10.state_components),
        "files": entries,
    }
    manifest = {**body, "manifest_checksum": checksum(body)}
    record = {
        "checkpoint_id": checkpoint_id,
        "job_id": context["job_id"],
        "attempt_id": body["provenance"]["attempt_id"],
        "sequence": sequence,
        "manifest_artifact_id": str(new_uuid7()),
        "manifest_checksum": "sha256:" + hashlib.sha256(rfc8785.dumps(manifest)).hexdigest(),
        "state": "COMMITTED",
        "created_at": "2026-09-28T00:00:00.000Z",
    }
    return {"record": record, "manifest": manifest, "files": views}, files


MODEL = b"safetensors-model-fixture-bytes"
MODEL_ID = "018f0d60-7b6a-7a30-9d82-1aa39c4f30c8"
INFERENCE_LABELS = {"io.nexa.runner.checkpoint": "batch-inference-state-v1"}


def model_view(raw: bytes = MODEL) -> dict:
    return {
        "artifact_id": MODEL_ID,
        "tenant_id": UUIDS["tenant"],
        "kind": "MODEL",
        "media_type": "application/octet-stream",
        "size_bytes": len(raw),
        "checksum": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "state": "COMMITTED",
    }


def inference_claim(*, checkpointable=True, restart_safe=True, restore=None, interval=5) -> dict:
    context = training_claim(
        checkpointable=checkpointable,
        restart_safe=restart_safe,
        restore=restore,
        interval=interval,
        parameters=h.INFERENCE_PARAMETERS,
    )
    context["spec"].update(template_id="batch-inference", model_artifact_id=MODEL_ID)
    context.update(adapter_id="batch.inference", input_artifacts=[dataset_view(), model_view()])
    return context


def inference_job_document(context: dict, next_chunk: int, *, item_count: int = 1000) -> dict:
    """The runner's inference-state.json of this claim (job-bound checksums, 1 thread)."""
    document = h.inference_document(
        next_chunk, item_count=item_count, parameters=context["spec"]["parameters"]
    )
    document["input_checksum"] = context["input_artifacts"][0]["checksum"]
    document["spec_checksum"] = checksum(context["spec"])
    document["model_checksum"] = context["input_artifacts"][1]["checksum"]
    return document
