"""Closed CPU iterative checkpoint state shared by workload, runner and worker.

Stdlib only: the runner image carries no third-party JSON canonicalizer. For
this closed document (ASCII keys, safe integers, lowercase-hex checksums) sorted
compact ``json.dumps`` output is byte-identical to RFC 8785, so the server's
``validate_cpu_state`` accepts exactly these bytes.
"""

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

STATE_SCHEMA_VERSION = 1
STATE_MAX_BYTES = 4096
_FIELDS = frozenset({"schema_version", "step", "accumulator", "input_checksum", "spec_checksum"})
_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_SAFE_INTEGER = 2**53 - 1


class CpuStateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CpuState:
    step: int
    accumulator: int
    input_checksum: str
    spec_checksum: str


def _is_int(value: object) -> bool:
    return type(value) is int


def _check(state: CpuState) -> None:
    if not _is_int(state.step) or not 0 <= state.step <= _MAX_SAFE_INTEGER:
        raise CpuStateError("state step is invalid")
    if not _is_int(state.accumulator) or not 0 <= state.accumulator <= _MAX_SAFE_INTEGER:
        raise CpuStateError("state accumulator is invalid")
    for value in (state.input_checksum, state.spec_checksum):
        if not isinstance(value, str) or not _CHECKSUM.fullmatch(value):
            raise CpuStateError("state checksum is invalid")


def read_state_file(path: str | Path) -> bytes:
    """Read a regular, non-symlink state file of at most ``STATE_MAX_BYTES``."""
    try:
        # O_NONBLOCK: a FIFO planted at the path opens at once and is rejected below.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise CpuStateError("state file is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > STATE_MAX_BYTES:
            raise CpuStateError("state file is not a bounded regular file")
        body = os.read(descriptor, STATE_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(body) != metadata.st_size:
        raise CpuStateError("state file changed while it was read")
    return body


def encode_state(state: CpuState) -> bytes:
    _check(state)
    document = {
        "schema_version": STATE_SCHEMA_VERSION,
        "step": state.step,
        "accumulator": state.accumulator,
        "input_checksum": state.input_checksum,
        "spec_checksum": state.spec_checksum,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CpuStateError("duplicate state key")
        result[key] = value
    return result


def _reject_number(_value: str) -> object:
    raise CpuStateError("state numbers must be integers")


def decode_state(
    raw: bytes,
    *,
    iterations: int,
    modulus: int,
    input_checksum: str,
    spec_checksum: str,
) -> CpuState:
    """Parse, bound and re-canonicalize state for exactly one job input/spec."""
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= STATE_MAX_BYTES:
        raise CpuStateError("state size is outside its bound")
    try:
        document = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_pairs,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CpuStateError("state is not strict JSON") from exc
    if not isinstance(document, dict) or set(document) != _FIELDS:
        raise CpuStateError("state fields are invalid")
    if not _is_int(document["schema_version"]) or document["schema_version"] != 1:
        raise CpuStateError("state schema version is unsupported")
    state = CpuState(
        step=document["step"],
        accumulator=document["accumulator"],
        input_checksum=document["input_checksum"],
        spec_checksum=document["spec_checksum"],
    )
    _check(state)
    if state.step > iterations or state.accumulator >= modulus:
        raise CpuStateError("state cursor is outside the job bounds")
    if state.input_checksum != input_checksum or state.spec_checksum != spec_checksum:
        raise CpuStateError("state belongs to another input or spec")
    if encode_state(state) != raw:
        raise CpuStateError("state is not canonical")
    return state


__all__ = [
    "STATE_MAX_BYTES",
    "STATE_SCHEMA_VERSION",
    "CpuState",
    "CpuStateError",
    "decode_state",
    "encode_state",
    "read_state_file",
]
