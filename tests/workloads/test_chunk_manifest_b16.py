"""B16 closed CHUNK_OUTPUT manifest: canonical bytes, positional coverage and bounds."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest
import rfc8785

from nexa.worker import docker_client
from nexa.workloads import chunk_manifest, inference_state
from nexa.workloads.canonical_json import canonical_json
from tests.workloads import b16_helpers as h

ITEMS = 1000
SIZE = h.INFERENCE_PARAMETERS["chunk_size"]
SOURCE = h.INFERENCE_PROVENANCE["attempt_id"]
PRIOR = "01900000-0000-7000-8000-0000000000b1"


def _artifact(index: int) -> str:
    return f"01900000-0000-7000-8000-{index + 0x100:012x}"


def _entries(count: int, *, source: str = SOURCE, fence: int = 3) -> list[dict]:
    return [
        chunk_manifest.entry(
            index,
            item_count=ITEMS,
            chunk_size=SIZE,
            source_attempt_id=source,
            source_job_fence=fence,
            file=h.chunk_file(index, artifact_id=_artifact(index), body=h.chunk_body(index)),
        )
        for index in range(count)
    ]


def _expected(total: int, **overrides) -> dict:
    return {
        "provenance": dict(h.INFERENCE_PROVENANCE),
        "model_checksum": h.MODEL_CHECKSUM,
        "item_count": ITEMS,
        "chunk_size": SIZE,
        "output_format": "JSONL",
        "chunk_total": total,
        **overrides,
    }


def _raw(chunks: list[dict], **overrides) -> bytes:
    return chunk_manifest.build(
        provenance=overrides.get("provenance", h.INFERENCE_PROVENANCE),
        model_checksum=overrides.get("model_checksum", h.MODEL_CHECKSUM),
        chunks=chunks,
    )


def _reseal(document: dict) -> bytes:
    body = {key: value for key, value in document.items() if key != "manifest_checksum"}
    seal = "sha256:" + hashlib.sha256(canonical_json(body)).hexdigest()
    return canonical_json({**body, "manifest_checksum": seal})


def test_built_manifest_is_canonical_and_parses_back():
    chunks = _entries(2)
    raw = _raw(chunks)
    assert raw == rfc8785.dumps(json.loads(raw))
    assert chunk_manifest.parse(raw, **_expected(2)) == chunks
    document = json.loads(raw)
    assert document["kind"] == "CHUNK_OUTPUT" and document["schema_version"] == 1
    assert [c["start_index"] for c in chunks] == [0, 300]
    assert [c["end_index_exclusive"] for c in chunks] == [300, 600]


def test_final_manifest_covers_every_item_with_a_short_last_chunk():
    total = inference_state.chunk_count(ITEMS, SIZE)
    chunks = _entries(total)
    assert chunk_manifest.parse(_raw(chunks), **_expected(total)) == chunks
    assert chunks[-1]["start_index"] == 900 and chunks[-1]["end_index_exclusive"] == ITEMS


def test_carried_entries_keep_their_prior_source():
    chunks = [*_entries(1, source=PRIOR, fence=2), *_entries(2)[1:]]
    assert chunk_manifest.parse(_raw(chunks), **_expected(2)) == chunks


def _entry(document, index=0):
    return document["chunks"][index]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d.update(kind="CHECKPOINT"),
        lambda d: d.update(schema_version=2),
        lambda d: d["provenance"].update(attempt_id=PRIOR),
        lambda d: d["provenance"].update(template_id="pytorch-cifar10-cnn"),
        lambda d: d.update(model_checksum="sha256:" + "0" * 64),
        lambda d: d["chunks"].reverse(),
        lambda d: d["chunks"].pop(0),
        lambda d: d["chunks"].append(deepcopy(_entry(d, 1))),
        lambda d: _entry(d).update(extra=1),
        lambda d: _entry(d).update(chunk_id="chunk-00000001"),
        lambda d: _entry(d).update(start_index=1),
        lambda d: _entry(d).update(end_index_exclusive=301),
        lambda d: _entry(d).update(end_index_exclusive="300"),
        lambda d: _entry(d).update(source_attempt_id="not-a-uuid"),
        lambda d: _entry(d).update(source_attempt_id="01900000-0000-4000-8000-000000000004"),
        lambda d: _entry(d).update(source_job_fence=0),
        lambda d: _entry(d).update(source_job_fence=True),
        lambda d: _entry(d)["file"].update(extra=1),
        lambda d: _entry(d)["file"].update(logical_name="chunk-00000001.jsonl"),
        lambda d: _entry(d)["file"].update(logical_name="chunk-00000000.parquet"),
        lambda d: _entry(d)["file"].update(media_type="application/json"),
        lambda d: _entry(d)["file"].update(size_bytes=0),
        lambda d: _entry(d)["file"].update(size_bytes=chunk_manifest.MAX_CHUNK_FILE_BYTES + 1),
        lambda d: _entry(d)["file"].update(checksum="sha256:" + "Z" * 64),
        lambda d: _entry(d)["file"].update(artifact_id=_entry(d, 1)["file"]["artifact_id"]),
    ],
)
def test_each_deviation_is_rejected(mutate):
    document = json.loads(_raw(_entries(2)))
    mutate(document)
    with pytest.raises(chunk_manifest.ChunkManifestError):
        chunk_manifest.parse(_reseal(document), **_expected(2))


def test_bytes_must_be_canonical_and_sealed():
    raw = _raw(_entries(2))
    with pytest.raises(chunk_manifest.ChunkManifestError):
        chunk_manifest.parse(raw + b" ", **_expected(2))
    with pytest.raises(chunk_manifest.ChunkManifestError):
        chunk_manifest.parse(json.dumps(json.loads(raw)).encode(), **_expected(2))
    document = json.loads(raw)
    document["manifest_checksum"] = "sha256:" + "0" * 64
    with pytest.raises(chunk_manifest.ChunkManifestError):
        chunk_manifest.parse(canonical_json(document), **_expected(2))
    with pytest.raises(chunk_manifest.ChunkManifestError):
        chunk_manifest.parse(raw.replace(b'"kind"', b'"kind":"X","kind"', 1), **_expected(2))


def test_coverage_must_equal_the_expected_prefix():
    raw = _raw(_entries(2))
    for total in (1, 3):
        with pytest.raises(chunk_manifest.ChunkManifestError):
            chunk_manifest.parse(raw, **_expected(total))
    with pytest.raises(chunk_manifest.ChunkManifestError):
        chunk_manifest.parse(raw, **_expected(0))
    with pytest.raises(chunk_manifest.ChunkManifestError):
        chunk_manifest.parse(raw, **_expected(2, chunk_size=200))
    with pytest.raises(chunk_manifest.ChunkManifestError):
        chunk_manifest.parse(raw, **_expected(2, output_format="PARQUET"))


def test_bounds_fit_the_worker_container_reader():
    """B16-R17: the largest manifest and every chunk file fit the bounded Docker reader."""
    assert chunk_manifest.MAX_CHUNK_FILE_BYTES == docker_client.ADAPTER_OUTPUT_MAX_BYTES
    assert chunk_manifest.MAX_MANIFEST_BYTES == docker_client.ADAPTER_OUTPUT_MAX_BYTES
    items = chunk_manifest.MAX_CHUNKS * 100_000 - 1
    widest = {
        **h.INFERENCE_PROVENANCE,
        "job_fence": 2**53 - 1,
    }
    chunks = [
        {
            "chunk_id": inference_state.chunk_id(index),
            "start_index": index * 100_000,
            "end_index_exclusive": min(items, (index + 1) * 100_000),
            "source_attempt_id": SOURCE,
            "source_job_fence": 2**53 - 1,
            "file": {
                "artifact_id": _artifact(index),
                "logical_name": inference_state.chunk_file_name(index, "PARQUET"),
                "media_type": chunk_manifest.CHUNK_MEDIA_TYPES["PARQUET"],
                "size_bytes": chunk_manifest.MAX_CHUNK_FILE_BYTES,
                "checksum": "sha256:" + "f" * 64,
            },
        }
        for index in range(chunk_manifest.MAX_CHUNKS)
    ]
    raw = chunk_manifest.build(provenance=widest, model_checksum=h.MODEL_CHECKSUM, chunks=chunks)
    assert len(raw) <= chunk_manifest.MAX_MANIFEST_BYTES
    with pytest.raises(chunk_manifest.ChunkManifestError):
        chunk_manifest.parse(
            raw,
            **_expected(
                chunk_manifest.MAX_CHUNKS + 1,
                provenance=widest,
                item_count=items,
                chunk_size=100_000,
                output_format="PARQUET",
            ),
        )


def test_media_types_match_the_adapter_descriptor():
    from nexa.domain import workload_adapters

    assert {
        name: media for name, (media, _) in workload_adapters.CHUNK_MEDIA_TYPES.items()
    } == chunk_manifest.CHUNK_MEDIA_TYPES
    assert (
        chunk_manifest.MEDIA_TYPE
        in workload_adapters.BATCH_INFERENCE.upload_media_types["CHUNK_OUTPUT_MANIFEST"]
    )
