"""Allowlisted CIFAR-10 small CNN: tensor names, shapes and file identities (stdlib only).

The runner and the server check safetensors headers against these tables; only the
workload image turns them into a torch module.
"""

from __future__ import annotations

from types import MappingProxyType

from .safetensors_format import Header, SafetensorsError

ARCHITECTURE_ID = "nexa-cifar10-smallcnn-v1"
ARCHITECTURE_VERSION = "1"
PREPROCESSING = "cifar10-u8-chw-v1"
OPTIMIZER = "sgd-momentum-v1"
MOMENTUM = 0.9
IMAGE_SHAPE = (3, 32, 32)
IMAGE_BYTES = 3 * 32 * 32
NUM_CLASSES = 10
TORCH_CPU_RNG_BYTES = 5056
MT19937_WORDS = 624

PARAMETERS = MappingProxyType(
    {
        "conv1.weight": (16, 3, 3, 3),
        "conv1.bias": (16,),
        "conv2.weight": (32, 16, 3, 3),
        "conv2.bias": (32,),
        "fc1.weight": (64, 32 * 8 * 8),
        "fc1.bias": (64,),
        "fc2.weight": (NUM_CLASSES, 64),
        "fc2.bias": (NUM_CLASSES,),
    }
)
PARAMETER_COUNT = 136_874

MODEL_METADATA = MappingProxyType(
    {
        "nexa.architecture_id": ARCHITECTURE_ID,
        "nexa.architecture_version": ARCHITECTURE_VERSION,
        "nexa.preprocessing": PREPROCESSING,
    }
)
OPTIMIZER_PREFIX = "momentum_buffer."
OPTIMIZER_METADATA = MappingProxyType(
    {
        "nexa.architecture_id": ARCHITECTURE_ID,
        "nexa.optimizer": OPTIMIZER,
    }
)
# Python random: 624 MT words plus the position; NumPy legacy MT19937: key plus position.
RNG_TENSORS = MappingProxyType(
    {
        "python.mt19937": ("U32", (MT19937_WORDS + 1,)),
        "numpy.mt19937": ("U32", (MT19937_WORDS + 1,)),
        "torch.cpu": ("U8", (TORCH_CPU_RNG_BYTES,)),
    }
)
RNG_METADATA = MappingProxyType({"nexa.rng": "python-numpy-torch-cpu-v1"})


def _require(header: Header, expected: dict[str, tuple[str, tuple[int, ...]]], metadata) -> None:
    actual = {name: (info.dtype, info.shape) for name, info in header.tensors.items()}
    if actual != expected:
        raise SafetensorsError("tensor names, dtypes or shapes do not match the architecture")
    if dict(header.metadata) != dict(metadata):
        raise SafetensorsError("file metadata does not match the architecture")


def validate_model_header(header: Header) -> None:
    _require(header, {name: ("F32", shape) for name, shape in PARAMETERS.items()}, MODEL_METADATA)


def validate_optimizer_header(header: Header) -> None:
    _require(
        header,
        {OPTIMIZER_PREFIX + name: ("F32", shape) for name, shape in PARAMETERS.items()},
        OPTIMIZER_METADATA,
    )


def validate_rng_header(header: Header) -> None:
    _require(header, dict(RNG_TENSORS), RNG_METADATA)


__all__ = [
    "ARCHITECTURE_ID",
    "ARCHITECTURE_VERSION",
    "IMAGE_BYTES",
    "IMAGE_SHAPE",
    "MODEL_METADATA",
    "MOMENTUM",
    "NUM_CLASSES",
    "OPTIMIZER",
    "OPTIMIZER_METADATA",
    "OPTIMIZER_PREFIX",
    "PARAMETERS",
    "PARAMETER_COUNT",
    "PREPROCESSING",
    "RNG_METADATA",
    "RNG_TENSORS",
    "validate_model_header",
    "validate_optimizer_header",
    "validate_rng_header",
]
