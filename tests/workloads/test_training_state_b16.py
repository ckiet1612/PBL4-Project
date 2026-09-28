"""B16 training-state.json, snapshot split and runtime cursor (stdlib, no torch)."""

from __future__ import annotations

import copy
import struct

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nexa.workloads import pytorch_arch, safetensors_format, training_state
from nexa.workloads.canonical_json import canonical_json

SUBSET = 200
BATCH = 64
EPOCHS = 3
PER_EPOCH = 4


def _state(epoch: int = 1, batch_index: int = 2) -> dict:
    finished = epoch == EPOCHS
    completed = [f"sha256:{index:064x}" for index in range(epoch)]
    running = None
    if not finished:
        running = (
            training_state.initial_order_digest(epoch) if batch_index == 0 else "sha256:" + "e" * 64
        )
    return {
        "schema_version": 1,
        "format": training_state.FORMAT,
        "architecture_id": pytorch_arch.ARCHITECTURE_ID,
        "architecture_version": pytorch_arch.ARCHITECTURE_VERSION,
        "preprocessing": pytorch_arch.PREPROCESSING,
        "optimizer": {"algorithm": pytorch_arch.OPTIMIZER, "learning_rate": 0.05, "momentum": 0.9},
        "threads": 1,
        "input_checksum": "sha256:" + "a" * 64,
        "spec_checksum": "sha256:" + "b" * 64,
        "cursor": {
            "epoch": epoch,
            "batch_index": batch_index,
            "step": epoch * PER_EPOCH + batch_index,
        },
        "sampler": {
            "algorithm": training_state.SAMPLER_ALGORITHM,
            "seed": 7,
            "subset_size": SUBSET,
            "batch_size": BATCH,
            "epochs": EPOCHS,
            "permutation_checksum": None
            if finished
            else training_state.permutation_checksum(training_state.permutation(7, epoch, SUBSET)),
        },
        "sample_order": {"completed": completed, "running": running},
    }


def test_permutation_is_seeded_by_seed_and_epoch() -> None:
    first = training_state.permutation(7, 0, SUBSET)
    assert first == training_state.permutation(7, 0, SUBSET)
    assert sorted(first) == list(range(SUBSET))
    assert first != training_state.permutation(7, 1, SUBSET)
    assert first != training_state.permutation(8, 0, SUBSET)
    # Golden prefix: string seeding (SHA-512) is stable across platforms and processes.
    assert training_state.permutation(0, 0, 10) == training_state.permutation(0, 0, 10)


def test_order_digest_chain_matches_documented_layout() -> None:
    import hashlib

    start = training_state.initial_order_digest(2)
    assert start == "sha256:" + hashlib.sha256(b"nexa-sample-order-v1:2").hexdigest()
    link = training_state.chain_order_digest(start, [3, 1])
    expected = hashlib.sha256(bytes.fromhex(start[7:]) + struct.pack("<2I", 3, 1)).hexdigest()
    assert link == "sha256:" + expected


@pytest.mark.parametrize(("epoch", "batch_index"), [(0, 0), (1, 2), (2, 3), (EPOCHS, 0)])
def test_valid_states_round_trip_and_derive_cursor(epoch: int, batch_index: int) -> None:
    document = _state(epoch, batch_index)
    raw = canonical_json(document)
    assert training_state.parse_training_state(raw) == document
    cursor = training_state.runtime_cursor(document)
    assert cursor["step"] == epoch * PER_EPOCH + batch_index
    assert cursor["epoch"] == epoch
    expected_items = 0 if epoch == EPOCHS else min(SUBSET, batch_index * BATCH)
    assert cursor["item_cursor"] == expected_items
    assert cursor["sampler_state_checksum"] == training_state.sampler_state_checksum(document)


@given(st.integers(0, EPOCHS * PER_EPOCH))
@settings(max_examples=50, deadline=None)
def test_runtime_cursor_is_monotonic_in_step(step: int) -> None:
    def cursor(value: int) -> dict:
        epoch, batch_index = divmod(value, PER_EPOCH)
        return training_state.runtime_cursor(_state(epoch, batch_index))

    if step < EPOCHS * PER_EPOCH:
        before, after = cursor(step), cursor(step + 1)
        assert after["step"] == before["step"] + 1
        assert after["epoch"] >= before["epoch"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), 2),
        (("format",), "cpu-state-v1"),
        (("architecture_id",), "resnet"),
        (("optimizer", "learning_rate"), 0),
        (("optimizer", "learning_rate"), 1.5),
        (("optimizer", "momentum"), 0.5),
        (("threads",), 0),
        (("input_checksum",), "sha256:x"),
        (("cursor", "step"), 5),
        (("cursor", "batch_index"), PER_EPOCH),
        (("cursor", "epoch"), EPOCHS + 1),
        (("sampler", "seed"), 2**31),
        (("sampler", "subset_size"), 99),
        (("sampler", "batch_size"), 513),
        (("sampler", "permutation_checksum"), "sha256:" + "0" * 64),
        (("sample_order", "completed"), []),
        (("sample_order", "running"), None),
    ],
)
def test_invalid_states_are_rejected(path: tuple[str, ...], value: object) -> None:
    document = copy.deepcopy(_state())
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(training_state.TrainingStateError):
        training_state.validate_training_state(document)


