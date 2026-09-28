"""Closed ``inference-state.json`` / ``summary.json`` of ``batch.inference`` (stdlib only).

The workload writes them, the runner derives chunk extents and the runtime cursor from them,
and the server checks the same rules without importing torch. Integers and strings only.
"""

from __future__ import annotations

import json
import re

from .canonical_json import canonical_json
from .pytorch_arch import ARCHITECTURE_ID, NUM_CLASSES

STATE_FORMAT = "batch-inference-state-v1"
SUMMARY_FORMAT = "batch-inference-summary-v1"
MAX_DOCUMENT_BYTES = 4 * 1024
MAX_ITEMS = 60_000
MAX_CHUNK_SIZE = 100_000
MAX_BATCH_SIZE = 4096
MAX_THREADS = 64
OUTPUT_FORMATS = {"JSONL": "jsonl", "PARQUET": "parquet"}
CHUNK_FILE = re.compile(r"^chunk-([0-9]{8})\.(jsonl|parquet)$")
_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")
IDENTITY_FIELDS = (
    "architecture_id",
    "input_checksum",
    "model_checksum",
    "spec_checksum",
    "threads",
    "chunk_size",
    "batch_size",
    "output_format",
    "item_count",
)


class InferenceStateError(ValueError):
    """Inference state or summary bytes are outside the closed format."""


def chunk_count(item_count: int, chunk_size: int) -> int:
    return -(-item_count // chunk_size)


def chunk_id(index: int) -> str:
    return f"chunk-{index:08d}"


def chunk_extent(index: int, item_count: int, chunk_size: int) -> tuple[int, int]:
    start = index * chunk_size
    if not 0 <= start < item_count:
        raise InferenceStateError("chunk index is outside the item range")
    return start, min(item_count, start + chunk_size)


def chunk_file_name(index: int, output_format: str) -> str:
    return f"{chunk_id(index)}.{OUTPUT_FORMATS[output_format]}"


def _int(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _check_identity(document: dict) -> None:
    if document["architecture_id"] != ARCHITECTURE_ID:
        raise InferenceStateError("inference architecture is not allowlisted")
    for field in ("input_checksum", "model_checksum", "spec_checksum"):
        value = document[field]
        if not isinstance(value, str) or not _CHECKSUM.fullmatch(value):
            raise InferenceStateError(f"inference {field} is invalid")
    if (
        not _int(document["threads"], 1, MAX_THREADS)
        or not _int(document["chunk_size"], 1, MAX_CHUNK_SIZE)
        or not _int(document["batch_size"], 1, MAX_BATCH_SIZE)
        or document["output_format"] not in OUTPUT_FORMATS
        or not _int(document["item_count"], 1, MAX_ITEMS)
    ):
        raise InferenceStateError("inference parameters are invalid")


def _check_counts(counts: object, expected_total: int) -> None:
    if (
        not isinstance(counts, list)
        or len(counts) != NUM_CLASSES
        or not all(_int(value, 0, MAX_ITEMS) for value in counts)
        or sum(counts) != expected_total
    ):
        raise InferenceStateError("prediction counts do not match the cursor")


def validate_state(document: object) -> dict:
    fields = {"schema_version", "format", *IDENTITY_FIELDS, "next_chunk", "prediction_counts"}
    if not isinstance(document, dict) or set(document) != fields:
        raise InferenceStateError("inference state fields are not closed")
    if document["schema_version"] != 1 or document["format"] != STATE_FORMAT:
        raise InferenceStateError("inference state format is unsupported")
    _check_identity(document)
    total = chunk_count(document["item_count"], document["chunk_size"])
    if not _int(document["next_chunk"], 0, total):
        raise InferenceStateError("inference cursor is out of range")
    _check_counts(document["prediction_counts"], item_cursor(document))
    return document


def validate_summary(document: object) -> dict:
    fields = {"schema_version", "format", *IDENTITY_FIELDS, "chunk_count", "prediction_counts"}
    if not isinstance(document, dict) or set(document) != fields:
        raise InferenceStateError("inference summary fields are not closed")
    if document["schema_version"] != 1 or document["format"] != SUMMARY_FORMAT:
        raise InferenceStateError("inference summary format is unsupported")
    _check_identity(document)
    if document["chunk_count"] != chunk_count(document["item_count"], document["chunk_size"]):
        raise InferenceStateError("inference summary chunk count is inconsistent")
    _check_counts(document["prediction_counts"], document["item_count"])
    return document


def item_cursor(document: dict) -> int:
    return min(document["item_count"], document["next_chunk"] * document["chunk_size"])


def runtime_cursor(document: dict) -> dict:
    return {"step": document["next_chunk"], "epoch": 0, "item_cursor": item_cursor(document)}


def summary_from_state(document: dict) -> dict:
    """The final state with every chunk written becomes the result summary."""
    if document["next_chunk"] != chunk_count(document["item_count"], document["chunk_size"]):
        raise InferenceStateError("inference has chunks left")
    summary = {field: document[field] for field in IDENTITY_FIELDS}
    summary.update(
        schema_version=1,
        format=SUMMARY_FORMAT,
        chunk_count=document["next_chunk"],
        prediction_counts=list(document["prediction_counts"]),
    )
    return validate_summary(summary)


def _reject_constant(value: str) -> object:
    raise InferenceStateError(f"invalid JSON constant {value}")


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise InferenceStateError("duplicate inference key")
        document[key] = value
    return document


def _parse(raw: bytes, validator) -> dict:
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise InferenceStateError("inference document is too large")
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InferenceStateError("inference document is not strict JSON") from exc
    validated = validator(document)
    if canonical_json(validated) != raw:
        raise InferenceStateError("inference document is not canonical JSON")
    return validated


def parse_state(raw: bytes) -> dict:
    return _parse(raw, validate_state)


def parse_summary(raw: bytes) -> dict:
    return _parse(raw, validate_summary)


__all__ = [
    "CHUNK_FILE",
    "IDENTITY_FIELDS",
    "OUTPUT_FORMATS",
    "STATE_FORMAT",
    "SUMMARY_FORMAT",
    "InferenceStateError",
    "chunk_count",
    "chunk_extent",
    "chunk_file_name",
    "chunk_id",
    "item_cursor",
    "parse_state",
    "parse_summary",
    "runtime_cursor",
    "summary_from_state",
    "validate_state",
    "validate_summary",
]
