"""B16 inference-state.json / summary.json and chunk extents (stdlib, no torch)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nexa.workloads import inference_state, pytorch_arch
from nexa.workloads.canonical_json import canonical_json


def _state(next_chunk: int = 2, item_count: int = 1000, chunk_size: int = 300) -> dict:
    items = min(item_count, next_chunk * chunk_size)
    counts = [items // 10] * 10
    counts[0] += items - sum(counts)
    return {
        "schema_version": 1,
        "format": inference_state.STATE_FORMAT,
        "architecture_id": pytorch_arch.ARCHITECTURE_ID,
        "input_checksum": "sha256:" + "a" * 64,
        "model_checksum": "sha256:" + "b" * 64,
        "spec_checksum": "sha256:" + "c" * 64,
        "threads": 1,
        "chunk_size": chunk_size,
        "batch_size": 128,
        "output_format": "JSONL",
        "item_count": item_count,
        "next_chunk": next_chunk,
        "prediction_counts": counts,
    }


@given(st.integers(1, 5000), st.integers(1, 6000))
@settings(max_examples=200, deadline=None)
def test_chunks_tile_the_item_range_exactly(item_count: int, chunk_size: int) -> None:
    total = inference_state.chunk_count(item_count, chunk_size)
    cursor = 0
    for index in range(total):
        start, end = inference_state.chunk_extent(index, item_count, chunk_size)
        assert start == cursor
        assert 0 < end - start <= chunk_size
        cursor = end
    assert cursor == item_count
    with pytest.raises(inference_state.InferenceStateError):
        inference_state.chunk_extent(total, item_count, chunk_size)


def test_chunk_names_follow_the_contract_pattern() -> None:
    assert inference_state.chunk_id(7) == "chunk-00000007"
    assert inference_state.chunk_file_name(7, "JSONL") == "chunk-00000007.jsonl"
    assert inference_state.chunk_file_name(7, "PARQUET") == "chunk-00000007.parquet"
    assert inference_state.CHUNK_FILE.match("chunk-00000007.jsonl")
    assert not inference_state.CHUNK_FILE.match("chunk-7.jsonl")


@pytest.mark.parametrize("next_chunk", [0, 1, 3, 4])
def test_state_round_trip_and_cursor(next_chunk: int) -> None:
    document = _state(next_chunk)
    assert inference_state.parse_state(canonical_json(document)) == document
    assert inference_state.runtime_cursor(document) == {
        "step": next_chunk,
        "epoch": 0,
        "item_cursor": min(1000, next_chunk * 300),
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format", "pytorch-cifar10-state-v1"),
        ("architecture_id", "other"),
        ("model_checksum", "md5:1"),
        ("chunk_size", 0),
        ("chunk_size", 100_001),
        ("batch_size", 4097),
        ("output_format", "CSV"),
        ("item_count", 0),
        ("next_chunk", 5),
        ("prediction_counts", [0] * 10),
        ("prediction_counts", [60] * 9),
        ("threads", 65),
    ],
)
def test_invalid_state_fields_are_rejected(field: str, value: object) -> None:
    document = _state()
    document[field] = value
    with pytest.raises(inference_state.InferenceStateError):
        inference_state.validate_state(document)


def test_state_fields_are_closed_and_canonical() -> None:
    document = _state()
    document["extra"] = 1
    with pytest.raises(inference_state.InferenceStateError, match="closed"):
        inference_state.validate_state(document)
    raw = canonical_json(_state())
    with pytest.raises(inference_state.InferenceStateError, match="canonical"):
        inference_state.parse_state(raw.replace(b",", b", ", 1))
    with pytest.raises(inference_state.InferenceStateError, match="large"):
        inference_state.parse_state(b" " * 5000)


def test_summary_requires_every_chunk() -> None:
    with pytest.raises(inference_state.InferenceStateError, match="chunks left"):
        inference_state.summary_from_state(_state(3))
    summary = inference_state.summary_from_state(_state(4))
    assert summary["chunk_count"] == 4
    assert sum(summary["prediction_counts"]) == 1000
    assert inference_state.parse_summary(canonical_json(summary)) == summary
    summary["chunk_count"] = 5
    with pytest.raises(inference_state.InferenceStateError, match="chunk count"):
        inference_state.validate_summary(summary)
