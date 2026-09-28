"""Torch-side helpers for the B16 PyTorch workloads; imported only inside the workload image.

Control-plane packages never import this module (tests/workloads boundary test). Files are
read and written through the stdlib safetensors codec; nothing here unpickles.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import torch
from torch import nn
from torch.nn import functional as F  # noqa: N812

from . import pytorch_arch as arch
from . import safetensors_format
from .dataset_format import COLUMNS, METADATA_KEY, DatasetError, RowDigest, parse_metadata

# sysexits(3): the runner maps these to INVALID_INPUT and INTERNAL.
EXIT_INVALID_INPUT = 65
EXIT_INTERNAL = 70
_READ_CHUNK = 1024 * 1024


class WorkloadInputError(Exception):
    """Dataset, model, parameters or restore state are invalid for this workload."""


class InternalWorkloadError(Exception):
    """A state the runner already verified is inconsistent; mapped to INTERNAL."""


class NonFiniteError(InternalWorkloadError):
    """Loss, gradient or output became NaN/Inf; never retried."""


def configure_torch(threads: int) -> None:
    if not 1 <= threads <= 64:
        raise WorkloadInputError("thread count is out of range")
    torch.set_num_threads(threads)
    # Settable once per process, before any parallel work; later calls keep the value.
    if torch.get_num_interop_threads() != 1:
        torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)


class SmallCnn(nn.Module):
    """``nexa-cifar10-smallcnn-v1``; parameter names/shapes equal ``pytorch_arch.PARAMETERS``."""

    def __init__(self) -> None:
        super().__init__()
        self.conv1 = nn.Conv2d(3, 16, 3, padding=1)
        self.conv2 = nn.Conv2d(16, 32, 3, padding=1)
        self.fc1 = nn.Linear(32 * 8 * 8, 64)
        self.fc2 = nn.Linear(64, arch.NUM_CLASSES)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = F.max_pool2d(F.relu(self.conv1(x)), 2)
        x = F.max_pool2d(F.relu(self.conv2(x)), 2)
        x = torch.flatten(x, 1)
        return self.fc2(F.relu(self.fc1(x)))


@dataclass(frozen=True)
class Dataset:
    metadata: dict
    source_index: np.ndarray
    labels: np.ndarray
    images: np.ndarray

    def split(self, name: str) -> tuple[int, int]:
        bounds = self.metadata["splits"].get(name)
        if bounds is None:
            raise WorkloadInputError(f"dataset has no {name} split")
        return int(bounds[0]), int(bounds[1])


def file_checksum(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def load_dataset(path: str | Path) -> Dataset:
    """Read the closed Arrow IPC file and verify every row against its metadata."""
    try:
        with pa.OSFile(str(path), "rb") as source:
            reader = pa.ipc.open_file(source)
            schema = reader.schema
            table = reader.read_all()
    except (OSError, pa.ArrowException) as exc:
        raise WorkloadInputError("dataset is not a readable Arrow IPC file") from exc
    expected = pa.schema(
        [
            pa.field("source_index", pa.uint32(), nullable=False),
            pa.field("label", pa.uint8(), nullable=False),
            pa.field("image", pa.binary(arch.IMAGE_BYTES), nullable=False),
        ]
    )
    if schema.names != list(COLUMNS) or not schema.remove_metadata().equals(expected):
        raise WorkloadInputError("dataset columns do not match the closed schema")
    raw = (schema.metadata or {}).get(METADATA_KEY.encode())
    if raw is None or set(schema.metadata) != {METADATA_KEY.encode()}:
        raise WorkloadInputError("dataset metadata is missing or not closed")
    try:
        metadata = parse_metadata(raw)
    except DatasetError as exc:
        raise WorkloadInputError(str(exc)) from exc
    if table.num_rows != metadata["item_count"] or any(
        column.null_count for column in table.columns
    ):
        raise WorkloadInputError("dataset row count does not match its metadata")
    source_index = table.column("source_index").to_numpy()
    labels = table.column("label").to_numpy()
    image_column = table.column("image").combine_chunks()
    buffer = image_column.buffers()[1]
    offset = image_column.offset * arch.IMAGE_BYTES
    images = np.frombuffer(buffer, dtype=np.uint8)[
        offset : offset + table.num_rows * arch.IMAGE_BYTES
    ].reshape(table.num_rows, arch.IMAGE_BYTES)
    digest = RowDigest()
    counts = {name: [0] * arch.NUM_CLASSES for name in metadata["splits"]}
    bounds = sorted((tuple(value), name) for name, value in metadata["splits"].items())
    try:
        for row in range(table.num_rows):
            label = int(labels[row])
            digest.update(int(source_index[row]), label, images[row].tobytes())
            for (low, high), name in bounds:
                if low <= row < high and label < arch.NUM_CLASSES:
                    counts[name][label] += 1
    except DatasetError as exc:
        raise WorkloadInputError(str(exc)) from exc
    if (
        digest.indices_checksum != metadata["indices_checksum"]
        or digest.content_checksum != metadata["content_checksum"]
        or counts != metadata["class_counts"]
    ):
        raise WorkloadInputError("dataset rows do not match their metadata checksums")
    return Dataset(metadata, source_index, labels, images)


def images_to_tensor(images: np.ndarray) -> torch.Tensor:
    """``cifar10-u8-chw-v1``: uint8 CHW rows scaled to [-1, 1]."""
    tensor = torch.from_numpy(images.astype(np.float32)).view(-1, *arch.IMAGE_SHAPE)
    return tensor.div_(255.0).sub_(0.5).div_(0.5)


def tensor_bytes(tensor: torch.Tensor) -> bytes:
    return tensor.detach().contiguous().cpu().numpy().tobytes()


def model_tensors(model: SmallCnn) -> dict[str, tuple[str, tuple[int, ...], bytes]]:
    return {
        name: ("F32", tuple(parameter.shape), tensor_bytes(parameter))
        for name, parameter in model.named_parameters()
    }


def encode_model(model: SmallCnn) -> bytes:
    return safetensors_format.encode(model_tensors(model), dict(arch.MODEL_METADATA))


def float_tensor(data: bytes, shape: tuple[int, ...]) -> torch.Tensor:
    tensor = torch.frombuffer(bytearray(data), dtype=torch.float32).view(shape).clone()
    if not bool(torch.isfinite(tensor).all()):
        raise WorkloadInputError("tensor holds non-finite values")
    return tensor


def load_model_bytes(model: SmallCnn, raw: bytes) -> None:
    """Load only allowlisted names/shapes/dtypes; anything else is INVALID_INPUT."""
    try:
        header, tensors = safetensors_format.decode(raw)
        arch.validate_model_header(header)
    except safetensors_format.SafetensorsError as exc:
        raise WorkloadInputError(str(exc)) from exc
    with torch.no_grad():
        for name, parameter in model.named_parameters():
            parameter.copy_(float_tensor(tensors[name], arch.PARAMETERS[name]))


def l2_norm(tensors) -> float:
    total = torch.zeros((), dtype=torch.float64)
    for tensor in tensors:
        total += tensor.detach().double().pow(2).sum()
    value = float(total.sqrt())
    if not np.isfinite(value):
        raise NonFiniteError("norm is not finite")
    return value


def atomic_write(path: str | Path, payload: bytes) -> None:
    """Replace ``path`` atomically so the runner never reads a torn file."""
    target = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


__all__ = [
    "EXIT_INTERNAL",
    "EXIT_INVALID_INPUT",
    "Dataset",
    "InternalWorkloadError",
    "NonFiniteError",
    "SmallCnn",
    "WorkloadInputError",
    "atomic_write",
    "configure_torch",
    "encode_model",
    "file_checksum",
    "float_tensor",
    "images_to_tensor",
    "l2_norm",
    "load_dataset",
    "load_model_bytes",
    "model_tensors",
    "tensor_bytes",
]
