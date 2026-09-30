"""Closed ``CHUNK_OUTPUT`` manifest of ``batch.inference`` (stdlib only).

The runner builds it from committed chunk bindings; the server validates the same bytes
before any recognition. Entries always form the contiguous prefix ``[0, k)`` of the job's
deterministic chunk sequence, so order, coverage and uniqueness are positional.
"""

from __future__ import annotations

import hashlib
import json
import re

from . import inference_state
from .canonical_json import canonical_json

KIND = "CHUNK_OUTPUT"
LOGICAL_NAME = "chunk-output-manifest.json"
MEDIA_TYPE = "application/json"
# B16-R17: every entry of the largest manifest fits the worker's bounded container reader,
# and every chunk file is read through the same bound.
MAX_CHUNKS = 2048
MAX_MANIFEST_BYTES = 1024 * 1024 - 16 * 1024
MAX_CHUNK_FILE_BYTES = 1024 * 1024 - 16 * 1024
CHUNK_MEDIA_TYPES = {"JSONL": "application/x-ndjson", "PARQUET": "application/vnd.apache.parquet"}
_UUID7 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")
_FIELDS = {"kind", "schema_version", "provenance", "model_checksum", "chunks", "manifest_checksum"}
_ENTRY_FIELDS = {
    "chunk_id",
    "start_index",
    "end_index_exclusive",
    "source_attempt_id",
    "source_job_fence",
    "file",
}
_FILE_FIELDS = {"artifact_id", "logical_name", "media_type", "size_bytes", "checksum"}
# B16-R21: one claim ``recognized_chunks`` item, the compact form of a recognized row.
RECOGNIZED_FIELDS = {
    "chunk_id",
    "start_index",
    "end_index_exclusive",
    "artifact_id",
    "size_bytes",
    "checksum",
    "source_attempt_id",
    "source_job_fence",
}


class ChunkManifestError(ValueError):
    """Chunk-output manifest bytes are outside the closed format."""


