"""Closed metadata schema of the B16 CIFAR-10 Arrow IPC dataset files (stdlib only).

JobSpec carries a single input artifact, so the dataset describes itself: the Arrow schema
metadata key ``nexa.dataset`` holds canonical JSON with the source, subset selection,
splits and a checksum over every row (B16-R02).
"""

from __future__ import annotations

import hashlib
import json
import re
import struct

from .canonical_json import canonical_json
from .pytorch_arch import IMAGE_BYTES, NUM_CLASSES, PREPROCESSING

METADATA_KEY = "nexa.dataset"
MAX_METADATA_BYTES = 16 * 1024
MAX_ITEMS = 60_000
COLUMNS = ("source_index", "label", "image")
SPLIT_NAMES = frozenset({"train", "eval", "inference"})
_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")
_MD5 = re.compile(r"^[0-9a-f]{32}$")
# Official CIFAR-10 binary layout: 5 training members then the test member.
SOURCE_MEMBERS = (
    "cifar-10-batches-bin/data_batch_1.bin",
    "cifar-10-batches-bin/data_batch_2.bin",
    "cifar-10-batches-bin/data_batch_3.bin",
    "cifar-10-batches-bin/data_batch_4.bin",
    "cifar-10-batches-bin/data_batch_5.bin",
    "cifar-10-batches-bin/test_batch.bin",
)
RECORDS_PER_MEMBER = 10_000
RECORD_BYTES = 1 + IMAGE_BYTES


class DatasetError(ValueError):
    """The dataset metadata or rows do not match the closed B16 format."""


def _reject_constant(value: str) -> object:
    raise DatasetError(f"invalid JSON constant {value}")


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise DatasetError("duplicate metadata key")
        document[key] = value
    return document


def _int(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def validate_metadata(document: object) -> dict[str, object]:
    """Check the closed metadata object; ranges are checked against ``item_count``."""
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "kind",
        "preprocessing",
        "source",
        "seed",
        "item_count",
        "splits",
        "indices_checksum",
        "class_counts",
        "content_checksum",
    }:
        raise DatasetError("dataset metadata fields are not closed")
    if document["schema_version"] != 1 or document["kind"] != "cifar10-subset":
        raise DatasetError("dataset metadata kind or version is unsupported")
    if document["preprocessing"] != PREPROCESSING:
        raise DatasetError("dataset preprocessing is not allowlisted")
    source = document["source"]
    if (
        not isinstance(source, dict)
        or set(source) != {"name", "archive_md5", "archive_sha256", "members"}
        or source["name"] != "cifar-10-binary"
        or not isinstance(source["archive_md5"], str)
        or not _MD5.fullmatch(source["archive_md5"])
        or not isinstance(source["archive_sha256"], str)
        or not _CHECKSUM.fullmatch(source["archive_sha256"])
        or source["members"] != list(SOURCE_MEMBERS)
    ):
        raise DatasetError("dataset source is invalid")
    item_count = document["item_count"]
    if not _int(document["seed"], 0, 2**31 - 1) or not _int(item_count, 1, MAX_ITEMS):
        raise DatasetError("dataset seed or item count is out of range")
    splits = document["splits"]
    counts = document["class_counts"]
    if (
        not isinstance(splits, dict)
        or not splits
        or not set(splits) <= SPLIT_NAMES
        or not isinstance(counts, dict)
        or set(counts) != set(splits)
    ):
        raise DatasetError("dataset splits are invalid")
    # Shape and type first: sorting mixed JSON values raises TypeError (B16-R25).
    if not all(
        isinstance(bounds, list)
        and len(bounds) == 2
        and all(type(value) is int for value in bounds)
        for bounds in splits.values()
    ):
        raise DatasetError("dataset splits must tile the rows in order")
    cursor = 0
    for name, bounds in sorted(splits.items(), key=lambda item: item[1]):
        if bounds[0] != cursor or bounds[1] <= bounds[0]:
            raise DatasetError("dataset splits must tile the rows in order")
        cursor = bounds[1]
        per_class = counts[name]
        if (
            not isinstance(per_class, list)
            or len(per_class) != NUM_CLASSES
            or not all(_int(value, 0, MAX_ITEMS) for value in per_class)
            or sum(per_class) != bounds[1] - bounds[0]
        ):
            raise DatasetError("dataset class counts do not match the splits")
    if cursor != item_count:
        raise DatasetError("dataset splits do not cover every row")
    for field in ("indices_checksum", "content_checksum"):
        if not isinstance(document[field], str) or not _CHECKSUM.fullmatch(document[field]):
            raise DatasetError(f"dataset {field} is invalid")
    return document


def parse_metadata(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_METADATA_BYTES:
        raise DatasetError("dataset metadata is too large")
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_reject_constant
        )
    except DatasetError:
        raise
    except (ValueError, RecursionError) as exc:
        # JSONDecodeError, UnicodeDecodeError, an integer over the digit limit, deep nesting.
        raise DatasetError("dataset metadata is not strict JSON") from exc
    try:
        validated = validate_metadata(document)
    except (TypeError, RecursionError) as exc:
        # User bytes stay INVALID_INPUT even if a check meets an unexpected JSON type.
        raise DatasetError("dataset metadata has an invalid value type") from exc
    if canonical_json(validated) != raw:
        raise DatasetError("dataset metadata is not canonical JSON")
    return validated


class RowDigest:
    """Streaming checksums over (source_index u32le, label u8, image 3072 bytes) rows."""

    def __init__(self) -> None:
        self._indices = hashlib.sha256()
        self._content = hashlib.sha256()
        self.count = 0

    def update(self, source_index: int, label: int, image: bytes) -> None:
        if not 0 <= label < NUM_CLASSES or len(image) != IMAGE_BYTES:
            raise DatasetError("dataset row is malformed")
        packed = struct.pack("<I", source_index)
        self._indices.update(packed)
        self._content.update(packed + bytes((label,)) + image)
        self.count += 1

    @property
    def indices_checksum(self) -> str:
        return "sha256:" + self._indices.hexdigest()

    @property
    def content_checksum(self) -> str:
        return "sha256:" + self._content.hexdigest()


__all__ = [
    "COLUMNS",
    "METADATA_KEY",
    "RECORDS_PER_MEMBER",
    "RECORD_BYTES",
    "SOURCE_MEMBERS",
    "DatasetError",
    "RowDigest",
    "parse_metadata",
    "validate_metadata",
]
