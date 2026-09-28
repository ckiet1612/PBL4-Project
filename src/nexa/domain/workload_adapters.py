"""Static allowlist of WorkloadAdapter descriptors (internal-interfaces WorkloadAdapter.describe).

Each descriptor owns exactly one template family. Control-plane code looks adapters up here
instead of comparing adapter IDs inline; the ML implementations live only inside their images.
This module is stdlib-only so domain, coordinator, worker and runner can all import it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType

_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")

JSON = "application/json"
OCTET_STREAM = "application/octet-stream"
ARROW_FILE = "application/vnd.apache.arrow.file"
NDJSON = "application/x-ndjson"
PARQUET = "application/vnd.apache.parquet"
CPU_RESULT = "application/vnd.nexa.cpu-iterative-result+json"


@dataclass(frozen=True, slots=True)
class FileRule:
    logical_name: str
    media_type: str


@dataclass(frozen=True, slots=True)
class AdapterDescriptor:
    adapter_id: str
    adapter_version: str
    template_id: str
    # API/inventory framework enum and the manifest compatibility enum.
    framework: str
    manifest_framework: str
    # Exact image label value, or None when the template capability pins the version.
    framework_version: str | None
    # Value of the image label io.nexa.runner.checkpoint; None only for legacy CPU images.
    checkpoint_format: str
    checkpoint_label_required: bool
    input_kind: str
    input_media_type: str
    input_target_path: str
    model_kind: str | None
    model_media_type: str | None
    model_target_path: str | None
    state_components: tuple[str, ...]
    checkpoint_files: tuple[FileRule, ...]
    result_files: tuple[FileRule, ...]
    chunked: bool
    upload_media_types: MappingProxyType

    def framework_version_matches(self, value: object) -> bool:
        if not isinstance(value, str) or not _SEMVER.fullmatch(value):
            return False
        return self.framework_version is None or value == self.framework_version


def _uploads(**kinds: set[str]) -> MappingProxyType:
    return MappingProxyType({kind: frozenset(values) for kind, values in kinds.items()})


CPU_ITERATIVE = AdapterDescriptor(
    adapter_id="cpu.iterative",
    adapter_version="1.0.0",
    template_id="cpu-iterative",
    framework="NEXA_CPU",
    manifest_framework="PYTHON",
    framework_version="1.0.0",
    checkpoint_format="cpu-state-v1",
    checkpoint_label_required=False,
    input_kind="INPUT",
    input_media_type="application/vnd.nexa.cpu-iterative-input+json",
    input_target_path="/input/input.json",
    model_kind=None,
    model_media_type=None,
    model_target_path=None,
    state_components=("ACCUMULATOR",),
    checkpoint_files=(FileRule("state.json", JSON),),
    result_files=(FileRule("result.json", CPU_RESULT),),
    chunked=False,
    upload_media_types=_uploads(
        RESULT_FILE={CPU_RESULT, JSON},
        RESULT_MANIFEST={JSON},
        CHECKPOINT_FILE={JSON},
        CHECKPOINT_MANIFEST={JSON},
    ),
)

PYTORCH_CIFAR10 = AdapterDescriptor(
    adapter_id="pytorch.cifar10",
    adapter_version="1.0.0",
    template_id="pytorch-cifar10-cnn",
    framework="PYTORCH",
    manifest_framework="PYTORCH",
    framework_version=None,
    checkpoint_format="pytorch-cifar10-state-v1",
    checkpoint_label_required=True,
    input_kind="DATASET",
    input_media_type=ARROW_FILE,
    input_target_path="/input/dataset.arrow",
    model_kind=None,
    model_media_type=None,
    model_target_path=None,
    state_components=(
        "MODEL",
        "OPTIMIZER",
        "RNG_PYTHON",
        "RNG_NUMPY",
        "RNG_TORCH_CPU",
        "SAMPLER",
    ),
    checkpoint_files=(
        FileRule("model.safetensors", OCTET_STREAM),
        FileRule("optimizer.safetensors", OCTET_STREAM),
        FileRule("rng.safetensors", OCTET_STREAM),
        FileRule("training-state.json", JSON),
    ),
    result_files=(
        FileRule("model.safetensors", OCTET_STREAM),
        FileRule("metrics.json", JSON),
    ),
    chunked=False,
    upload_media_types=_uploads(
        RESULT_FILE={OCTET_STREAM, JSON},
        RESULT_MANIFEST={JSON},
        CHECKPOINT_FILE={OCTET_STREAM, JSON},
        CHECKPOINT_MANIFEST={JSON},
    ),
)

BATCH_INFERENCE = AdapterDescriptor(
    adapter_id="batch.inference",
    adapter_version="1.0.0",
    template_id="batch-inference",
    framework="PYTORCH",
    manifest_framework="PYTORCH",
    framework_version=None,
    checkpoint_format="batch-inference-state-v1",
    checkpoint_label_required=True,
    input_kind="DATASET",
    input_media_type=ARROW_FILE,
    input_target_path="/input/dataset.arrow",
    model_kind="MODEL",
    model_media_type=OCTET_STREAM,
    model_target_path="/input/model.safetensors",
    state_components=("INFERENCE_CURSOR",),
    checkpoint_files=(FileRule("inference-state.json", JSON),),
    result_files=(FileRule("summary.json", JSON),),
    chunked=True,
    upload_media_types=_uploads(
        RESULT_FILE={JSON, NDJSON, PARQUET},
        RESULT_MANIFEST={JSON},
        CHECKPOINT_FILE={JSON},
        CHECKPOINT_MANIFEST={JSON},
        CHUNK_OUTPUT_MANIFEST={JSON},
    ),
)

DESCRIPTORS: tuple[AdapterDescriptor, ...] = (CPU_ITERATIVE, PYTORCH_CIFAR10, BATCH_INFERENCE)
_BY_ADAPTER = {(d.adapter_id, d.adapter_version): d for d in DESCRIPTORS}
_BY_TEMPLATE = {d.template_id: d for d in DESCRIPTORS}
CHUNK_MEDIA_TYPES = MappingProxyType({"JSONL": (NDJSON, "jsonl"), "PARQUET": (PARQUET, "parquet")})


def descriptor(adapter_id: object, adapter_version: object) -> AdapterDescriptor | None:
    return _BY_ADAPTER.get((adapter_id, adapter_version))


def template_descriptor(template) -> AdapterDescriptor | None:
    """Descriptor for a template row only when its family is owned by that adapter."""
    found = descriptor(template.get("adapter_id"), template.get("adapter_version"))
    if found is None or found.template_id != template.get("template_id"):
        return None
    return found


def runtime_descriptor(template) -> AdapterDescriptor | None:
    """Adapter that executes a template row on a worker.

    A template family owned by one adapter never runs on another; any other template
    ID keeps the B11 binding by adapter ID and version alone (B16-R11).
    """
    found = descriptor(template.get("adapter_id"), template.get("adapter_version"))
    owner = _BY_TEMPLATE.get(template.get("template_id"))
    if found is None or (owner is not None and owner is not found):
        return None
    return found


def descriptor_for_labels(labels) -> AdapterDescriptor | None:
    """Exact image-label match; a mismatch never advertises an adapter."""
    if not isinstance(labels, dict) or labels.get("io.nexa.runner") != "trusted-runner-v1":
        return None
    found = descriptor(labels.get("io.nexa.adapter.id"), labels.get("io.nexa.adapter.version"))
    if (
        found is None
        or labels.get("io.nexa.framework") != found.framework
        or not found.framework_version_matches(labels.get("io.nexa.framework.version"))
    ):
        return None
    checkpoint = labels.get("io.nexa.runner.checkpoint")
    if checkpoint != found.checkpoint_format and (
        found.checkpoint_label_required or checkpoint is not None
    ):
        return None
    return found


__all__ = [
    "BATCH_INFERENCE",
    "CHUNK_MEDIA_TYPES",
    "CPU_ITERATIVE",
    "DESCRIPTORS",
    "PYTORCH_CIFAR10",
    "AdapterDescriptor",
    "FileRule",
    "descriptor",
    "descriptor_for_labels",
    "template_descriptor",
]