def _checksum(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def entry(
    index: int,
    *,
    item_count: int,
    chunk_size: int,
    source_attempt_id: str,
    source_job_fence: int,
    file: dict,
) -> dict:
    start, end = inference_state.chunk_extent(index, item_count, chunk_size)
    return {
        "chunk_id": inference_state.chunk_id(index),
        "start_index": start,
        "end_index_exclusive": end,
        "source_attempt_id": source_attempt_id,
        "source_job_fence": source_job_fence,
        "file": {field: file[field] for field in sorted(_FILE_FIELDS)},
    }


def build(*, provenance: dict, model_checksum: str, chunks: list[dict]) -> bytes:
    body = {
        "kind": KIND,
        "schema_version": 1,
        "provenance": dict(provenance),
        "model_checksum": model_checksum,
        "chunks": list(chunks),
    }
    raw = canonical_json({**body, "manifest_checksum": _checksum(canonical_json(body))})
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ChunkManifestError("chunk-output manifest is too large")
    return raw


def _int(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _check_file(document: object, index: int, output_format: str) -> None:
    if not isinstance(document, dict) or set(document) != _FILE_FIELDS:
        raise ChunkManifestError("chunk file fields are not closed")
    if (
        not isinstance(document["artifact_id"], str)
        or not _UUID7.fullmatch(document["artifact_id"])
        or document["logical_name"] != inference_state.chunk_file_name(index, output_format)
        or document["media_type"] != CHUNK_MEDIA_TYPES[output_format]
        or not _int(document["size_bytes"], 1, MAX_CHUNK_FILE_BYTES)
        or not isinstance(document["checksum"], str)
        or not _CHECKSUM.fullmatch(document["checksum"])
    ):
        raise ChunkManifestError(f"chunk {index} file entry is invalid")


def validate(
    document: object,
    *,
    provenance: dict,
    model_checksum: str,
    item_count: int,
    chunk_size: int,
    output_format: str,
    chunk_total: int,
) -> list[dict]:
    """Check one parsed manifest covering exactly chunks ``[0, chunk_total)``; return entries."""
    if not isinstance(document, dict) or set(document) != _FIELDS:
        raise ChunkManifestError("chunk-output manifest fields are not closed")
    if document["kind"] != KIND or document["schema_version"] != 1:
        raise ChunkManifestError("chunk-output manifest format is unsupported")
    if document["provenance"] != provenance:
        raise ChunkManifestError("chunk-output manifest provenance mismatch")
    if document["model_checksum"] != model_checksum:
        raise ChunkManifestError("chunk-output manifest model checksum mismatch")
    if output_format not in CHUNK_MEDIA_TYPES:
        raise ChunkManifestError("chunk output format is unsupported")
    chunks = document["chunks"]
    if not _int(chunk_total, 1, MAX_CHUNKS):
        raise ChunkManifestError("chunk-output manifest coverage is out of range")
    if not isinstance(chunks, list) or len(chunks) != chunk_total:
        raise ChunkManifestError("chunk-output manifest does not cover the cursor prefix")
    artifact_ids = set()
    for index, item in enumerate(chunks):
        if not isinstance(item, dict) or set(item) != _ENTRY_FIELDS:
            raise ChunkManifestError("chunk entry fields are not closed")
        try:
            start, end = inference_state.chunk_extent(index, item_count, chunk_size)
        except inference_state.InferenceStateError as exc:
            raise ChunkManifestError("chunk entry is outside the item range") from exc
        if (
            item["chunk_id"] != inference_state.chunk_id(index)
            or not _int(item["start_index"], start, start)
            or not _int(item["end_index_exclusive"], end, end)
            or not isinstance(item["source_attempt_id"], str)
            or not _UUID7.fullmatch(item["source_attempt_id"])
            or not _int(item["source_job_fence"], 1, 2**53 - 1)
        ):
            raise ChunkManifestError(f"chunk {index} range or source is invalid")
        _check_file(item["file"], index, output_format)
        if item["file"]["artifact_id"] in artifact_ids:
            raise ChunkManifestError("chunk artifact is listed twice")
        artifact_ids.add(item["file"]["artifact_id"])
    body = {key: value for key, value in document.items() if key != "manifest_checksum"}
    if document["manifest_checksum"] != _checksum(canonical_json(body)):
        raise ChunkManifestError("chunk-output manifest checksum mismatch")
    return chunks


def validate_recognized(value: object, *, first: int, chunk_size: int) -> list[dict]:
    """Check claim ``recognized_chunks``: the contiguous run of chunks from ``first``.

    The item count is unknown until the runner reads the input, so only the last item
    may be short; :func:`carried_entry` later binds each range to the exact extent.
    """
    if not isinstance(value, list) or not _int(first, 0, MAX_CHUNKS):
        raise ChunkManifestError("recognized chunks are not a list")
    if first + len(value) > MAX_CHUNKS:
        raise ChunkManifestError("recognized chunks exceed the chunk bound")
    artifact_ids = set()
    for offset, item in enumerate(value):
        index = first + offset
        if not isinstance(item, dict) or set(item) != RECOGNIZED_FIELDS:
            raise ChunkManifestError("recognized chunk fields are not closed")
        start = index * chunk_size
        last = offset == len(value) - 1
        end = item["end_index_exclusive"]
        if (
            item["chunk_id"] != inference_state.chunk_id(index)
            or not _int(item["start_index"], start, start)
            or not _int(end, start + 1, start + chunk_size)
            or (not last and end != start + chunk_size)
            or not isinstance(item["artifact_id"], str)
            or not _UUID7.fullmatch(item["artifact_id"])
            or not _int(item["size_bytes"], 1, MAX_CHUNK_FILE_BYTES)
            or not isinstance(item["checksum"], str)
            or not _CHECKSUM.fullmatch(item["checksum"])
            or not isinstance(item["source_attempt_id"], str)
            or not _UUID7.fullmatch(item["source_attempt_id"])
            or not _int(item["source_job_fence"], 1, 2**53 - 1)
        ):
            raise ChunkManifestError(f"recognized chunk {index} is invalid")
        if item["artifact_id"] in artifact_ids:
            raise ChunkManifestError("recognized chunk artifact is listed twice")
        artifact_ids.add(item["artifact_id"])
    return value


def carried_entry(
    item: dict, *, index: int, item_count: int, chunk_size: int, output_format: str
) -> dict:
    """The original manifest entry of one recognized chunk, carried forward unchanged."""
    try:
        start, end = inference_state.chunk_extent(index, item_count, chunk_size)
    except inference_state.InferenceStateError as exc:
        raise ChunkManifestError("recognized chunk is outside the item range") from exc
    if (
        item["chunk_id"] != inference_state.chunk_id(index)
        or (item["start_index"], item["end_index_exclusive"]) != (start, end)
        or output_format not in CHUNK_MEDIA_TYPES
    ):
        raise ChunkManifestError(f"recognized chunk {index} does not match this job's extent")
    return entry(
        index,
        item_count=item_count,
        chunk_size=chunk_size,
        source_attempt_id=item["source_attempt_id"],
        source_job_fence=item["source_job_fence"],
        file={
            "artifact_id": item["artifact_id"],
            "logical_name": inference_state.chunk_file_name(index, output_format),
            "media_type": CHUNK_MEDIA_TYPES[output_format],
            "size_bytes": item["size_bytes"],
            "checksum": item["checksum"],
        },
    )


def _reject_constant(value: str) -> object:
    raise ChunkManifestError(f"invalid JSON constant {value}")


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ChunkManifestError("duplicate chunk-output manifest key")
        document[key] = value
    return document


def parse(raw: bytes, **expected: object) -> list[dict]:
    """Parse strict canonical bytes, then :func:`validate` them against ``expected``."""
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ChunkManifestError("chunk-output manifest is too large")
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ChunkManifestError("chunk-output manifest is not strict JSON") from exc
    try:
        canonical = canonical_json(document)
    except ValueError as exc:
        raise ChunkManifestError("chunk-output manifest is not canonical JSON") from exc
    if canonical != raw:
        raise ChunkManifestError("chunk-output manifest is not canonical JSON")
    return validate(document, **expected)  # type: ignore[arg-type]


__all__ = [
    "CHUNK_MEDIA_TYPES",
    "KIND",
    "LOGICAL_NAME",
    "MAX_CHUNKS",
    "MAX_CHUNK_FILE_BYTES",
    "MAX_MANIFEST_BYTES",
    "MEDIA_TYPE",
    "RECOGNIZED_FIELDS",
    "ChunkManifestError",
    "build",
    "carried_entry",
    "entry",
    "parse",
    "validate",
    "validate_recognized",
]
