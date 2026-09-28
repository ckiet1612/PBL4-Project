"""B16 stdlib safetensors codec: strict bounded header, deterministic bytes, arch allowlist."""

import json
import struct

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nexa.workloads import pytorch_arch
from nexa.workloads.safetensors_format import (
    MAX_HEADER_BYTES,
    SafetensorsError,
    decode,
    encode,
    parse_header,
    read_header,
    select_prefix,
    subset,
)


def _raw(header: dict, data: bytes = b"", *, pad: bool = True) -> bytes:
    text = json.dumps(header).encode()
    if pad:
        text += b" " * (-(8 + len(text)) % 8)
    return struct.pack("<Q", len(text)) + text + data


def _model_tensors(fill: int = 0):
    return {
        name: ("F32", shape, bytes([fill]) * (4 * _count(shape)))
        for name, shape in pytorch_arch.PARAMETERS.items()
    }


def _count(shape):
    total = 1
    for dim in shape:
        total *= dim
    return total


_names = st.from_regex(r"[a-z][a-z0-9_.]{0,11}", fullmatch=True)
_tensor = st.tuples(
    st.sampled_from(["U8", "U32", "I64", "F32"]),
    st.lists(st.integers(min_value=1, max_value=4), min_size=0, max_size=3).map(tuple),
)


@settings(max_examples=200, deadline=None)
@given(st.dictionaries(_names, _tensor, min_size=1, max_size=6), st.binary(max_size=1))
def test_encode_decode_round_trip_is_deterministic(specs, salt) -> None:
    sizes = {"U8": 1, "U32": 4, "I64": 8, "F32": 4}
    tensors = {
        name: (dtype, shape, (salt or b"\x07") * (sizes[dtype] * _count(shape)))
        for name, (dtype, shape) in specs.items()
    }
    raw = encode(tensors, {"nexa.k": "v"})
    assert raw == encode(dict(reversed(list(tensors.items()))), {"nexa.k": "v"})
    assert (8 + struct.unpack("<Q", raw[:8])[0]) % 8 == 0
    header, data = decode(raw)
    assert dict(header.metadata) == {"nexa.k": "v"}
    assert {name: (i.dtype, i.shape) for name, i in header.tensors.items()} == specs
    assert data == {name: value[2] for name, value in tensors.items()}


def test_read_header_never_reads_tensor_data(tmp_path) -> None:
    raw = encode(_model_tensors(), dict(pytorch_arch.MODEL_METADATA))
    path = tmp_path / "model.safetensors"
    path.write_bytes(raw)
    header = read_header(path)
    pytorch_arch.validate_model_header(header)
    assert header.total_size == len(raw)
    assert sum(_count(shape) for shape in pytorch_arch.PARAMETERS.values()) == (
        pytorch_arch.PARAMETER_COUNT
    )


@pytest.mark.parametrize(
    ("header", "data", "match"),
    [
        ({"a": {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]}}, b"xx", "dtype"),
        ({"a": {"dtype": "U8", "shape": [2], "data_offsets": [0, 1]}}, b"x", "byte length"),
        ({"a": {"dtype": "U8", "shape": [1], "data_offsets": [1, 2]}}, b"xx", "contiguous"),
        ({"a": {"dtype": "U8", "shape": [1], "data_offsets": [0, 1]}}, b"xx", "buffer"),
        ({"a": {"dtype": "U8", "shape": [0], "data_offsets": [0, 0]}}, b"", "shape"),
        ({"a": {"dtype": "U8", "shape": [1], "data_offsets": [0, 1], "x": 1}}, b"x", "closed"),
        ({"../a": {"dtype": "U8", "shape": [1], "data_offsets": [0, 1]}}, b"x", "name"),
        ({"__metadata__": {"k": 1}, "a": {}}, b"", "metadata"),
        ({}, b"", "count"),
    ],
)
def test_malformed_headers_are_rejected(header, data, match) -> None:
    raw = _raw(header, data)
    with pytest.raises(SafetensorsError, match=match):
        parse_header(raw, total_size=len(raw))


def test_duplicate_keys_nan_and_oversized_headers_are_rejected() -> None:
    body = b'{"a":{"dtype":"U8","shape":[1],"data_offsets":[0,1]},"a":{}}'
    raw = struct.pack("<Q", len(body)) + body + b"x"
    with pytest.raises(SafetensorsError, match="duplicate"):
        parse_header(raw, total_size=len(raw))
    body = b'{"__metadata__":{"k":NaN}}'
    raw = struct.pack("<Q", len(body)) + body
    with pytest.raises(SafetensorsError, match="constant"):
        parse_header(raw, total_size=len(raw))
    raw = struct.pack("<Q", MAX_HEADER_BYTES + 1) + b"{}"
    with pytest.raises(SafetensorsError, match="length"):
        parse_header(raw, total_size=MAX_HEADER_BYTES + 100)
    with pytest.raises(SafetensorsError, match="shorter"):
        parse_header(b"\x01", total_size=1)


