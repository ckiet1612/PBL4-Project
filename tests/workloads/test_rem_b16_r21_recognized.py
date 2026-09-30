"""Remediation B16-R21: the closed ``recognized_chunks`` list and its carried entries.

Claim names the chunks recognized beyond the restore cursor; worker and runner accept
only the contiguous run that starts at that cursor, and rebuild each chunk-output
manifest entry exactly as the original source published it.
"""

import pytest

from nexa.workloads import adapter_launch, chunk_manifest, inference_state
from tests.workloads import b16_helpers as h

ATTEMPT = "01900000-0000-7000-8000-0000000000a1"
ARTIFACTS = [f"01900000-0000-7000-8000-00000000{index:04x}" for index in range(8)]
CHUNK_SIZE = h.INFERENCE_PARAMETERS["chunk_size"]


def _item(index, *, artifact=None, body=None):
    body = body or h.chunk_body(index)
    file = h.chunk_file(index, artifact_id=artifact or ARTIFACTS[index], body=body)
    start, end = inference_state.chunk_extent(index, 1000, CHUNK_SIZE)
    return {
        "chunk_id": inference_state.chunk_id(index),
        "start_index": start,
        "end_index_exclusive": end,
        "artifact_id": file["artifact_id"],
        "size_bytes": file["size_bytes"],
        "checksum": file["checksum"],
        "source_attempt_id": ATTEMPT,
        "source_job_fence": 2,
    }


def test_recognized_run_starting_at_the_restore_cursor_is_accepted():
    items = [_item(1), _item(2), _item(3)]
    assert chunk_manifest.validate_recognized(items, first=1, chunk_size=CHUNK_SIZE) == items
    assert chunk_manifest.validate_recognized([], first=4, chunk_size=CHUNK_SIZE) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda items: items[1:],  # does not start at the cursor
        lambda items: [items[0], items[2]],  # gap
        lambda items: [{**items[0], "extra": 1}, *items[1:]],
        lambda items: [{k: v for k, v in items[0].items() if k != "checksum"}],
        lambda items: [{**items[0], "start_index": items[0]["start_index"] + 1}],
        # Only the last chunk may be short.
        lambda items: (
            [{**items[0], "end_index_exclusive": items[0]["end_index_exclusive"] - 1}] + items[1:]
        ),
        lambda items: [{**items[0], "end_index_exclusive": items[0]["start_index"]}],
        lambda items: [items[0], {**items[1], "artifact_id": items[0]["artifact_id"]}],
        lambda items: [{**items[0], "artifact_id": "not-a-uuid"}],
        lambda items: [{**items[0], "source_attempt_id": "01900000-0000-4000-8000-000000000001"}],
        lambda items: [{**items[0], "source_job_fence": 0}],
        lambda items: [{**items[0], "source_job_fence": True}],
        lambda items: [{**items[0], "size_bytes": 0}],
        lambda items: [{**items[0], "size_bytes": chunk_manifest.MAX_CHUNK_FILE_BYTES + 1}],
        lambda items: [{**items[0], "checksum": "sha256:" + "A" * 64}],
        lambda items: {"chunks": items},
        lambda items: [None],
    ],
)
def test_recognized_list_outside_the_closed_format_is_rejected(mutate):
    items = [_item(1), _item(2), _item(3)]
    with pytest.raises(chunk_manifest.ChunkManifestError):
        chunk_manifest.validate_recognized(mutate(items), first=1, chunk_size=CHUNK_SIZE)


def test_recognized_list_never_exceeds_the_chunk_bound():
    last = chunk_manifest.MAX_CHUNKS - 1
    items = [
        {
            **_item(1, artifact=ARTIFACTS[offset]),
            "chunk_id": inference_state.chunk_id(index),
            "start_index": index * CHUNK_SIZE,
            "end_index_exclusive": (index + 1) * CHUNK_SIZE,
        }
        for offset, index in enumerate((last, last + 1))
    ]
    assert chunk_manifest.validate_recognized(items[:1], first=last, chunk_size=CHUNK_SIZE)
    with pytest.raises(chunk_manifest.ChunkManifestError):
        chunk_manifest.validate_recognized(items, first=last, chunk_size=CHUNK_SIZE)


def test_carried_entry_is_the_original_manifest_entry():
    body = h.chunk_body(1)
    file = h.chunk_file(1, artifact_id=ARTIFACTS[1], body=body)
    original = chunk_manifest.entry(
        1,
        item_count=1000,
        chunk_size=CHUNK_SIZE,
        source_attempt_id=ATTEMPT,
        source_job_fence=2,
        file=file,
    )
    carried = chunk_manifest.carried_entry(
        _item(1), index=1, item_count=1000, chunk_size=CHUNK_SIZE, output_format="JSONL"
    )
    assert carried == original
    with pytest.raises(chunk_manifest.ChunkManifestError):
        # The extent of this job's items decides the range, not the claim.
        chunk_manifest.carried_entry(
            _item(3), index=3, item_count=999, chunk_size=CHUNK_SIZE, output_format="JSONL"
        )
    with pytest.raises(chunk_manifest.ChunkManifestError):
        chunk_manifest.carried_entry(
            _item(1), index=2, item_count=1000, chunk_size=CHUNK_SIZE, output_format="JSONL"
        )


def _restored_spec(next_chunk):
    state = b"{}"
    manifest = b"{}"
    return h.inference_spec(
        restore=h.inference_restore_block(state, manifest, next_chunk=next_chunk)
    )


def test_launch_spec_accepts_recognized_chunks_after_the_restore_cursor():
    spec = {**_restored_spec(1), "recognized_chunks": [_item(1), _item(2)]}
    assert adapter_launch.validate_launch_spec(spec) is spec
    fallback = {**h.inference_spec(), "recognized_chunks": [_item(0)]}
    assert adapter_launch.validate_launch_spec(fallback) is fallback
    # Specs without the key (every attempt with nothing to carry) stay valid.
    adapter_launch.validate_launch_spec(_restored_spec(1))


@pytest.mark.parametrize(
    "spec",
    [
        # An empty list is never written: absence means nothing to carry.
        {**h.inference_spec(), "recognized_chunks": []},
        {**_restored_spec(1), "recognized_chunks": [_item(2)]},
        {**h.inference_spec(), "recognized_chunks": None},
        {**h.training_spec(), "recognized_chunks": [_item(0)]},
    ],
)
def test_launch_spec_rejects_recognized_chunks_it_cannot_carry(spec):
    with pytest.raises(adapter_launch.LaunchSpecError):
        adapter_launch.validate_launch_spec(spec)


def test_recognized_chunks_are_mounted_and_handed_to_the_workload():
    # RV03: the workload reads the carried chunks from read-only files, never recomputes.
    spec = {**_restored_spec(1), "recognized_chunks": [_item(1), _item(2)]}
    fmt = spec["parameters"]["output_format"]
    assert adapter_launch.recognized_paths(spec) == [
        f"/input/recognized/{inference_state.chunk_file_name(index, fmt)}" for index in (1, 2)
    ]
    command = adapter_launch.workload_command(spec)
    assert command[-4:] == ("--recognized-dir", "/input/recognized", "--recognized-count", "2")
    fallback = {**h.inference_spec(), "recognized_chunks": [_item(0)]}
    assert adapter_launch.recognized_paths(fallback) == [
        f"/input/recognized/{inference_state.chunk_file_name(0, fmt)}"
    ]
    # Nothing to carry: no mounts and no flags.
    plain = _restored_spec(1)
    assert adapter_launch.recognized_paths(plain) == []
    assert "--recognized-dir" not in adapter_launch.workload_command(plain)
