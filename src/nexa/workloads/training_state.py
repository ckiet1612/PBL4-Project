"""Closed ``training-state.json`` of ``pytorch-cifar10-state-v1`` checkpoints (stdlib only).

The workload embeds this document in its snapshot, the runner splits it into the checkpoint
file ``training-state.json`` and derives the runtime cursor from it, and the server checks the
same rules without importing torch. Integers/strings only, except the optimizer scalars.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import struct

from . import pytorch_arch, safetensors_format
from .canonical_json import canonical_json
from .pytorch_arch import (
    ARCHITECTURE_ID,
    ARCHITECTURE_VERSION,
    MOMENTUM,
    OPTIMIZER,
    PREPROCESSING,
)

FORMAT = "pytorch-cifar10-state-v1"
SAMPLER_ALGORITHM = "nexa-sampler-v1"
MAX_STATE_BYTES = 16 * 1024
MAX_EPOCHS = 100
MAX_BATCH_SIZE = 512
MIN_SUBSET = 100
MAX_SUBSET = 50_000
MAX_SEED = 2**31 - 1
MAX_THREADS = 64
SNAPSHOT_METADATA_KEY = "nexa.training_state"
STATE_FILE = "training-state.json"
# Checkpoint logical names in manifest order; the snapshot prefix each one is cut from.
CHECKPOINT_FILES = (
    ("model.safetensors", "model."),
    ("optimizer.safetensors", "optimizer."),
    ("rng.safetensors", "rng."),
)
_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")


class TrainingStateError(ValueError):
    """Training state bytes or fields are outside the closed format."""


def batches_per_epoch(subset_size: int, batch_size: int) -> int:
    return -(-subset_size // batch_size)


def permutation(seed: int, epoch: int, size: int) -> list[int]:
    """Epoch order of subset positions; string seeding is platform-independent (SHA-512)."""
    order = list(range(size))
    random.Random(f"{SAMPLER_ALGORITHM}:{seed}:{epoch}").shuffle(order)
    return order


def permutation_checksum(order: list[int]) -> str:
    return "sha256:" + hashlib.sha256(struct.pack(f"<{len(order)}I", *order)).hexdigest()


def initial_order_digest(epoch: int) -> str:
    return "sha256:" + hashlib.sha256(f"nexa-sample-order-v1:{epoch}".encode()).hexdigest()


def chain_order_digest(previous: str, positions: list[int]) -> str:
    """``d = sha256(d_prev || u32le positions)``; the epoch digest is the last link."""
    digest = hashlib.sha256(bytes.fromhex(previous.removeprefix("sha256:")))
    digest.update(struct.pack(f"<{len(positions)}I", *positions))
    return "sha256:" + digest.hexdigest()


def _int(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _checksum(value: object) -> bool:
    return isinstance(value, str) and _CHECKSUM.fullmatch(value) is not None


def _fail(message: str) -> TrainingStateError:
    return TrainingStateError(message)


def validate_training_state(document: object, *, verify_permutation: bool = True) -> dict:
    """Check every closed field and the cross-field cursor/sampler/sample-order invariants."""
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "format",
        "architecture_id",
        "architecture_version",
        "preprocessing",
        "optimizer",
        "threads",
        "input_checksum",
        "spec_checksum",
        "cursor",
        "sampler",
        "sample_order",
    }:
        raise _fail("training state fields are not closed")
    if (
        document["schema_version"] != 1
        or document["format"] != FORMAT
        or document["architecture_id"] != ARCHITECTURE_ID
        or document["architecture_version"] != ARCHITECTURE_VERSION
        or document["preprocessing"] != PREPROCESSING
    ):
        raise _fail("training state identity is not allowlisted")
    optimizer = document["optimizer"]
    if (
        not isinstance(optimizer, dict)
        or set(optimizer) != {"algorithm", "learning_rate", "momentum"}
        or optimizer["algorithm"] != OPTIMIZER
        or optimizer["momentum"] != MOMENTUM
        or type(optimizer["learning_rate"]) not in (int, float)
        or not math.isfinite(optimizer["learning_rate"])
        or not 0 < optimizer["learning_rate"] <= 1
    ):
        raise _fail("training state optimizer is invalid")
    if not _int(document["threads"], 1, MAX_THREADS):
        raise _fail("training state thread count is invalid")
    if not _checksum(document["input_checksum"]) or not _checksum(document["spec_checksum"]):
        raise _fail("training state checksums are invalid")
    sampler = document["sampler"]
    if (
        not isinstance(sampler, dict)
        or set(sampler)
        != {"algorithm", "seed", "subset_size", "batch_size", "epochs", "permutation_checksum"}
        or sampler["algorithm"] != SAMPLER_ALGORITHM
        or not _int(sampler["seed"], 0, MAX_SEED)
        or not _int(sampler["subset_size"], MIN_SUBSET, MAX_SUBSET)
        or not _int(sampler["batch_size"], 1, MAX_BATCH_SIZE)
        or not _int(sampler["epochs"], 1, MAX_EPOCHS)
    ):
        raise _fail("training state sampler is invalid")
    epochs = sampler["epochs"]
    per_epoch = batches_per_epoch(sampler["subset_size"], sampler["batch_size"])
    cursor = document["cursor"]
    if (
        not isinstance(cursor, dict)
        or set(cursor) != {"epoch", "batch_index", "step"}
        or not _int(cursor["epoch"], 0, epochs)
        or not _int(cursor["batch_index"], 0, per_epoch - 1)
        or (cursor["epoch"] == epochs and cursor["batch_index"] != 0)
        or cursor["step"] != cursor["epoch"] * per_epoch + cursor["batch_index"]
    ):
        raise _fail("training state cursor is invalid")
    finished = cursor["epoch"] == epochs
    expected_permutation = None
    if not finished and verify_permutation:
        expected_permutation = permutation_checksum(
            permutation(sampler["seed"], cursor["epoch"], sampler["subset_size"])
        )
    if finished:
        if sampler["permutation_checksum"] is not None:
            raise _fail("finished training state must not carry a permutation")
    elif not _checksum(sampler["permutation_checksum"]) or (
        expected_permutation is not None and sampler["permutation_checksum"] != expected_permutation
    ):
        raise _fail("training state permutation does not match the sampler")
    order = document["sample_order"]
    if (
        not isinstance(order, dict)
        or set(order) != {"completed", "running"}
        or not isinstance(order["completed"], list)
        or len(order["completed"]) != cursor["epoch"]
        or not all(_checksum(value) for value in order["completed"])
    ):
        raise _fail("training state sample order is invalid")
    if finished:
        if order["running"] is not None:
            raise _fail("finished training state must not carry a running digest")
    elif not _checksum(order["running"]) or (
        cursor["batch_index"] == 0 and order["running"] != initial_order_digest(cursor["epoch"])
    ):
        raise _fail("training state running digest is invalid")
    return document


def _reject_constant(value: str) -> object:
    raise TrainingStateError(f"invalid JSON constant {value}")


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise TrainingStateError("duplicate training state key")
        document[key] = value
    return document


def parse_training_state(raw: bytes, *, verify_permutation: bool = True) -> dict:
    if len(raw) > MAX_STATE_BYTES:
        raise TrainingStateError("training state is too large")
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrainingStateError("training state is not strict JSON") from exc
    validated = validate_training_state(document, verify_permutation=verify_permutation)
    if canonical_json(validated) != raw:
        raise TrainingStateError("training state is not canonical JSON")
    return validated


def sampler_state_checksum(document: dict) -> str:
    payload = {key: document[key] for key in ("cursor", "sampler", "sample_order")}
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()


def runtime_cursor(document: dict) -> dict:
    """Manifest ``runtimeCursor`` derived from a validated state; never trusted from the runner."""
    cursor = document["cursor"]
    sampler = document["sampler"]
    finished = cursor["epoch"] == sampler["epochs"]
    item_cursor = (
        0
        if finished
        else min(sampler["subset_size"], cursor["batch_index"] * sampler["batch_size"])
    )
    return {
        "step": cursor["step"],
        "epoch": cursor["epoch"],
        "item_cursor": item_cursor,
        "sampler_state_checksum": sampler_state_checksum(document),
    }


def split_snapshot(raw: bytes) -> tuple[dict, dict[str, bytes]]:
    """Cut the workload snapshot into the four checkpoint files and validate each one."""
    try:
        header = safetensors_format.parse_header(raw, total_size=len(raw))
    except safetensors_format.SafetensorsError as exc:
        raise TrainingStateError(f"snapshot is malformed: {exc}") from exc
    if set(header.metadata) != {SNAPSHOT_METADATA_KEY}:
        raise TrainingStateError("snapshot metadata is not closed")
    state_raw = header.metadata[SNAPSHOT_METADATA_KEY].encode("utf-8")
    metadata = {
        "model.safetensors": dict(pytorch_arch.MODEL_METADATA),
        "optimizer.safetensors": dict(pytorch_arch.OPTIMIZER_METADATA),
        "rng.safetensors": dict(pytorch_arch.RNG_METADATA),
    }
    prefixes = tuple(prefix for _, prefix in CHECKPOINT_FILES)
    if not all(name.startswith(prefixes) for name in header.tensors):
        raise TrainingStateError("snapshot holds tensors outside the checkpoint components")
    try:
        files = {
            name: safetensors_format.select_prefix(raw, prefix, metadata[name])
            for name, prefix in CHECKPOINT_FILES
        }
    except safetensors_format.SafetensorsError as exc:
        raise TrainingStateError(f"snapshot is malformed: {exc}") from exc
    files[STATE_FILE] = state_raw
    return validate_checkpoint_files(files), files


def validate_checkpoint_files(files: dict[str, bytes]) -> dict:
    """Validate the four checkpoint files together; returns the training-state document."""
    if set(files) != {STATE_FILE, *(name for name, _ in CHECKPOINT_FILES)}:
        raise TrainingStateError("checkpoint files are not the closed set")
    document = parse_training_state(files[STATE_FILE])
    validators = {
        "model.safetensors": pytorch_arch.validate_model_header,
        "optimizer.safetensors": pytorch_arch.validate_optimizer_header,
        "rng.safetensors": pytorch_arch.validate_rng_header,
    }
    for name, validate in validators.items():
        try:
            validate(safetensors_format.parse_header(files[name], total_size=len(files[name])))
        except safetensors_format.SafetensorsError as exc:
            raise TrainingStateError(f"{name} does not match the architecture: {exc}") from exc
    return document


def total_steps(document: dict) -> int:
    sampler = document["sampler"]
    return sampler["epochs"] * batches_per_epoch(sampler["subset_size"], sampler["batch_size"])


__all__ = [
    "CHECKPOINT_FILES",
    "FORMAT",
    "SAMPLER_ALGORITHM",
    "SNAPSHOT_METADATA_KEY",
    "STATE_FILE",
    "TrainingStateError",
    "batches_per_epoch",
    "chain_order_digest",
    "initial_order_digest",
    "parse_training_state",
    "permutation",
    "permutation_checksum",
    "runtime_cursor",
    "sampler_state_checksum",
    "split_snapshot",
    "total_steps",
    "validate_checkpoint_files",
    "validate_training_state",
]
