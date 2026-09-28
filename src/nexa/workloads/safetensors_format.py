"""Stdlib reader/writer for the safetensors container format (no torch, no pickle).

The runner and the server validate headers with this module; workloads use it to write
byte-deterministic files. Layout: little-endian u64 header length, a JSON header padded
with spaces to an 8-byte boundary, then the tensor data buffer.
"""

from __future__ import annotations

import json
import math
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DTYPE_SIZES = {"U8": 1, "U32": 4, "I64": 8, "F32": 4}
MAX_HEADER_BYTES = 1024 * 1024
MAX_TENSORS = 256
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_METADATA_KEY = re.compile(r"^[a-z0-9_.-]{1,64}$")
_MAX_METADATA_VALUE = 16 * 1024


class SafetensorsError(ValueError):
    """Bytes are not an allowlisted, well-formed safetensors file."""


@dataclass(frozen=True, slots=True)
class TensorInfo:
    name: str
    dtype: str
    shape: tuple[int, ...]
    begin: int
    end: int


@dataclass(frozen=True, slots=True)
class Header:
    tensors: Mapping[str, TensorInfo]
    metadata: Mapping[str, str]
    data_start: int
    data_length: int

    @property
    def total_size(self) -> int:
        return self.data_start + self.data_length


def _reject_constant(value: str) -> object:
    raise SafetensorsError(f"invalid JSON constant {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise SafetensorsError("duplicate header key")
        document[key] = value
    return document


def _element_count(shape: tuple[int, ...]) -> int:
    return math.prod(shape)


def parse_header(prefix: bytes, *, total_size: int) -> Header:
    """Validate the header at the start of ``prefix`` against a file of ``total_size`` bytes."""
    if len(prefix) < 8:
        raise SafetensorsError("file is shorter than the header length field")
    (length,) = struct.unpack("<Q", prefix[:8])
    if length < 2 or length > MAX_HEADER_BYTES or 8 + length > total_size:
        raise SafetensorsError("header length is out of bounds")
    if len(prefix) < 8 + length:
        raise SafetensorsError("header is truncated")
    try:
        text = prefix[8 : 8 + length].decode("utf-8")
        document = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except SafetensorsError:
        raise
    except (ValueError, RecursionError) as exc:
        # JSONDecodeError, UnicodeDecodeError, an integer over the digit limit, deep nesting.
        raise SafetensorsError("header is not strict UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise SafetensorsError("header must be a JSON object")
    metadata = document.pop("__metadata__", {})
    if not isinstance(metadata, dict) or not all(
        isinstance(key, str)
        and _METADATA_KEY.fullmatch(key)
        and isinstance(value, str)
        and len(value) <= _MAX_METADATA_VALUE
        for key, value in metadata.items()
    ):
        raise SafetensorsError("header metadata must map short keys to strings")
    if not 1 <= len(document) <= MAX_TENSORS:
        raise SafetensorsError("tensor count is out of bounds")
    tensors: dict[str, TensorInfo] = {}
    for name, entry in document.items():
        if not _NAME.fullmatch(name):
            raise SafetensorsError("tensor name is not allowlisted")
        if not isinstance(entry, dict) or set(entry) != {"dtype", "shape", "data_offsets"}:
            raise SafetensorsError("tensor entry fields are not closed")
        dtype, shape, offsets = entry["dtype"], entry["shape"], entry["data_offsets"]
        if not isinstance(dtype, str) or dtype not in DTYPE_SIZES:
            raise SafetensorsError("tensor dtype is not allowlisted")
        if (
            not isinstance(shape, list)
            or len(shape) > 8
            or not all(type(dim) is int and 0 < dim <= 2**31 for dim in shape)
        ):
            raise SafetensorsError("tensor shape is invalid")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or not all(type(value) is int and value >= 0 for value in offsets)
        ):
            raise SafetensorsError("tensor offsets are invalid")
        begin, end = offsets
        dims = tuple(shape)
        if end - begin != _element_count(dims) * DTYPE_SIZES[dtype]:
            raise SafetensorsError("tensor byte length does not match its shape")
        tensors[name] = TensorInfo(name, dtype, dims, begin, end)
    cursor = 0
    for info in sorted(tensors.values(), key=lambda item: (item.begin, item.end)):
        if info.begin != cursor:
            raise SafetensorsError("tensor data is not contiguous")
        cursor = info.end
    if 8 + length + cursor != total_size:
        raise SafetensorsError("data buffer length does not match the header")
    return Header(tensors, dict(metadata), 8 + length, cursor)


def decode(raw: bytes) -> tuple[Header, dict[str, bytes]]:
    header = parse_header(raw, total_size=len(raw))
    start = header.data_start
    return header, {
        name: raw[start + info.begin : start + info.end] for name, info in header.tensors.items()
    }


def read_header(path: str | Path) -> Header:
    """Parse only the header of a file; tensor data is never read."""
    with open(path, "rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(0)
        prefix = handle.read(8)
        if len(prefix) == 8:
            (length,) = struct.unpack("<Q", prefix)
            if length <= MAX_HEADER_BYTES:
                prefix += handle.read(length)
    return parse_header(prefix, total_size=size)


def encode(
    tensors: Mapping[str, tuple[str, tuple[int, ...], bytes]],
    metadata: Mapping[str, str] | None = None,
) -> bytes:
    """Deterministic bytes: names sorted, data packed in that order, header space-padded."""
    header: dict[str, object] = {}
    chunks: list[bytes] = []
    offset = 0
    for name in sorted(tensors):
        dtype, shape, data = tensors[name]
        if dtype not in DTYPE_SIZES or len(data) != _element_count(shape) * DTYPE_SIZES[dtype]:
            raise SafetensorsError(f"tensor {name} bytes do not match dtype/shape")
        header[name] = {
            "dtype": dtype,
            "shape": list(shape),
            "data_offsets": [offset, offset + len(data)],
        }
        chunks.append(bytes(data))
        offset += len(data)
    if metadata:
        header["__metadata__"] = dict(metadata)
    text = json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    encoded = text.encode("ascii")
    encoded += b" " * (-(8 + len(encoded)) % 8)
    raw = struct.pack("<Q", len(encoded)) + encoded + b"".join(chunks)
    # Round-trip through the strict parser so a writer bug can never emit a bad file.
    parse_header(raw, total_size=len(raw))
    return raw


def subset(raw: bytes, names: tuple[str, ...], metadata: Mapping[str, str]) -> bytes:
    """Re-encode the named tensors of ``raw`` into a new deterministic file."""
    _, tensors = decode(raw)
    header = parse_header(raw, total_size=len(raw))
    missing = [name for name in names if name not in tensors]
    if missing:
        raise SafetensorsError("requested tensors are absent")
    return encode(
        {
            name: (header.tensors[name].dtype, header.tensors[name].shape, tensors[name])
            for name in names
        },
        metadata,
    )


def select_prefix(raw: bytes, prefix: str, metadata: Mapping[str, str]) -> bytes:
    """Re-encode the tensors named ``prefix + name`` of ``raw`` as ``name``."""
    header, tensors = decode(raw)
    selected = {
        name.removeprefix(prefix): (info.dtype, info.shape, tensors[name])
        for name, info in header.tensors.items()
        if name.startswith(prefix)
    }
    if not selected:
        raise SafetensorsError("requested tensors are absent")
    return encode(selected, metadata)


__all__ = [
    "DTYPE_SIZES",
    "MAX_HEADER_BYTES",
    "Header",
    "SafetensorsError",
    "TensorInfo",
    "decode",
    "encode",
    "parse_header",
    "read_header",
    "select_prefix",
    "subset",
]
