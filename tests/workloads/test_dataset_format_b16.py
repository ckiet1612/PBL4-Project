"""B16 CIFAR-10 dataset metadata: closed schema, canonical bytes and row checksums."""

from __future__ import annotations

import copy
import hashlib
import struct

import pytest

from nexa.workloads.canonical_json import canonical_json
from nexa.workloads.dataset_format import (
    SOURCE_MEMBERS,
    DatasetError,
    RowDigest,
    parse_metadata,
    validate_metadata,
)


def _metadata() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "cifar10-subset",
        "preprocessing": "cifar10-u8-chw-v1",
        "source": {
            "name": "cifar-10-binary",
            "archive_md5": "c32a1d4ab5d03f1284b67883e8d87530",
            "archive_sha256": "sha256:" + "a" * 64,
            "members": list(SOURCE_MEMBERS),
        },
        "seed": 16,
        "item_count": 30,
        "splits": {"train": [0, 20], "eval": [20, 30]},
        "indices_checksum": "sha256:" + "b" * 64,
        "class_counts": {"train": [2] * 10, "eval": [1] * 10},
        "content_checksum": "sha256:" + "c" * 64,
    }


def test_valid_metadata_round_trips_through_canonical_bytes() -> None:
    document = _metadata()
    assert parse_metadata(canonical_json(document)) == document


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), 2),
        (("kind",), "cifar100-subset"),
        (("preprocessing",), "float32-v1"),
        (("source", "name"), "cifar-10-python"),
        (("source", "archive_md5"), "C32A"),
        (("source", "archive_sha256"), "sha256:xyz"),
        (("source", "members"), list(SOURCE_MEMBERS[:5])),
        (("seed",), -1),
        (("seed",), 2**31),
        (("seed",), True),
        (("item_count",), 0),
        (("item_count",), 60_001),
        (("item_count",), 31),
        (("splits",), {"train": [0, 20], "eval": [21, 30]}),
        (("splits",), {"train": [0, 20], "test": [20, 30]}),
        (("splits",), {"train": [0, 20]}),
        (("splits",), {}),
        (("splits",), {"train": [0, 20], "eval": [20.0, 30]}),
        (("class_counts",), {"train": [2] * 10, "eval": [1] * 9}),
        (("class_counts",), {"train": [2] * 10, "eval": [1] * 9 + [2]}),
        (("class_counts",), {"train": [2] * 10, "eval": [-1] + [1] * 9 + [2]}),
        (("indices_checksum",), "sha256:" + "B" * 64),
        (("content_checksum",), 7),
    ],
)
def test_metadata_rejects_out_of_contract_values(path: tuple[str, ...], value: object) -> None:
    document = copy.deepcopy(_metadata())
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(DatasetError):
        validate_metadata(document)


def test_metadata_fields_are_closed() -> None:
    document = _metadata()
    document["extra"] = 1
    with pytest.raises(DatasetError, match="closed"):
        validate_metadata(document)
    document = _metadata()
    document["source"]["url"] = "https://example.invalid"
    with pytest.raises(DatasetError, match="source"):
        validate_metadata(document)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1}',
        b"NaN",
        b"\xff",
        b"[]",
        b" " * (16 * 1024 + 1),
    ],
)
def test_parse_metadata_rejects_non_strict_json(raw: bytes) -> None:
    with pytest.raises(DatasetError):
        parse_metadata(raw)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("splits",), {"train": [0, 20], "eval": "x"}),
        (("splits",), {"train": [0, 20], "eval": [20, "30"]}),
        (("splits",), {"train": [0, 20], "eval": [20, None]}),
        (("splits",), {"train": [0, 20], "eval": {"a": 1}}),
        (("splits",), {"train": [0, 20], "eval": [20, 30, 40]}),
        (("splits",), {"train": [0, 20], "eval": [20, True]}),
        (("splits",), ["train", "eval"]),
        (("class_counts",), {"train": [2] * 10, "eval": "x"}),
        (("class_counts",), {"train": [2] * 10, "eval": [None] * 10}),
        (("class_counts",), {"train": [2] * 10, "eval": [[1]] * 10}),
        (("class_counts",), {"train": [2] * 10, "eval": {"0": 10}}),
        (("source",), ["cifar-10-binary"]),
        (("source", "members"), {"a": 1}),
    ],
)
def test_metadata_wrong_json_types_raise_dataset_error(
    path: tuple[str, ...], value: object
) -> None:
    """B16-R25: a wrong JSON type is a DatasetError (INVALID_INPUT), never a TypeError."""
    document = copy.deepcopy(_metadata())
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(DatasetError):
        validate_metadata(document)
    with pytest.raises(DatasetError):
        parse_metadata(canonical_json(document))


@pytest.mark.parametrize(
    "raw",
    [
        b'{"seed":' + b"9" * 5000 + b"}",
        canonical_json(_metadata()).replace(b'"seed":16', b'"seed":' + b"1" * 5000),
        b"[" * 5000 + b"]" * 5000,
    ],
)
def test_parse_metadata_maps_parser_limits_to_dataset_error(raw: bytes) -> None:
    """B16-R25: integers over the digit limit and deep nesting stay DatasetError."""
    with pytest.raises(DatasetError):
        parse_metadata(raw)


def test_parse_metadata_requires_canonical_bytes() -> None:
    raw = canonical_json(_metadata())
    with pytest.raises(DatasetError, match="canonical"):
        parse_metadata(raw.replace(b",", b", ", 1))


def test_row_digest_matches_documented_layout() -> None:
    digest = RowDigest()
    rows = [(7, 3, bytes(range(256)) * 12), (50_001, 9, b"\x00" * 3072)]
    for row in rows:
        digest.update(*row)
    indices = hashlib.sha256(b"".join(struct.pack("<I", index) for index, _, _ in rows))
    content = hashlib.sha256(
        b"".join(struct.pack("<I", index) + bytes((label,)) + image for index, label, image in rows)
    )
    assert digest.count == 2
    assert digest.indices_checksum == "sha256:" + indices.hexdigest()
    assert digest.content_checksum == "sha256:" + content.hexdigest()


@pytest.mark.parametrize(("label", "image"), [(10, b"\x00" * 3072), (0, b"\x00" * 3071)])
def test_row_digest_rejects_malformed_rows(label: int, image: bytes) -> None:
    with pytest.raises(DatasetError, match="malformed"):
        RowDigest().update(0, label, image)