_ENTRY = '"a":{"dtype":"U8","shape":[1],"data_offsets":[0,1]}'


@pytest.mark.parametrize(
    "body",
    [
        '{"a":{"dtype":"U8","shape":[1],"data_offsets":[0,' + "9" * 5000 + "]}}",
        '{"a":{"dtype":"U8","shape":[' + "1" * 5000 + '],"data_offsets":[0,1]}}',
        '{"a":{"dtype":["U8"],"shape":[1],"data_offsets":[0,1]}}',
        '{"a":{"dtype":{"U8":1},"shape":[1],"data_offsets":[0,1]}}',
        '{"a":{"dtype":8,"shape":[1],"data_offsets":[0,1]}}',
        '{"a":{"dtype":"U8","shape":[1],"data_offsets":"0,1"}}',
        '{"a":{"dtype":"U8","shape":[1],"data_offsets":[0,"1"]}}',
        '{"a":{"dtype":"U8","shape":[1],"data_offsets":[0.0,1]}}',
        '{"a":{"dtype":"U8","shape":[1],"data_offsets":[0,1,2]}}',
        '{"a":{"dtype":"U8","shape":[1],"data_offsets":{"0":1}}}',
        '{"a":{"dtype":"U8","shape":"1","data_offsets":[0,1]}}',
        '{"a":{"dtype":"U8","shape":[[1]],"data_offsets":[0,1]}}',
        '{"a":[1],"b":1}',
        '{"__metadata__":["k"],' + _ENTRY + "}",
        '{"__metadata__":{"k":' + "7" * 5000 + "}," + _ENTRY + "}",
        "[" * 5000 + "]" * 5000,
        '"a"',
    ],
)
def test_wrong_json_types_and_parser_limits_raise_safetensors_error(body: str) -> None:
    """B16-R25: user header bytes never escape as TypeError/ValueError/RecursionError."""
    encoded = body.encode()
    raw = struct.pack("<Q", len(encoded)) + encoded + b"x"
    with pytest.raises(SafetensorsError):
        parse_header(raw, total_size=len(raw))


def test_architecture_validation_rejects_wrong_keys_shapes_dtypes_and_metadata() -> None:
    good = _model_tensors()
    variants = []
    renamed = dict(good)
    renamed["fc3.bias"] = renamed.pop("fc2.bias")
    variants.append((renamed, pytorch_arch.MODEL_METADATA))
    reshaped = dict(good)
    reshaped["fc2.bias"] = ("F32", (11,), b"\0" * 44)
    variants.append((reshaped, pytorch_arch.MODEL_METADATA))
    retyped = dict(good)
    retyped["fc2.bias"] = ("U32", (10,), b"\0" * 40)
    variants.append((retyped, pytorch_arch.MODEL_METADATA))
    variants.append((good, {**pytorch_arch.MODEL_METADATA, "nexa.architecture_version": "2"}))
    for tensors, metadata in variants:
        raw = encode(tensors, dict(metadata))
        with pytest.raises(SafetensorsError):
            pytorch_arch.validate_model_header(parse_header(raw, total_size=len(raw)))


def test_optimizer_and_rng_layouts_and_subset() -> None:
    optimizer = {
        pytorch_arch.OPTIMIZER_PREFIX + name: value for name, value in _model_tensors().items()
    }
    raw = encode(optimizer, dict(pytorch_arch.OPTIMIZER_METADATA))
    pytorch_arch.validate_optimizer_header(parse_header(raw, total_size=len(raw)))
    sizes = {"U8": 1, "U32": 4}
    rng = {
        name: (dtype, shape, b"\1" * (sizes[dtype] * _count(shape)))
        for name, (dtype, shape) in pytorch_arch.RNG_TENSORS.items()
    }
    combined = encode({**optimizer, **rng})
    part = subset(combined, tuple(rng), dict(pytorch_arch.RNG_METADATA))
    pytorch_arch.validate_rng_header(parse_header(part, total_size=len(part)))
    assert part == encode(rng, dict(pytorch_arch.RNG_METADATA))
    with pytest.raises(SafetensorsError, match="absent"):
        subset(combined, ("missing",), {})


def test_select_prefix_strips_the_prefix_and_keeps_only_matching_tensors() -> None:
    model = _model_tensors()
    snapshot = encode(
        {
            **{"model." + name: value for name, value in model.items()},
            "rng.torch.cpu": ("U8", (4,), b"\0\1\2\3"),
        },
        {"nexa.training_state": "{}"},
    )
    part = select_prefix(snapshot, "model.", pytorch_arch.MODEL_METADATA)
    assert part == encode(model, dict(pytorch_arch.MODEL_METADATA))
    rng = select_prefix(snapshot, "rng.", {})
    assert decode(rng)[1] == {"torch.cpu": b"\0\1\2\3"}
    with pytest.raises(SafetensorsError, match="absent"):
        select_prefix(snapshot, "optimizer.", {})