def test_epoch_start_requires_the_initial_running_digest() -> None:
    document = _state(1, 0)
    document["sample_order"]["running"] = "sha256:" + "e" * 64
    with pytest.raises(training_state.TrainingStateError, match="running"):
        training_state.validate_training_state(document)


def test_finished_state_rejects_leftover_sampler_fields() -> None:
    document = _state(EPOCHS, 0)
    document["sampler"]["permutation_checksum"] = "sha256:" + "0" * 64
    with pytest.raises(training_state.TrainingStateError):
        training_state.validate_training_state(document)


def test_parse_requires_canonical_bounded_json() -> None:
    raw = canonical_json(_state())
    with pytest.raises(training_state.TrainingStateError, match="canonical"):
        training_state.parse_training_state(raw.replace(b":", b": ", 1))
    with pytest.raises(training_state.TrainingStateError, match="large"):
        training_state.parse_training_state(b" " * (training_state.MAX_STATE_BYTES + 1))
    with pytest.raises(training_state.TrainingStateError, match="duplicate"):
        training_state.parse_training_state(b'{"a":1,"a":1}')
    with pytest.raises(training_state.TrainingStateError, match="strict"):
        training_state.parse_training_state(b'{"a":')


def _zeros(dtype: str, shape: tuple[int, ...]) -> tuple[str, tuple[int, ...], bytes]:
    size = safetensors_format.DTYPE_SIZES[dtype]
    count = 1
    for dim in shape:
        count *= dim
    return dtype, shape, b"\x00" * (count * size)


def _snapshot(document: dict, *, extra: dict | None = None, metadata: dict | None = None) -> bytes:
    tensors = {}
    for name, shape in pytorch_arch.PARAMETERS.items():
        tensors["model." + name] = _zeros("F32", shape)
        tensors["optimizer." + pytorch_arch.OPTIMIZER_PREFIX + name] = _zeros("F32", shape)
    for name, (dtype, shape) in pytorch_arch.RNG_TENSORS.items():
        tensors["rng." + name] = _zeros(dtype, shape)
    tensors.update(extra or {})
    return safetensors_format.encode(
        tensors,
        metadata
        if metadata is not None
        else {training_state.SNAPSHOT_METADATA_KEY: canonical_json(document).decode()},
    )


def test_split_snapshot_produces_valid_component_files() -> None:
    document = _state()
    parsed, files = training_state.split_snapshot(_snapshot(document))
    assert parsed == document
    assert list(files) == [
        "model.safetensors",
        "optimizer.safetensors",
        "rng.safetensors",
        "training-state.json",
    ]
    assert files["training-state.json"] == canonical_json(document)
    assert training_state.validate_checkpoint_files(files) == document
    header = safetensors_format.parse_header(
        files["model.safetensors"], total_size=len(files["model.safetensors"])
    )
    pytorch_arch.validate_model_header(header)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"extra": {"other.tensor": ("U8", (1,), b"\x00")}},
        {"extra": {"model.extra": ("F32", (1,), b"\x00" * 4)}},
        {"metadata": {}},
        {"metadata": {training_state.SNAPSHOT_METADATA_KEY: "{}", "extra": "x"}},
    ],
)
def test_split_snapshot_rejects_out_of_contract_snapshots(kwargs: dict) -> None:
    with pytest.raises(training_state.TrainingStateError):
        training_state.split_snapshot(_snapshot(_state(), **kwargs))


def test_checkpoint_files_must_be_the_closed_set() -> None:
    _, files = training_state.split_snapshot(_snapshot(_state()))
    extra = dict(files, **{"notes.txt": b""})
    with pytest.raises(training_state.TrainingStateError, match="closed"):
        training_state.validate_checkpoint_files(extra)
    swapped = dict(files, **{"model.safetensors": files["rng.safetensors"]})
    with pytest.raises(training_state.TrainingStateError, match="model.safetensors"):
        training_state.validate_checkpoint_files(swapped)
