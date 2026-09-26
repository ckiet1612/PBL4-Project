"""B14 CPU checkpoint state: canonical, closed, bounded and byte-equal on resume."""

import hashlib
import json
import os
import pickle
import re
import stat
import threading
from pathlib import Path

import pytest
import rfc8785
from hypothesis import given, settings
from hypothesis import strategies as st

from nexa.application.checkpoint_validation import validate_cpu_state
from nexa.workloads.cpu_entrypoint import run_cpu_file
from nexa.workloads.cpu_iterative import CpuInputError, CpuIterativeAdapter
from nexa.workloads.cpu_state import (
    STATE_MAX_BYTES,
    CpuState,
    CpuStateError,
    decode_state,
    encode_state,
    read_state_file,
)

SPEC = "sha256:" + "c" * 64
INPUT = b'{"initial_value":17}'
INPUT_CHECKSUM = "sha256:" + hashlib.sha256(INPUT).hexdigest()


def _state(step=3, accumulator=42, **overrides):
    document = {
        "schema_version": 1,
        "step": step,
        "accumulator": accumulator,
        "input_checksum": INPUT_CHECKSUM,
        "spec_checksum": SPEC,
        **overrides,
    }
    return document


def _decode(raw, *, iterations=10, modulus=101):
    return decode_state(
        raw,
        iterations=iterations,
        modulus=modulus,
        input_checksum=INPUT_CHECKSUM,
        spec_checksum=SPEC,
    )


def _accumulator_after(steps, *, initial=17, seed=7, modulus=1_000_003):
    accumulator = (initial + seed) % modulus
    for index in range(steps):
        accumulator = (accumulator * 1_664_525 + 1_013_904_223 + index) % modulus
    return accumulator


def test_encoding_is_rfc8785_and_accepted_by_server_validation() -> None:
    state = CpuState(step=3, accumulator=42, input_checksum=INPUT_CHECKSUM, spec_checksum=SPEC)
    raw = encode_state(state)

    assert raw == rfc8785.dumps(_state())
    assert _decode(raw) == state
    manifest = {
        "provenance": {"input_checksum": INPUT_CHECKSUM, "spec_checksum": SPEC},
        "cursor": {"step": 3, "epoch": 0, "item_cursor": 3, "accumulator": 42},
    }
    assert validate_cpu_state(raw, manifest=manifest) == _state()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"not json",
        json.dumps(_state(), indent=1).encode(),
        json.dumps(_state()).encode(),
        rfc8785.dumps(_state(extra=1)),
        rfc8785.dumps({k: v for k, v in _state().items() if k != "step"}),
        rfc8785.dumps(_state(schema_version=2)),
        rfc8785.dumps(_state(step=-1)),
        rfc8785.dumps(_state(step=11)),
        rfc8785.dumps(_state(step=True)),
        rfc8785.dumps(_state(accumulator=101)),
        rfc8785.dumps(_state(accumulator=-1)),
        rfc8785.dumps(_state()).replace(b'"accumulator":42', b'"accumulator":42.0'),
        rfc8785.dumps(_state(input_checksum="sha256:" + "0" * 64)),
        rfc8785.dumps(_state(spec_checksum="sha256:" + "0" * 64)),
        b'{"accumulator":42,"accumulator":43,"input_checksum":"'
        + INPUT_CHECKSUM.encode()
        + b'","schema_version":1,"spec_checksum":"'
        + SPEC.encode()
        + b'","step":3}',
        b" " * (STATE_MAX_BYTES + 1),
    ],
)
def test_decode_rejects_non_canonical_open_or_out_of_bounds_state(raw) -> None:
    with pytest.raises(CpuStateError):
        _decode(raw)


def test_pickled_state_is_rejected_and_no_source_module_deserializes_pickle() -> None:
    raw = pickle.dumps(_state())
    with pytest.raises(CpuStateError):
        _decode(raw)
    state = CpuState(step=3, accumulator=42, input_checksum=INPUT_CHECKSUM, spec_checksum=SPEC)
    with pytest.raises(CpuStateError):
        _decode(pickle.dumps(state))
    banned = re.compile(
        r"^\s*(?:import|from)\s+(?:c?pickle|marshal|shelve|dill|cloudpickle)\b", re.M
    )
    root = Path(__file__).resolve().parents[2] / "src" / "nexa"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if banned.search(path.read_text())
    ]
    assert offenders == []


def test_encode_rejects_values_outside_the_state_contract() -> None:
    with pytest.raises(CpuStateError):
        encode_state(
            CpuState(step=-1, accumulator=0, input_checksum=INPUT_CHECKSUM, spec_checksum=SPEC)
        )
    with pytest.raises(CpuStateError):
        encode_state(CpuState(step=0, accumulator=0, input_checksum="sha256:x", spec_checksum=SPEC))


@settings(max_examples=150, deadline=None)
@given(
    initial=st.integers(0, 2_147_483_647),
    seed=st.integers(0, 2_147_483_647),
    modulus=st.integers(2, 2_147_483_647),
    iterations=st.integers(1, 300),
    data=st.data(),
)
def test_resume_from_any_step_is_byte_equal_to_an_uninterrupted_run(
    initial, seed, modulus, iterations, data
) -> None:
    source = json.dumps({"initial_value": initial}, separators=(",", ":")).encode()
    adapter = CpuIterativeAdapter()
    baseline = adapter.run(
        input_bytes=source,
        iterations=iterations,
        seed=seed,
        modulus=modulus,
        spec_checksum=SPEC,
    )
    stride = data.draw(st.integers(1, iterations))
    observed: list[tuple[int, int]] = []
    observed_run = adapter.run(
        input_bytes=source,
        iterations=iterations,
        seed=seed,
        modulus=modulus,
        spec_checksum=SPEC,
        observer=lambda step, accumulator: observed.append((step, accumulator)),
        observer_stride=stride,
    )
    assert observed_run.result_bytes == baseline.result_bytes
    assert observed[0][0] == 0 and observed[-1] == (iterations, baseline.final_accumulator)
    step, accumulator = data.draw(st.sampled_from(observed))
    checksum = "sha256:" + hashlib.sha256(source).hexdigest()
    raw = encode_state(
        CpuState(step=step, accumulator=accumulator, input_checksum=checksum, spec_checksum=SPEC)
    )
    restored = decode_state(
        raw,
        iterations=iterations,
        modulus=modulus,
        input_checksum=checksum,
        spec_checksum=SPEC,
    )
    resumed = adapter.run(
        input_bytes=source,
        iterations=iterations,
        seed=seed,
        modulus=modulus,
        spec_checksum=SPEC,
        resume=restored,
    )
    assert resumed.result_bytes == baseline.result_bytes


def test_resume_applies_the_step_index_recurrence_exactly() -> None:
    k = 5
    state = CpuState(
        step=k,
        accumulator=_accumulator_after(k),
        input_checksum=INPUT_CHECKSUM,
        spec_checksum=SPEC,
    )
    result = CpuIterativeAdapter().run(
        input_bytes=INPUT,
        iterations=k + 1,
        seed=7,
        modulus=1_000_003,
        spec_checksum=SPEC,
        resume=state,
    )
    expected = (state.accumulator * 1_664_525 + 1_013_904_223 + k) % 1_000_003
    assert result.final_accumulator == expected == _accumulator_after(k + 1)


@pytest.mark.parametrize(
    "state",
    [
        CpuState(step=11, accumulator=1, input_checksum=INPUT_CHECKSUM, spec_checksum=SPEC),
        CpuState(step=1, accumulator=101, input_checksum=INPUT_CHECKSUM, spec_checksum=SPEC),
        CpuState(step=1, accumulator=1, input_checksum="sha256:" + "0" * 64, spec_checksum=SPEC),
        CpuState(
            step=1, accumulator=1, input_checksum=INPUT_CHECKSUM, spec_checksum="sha256:" + "0" * 64
        ),
    ],
)
def test_adapter_rejects_resume_state_from_another_job_or_out_of_bounds(state) -> None:
    with pytest.raises(CpuInputError):
        CpuIterativeAdapter().run(
            input_bytes=INPUT,
            iterations=10,
            seed=7,
            modulus=101,
            spec_checksum=SPEC,
            resume=state,
        )


def test_entrypoint_snapshots_state_and_resumes_to_the_same_result(tmp_path) -> None:
    source = tmp_path / "input.json"
    source.write_bytes(INPUT)
    output = tmp_path / "out" / "result.json"
    state_path = tmp_path / "out" / "state.json"
    ticks = iter(range(10_000))
    baseline = run_cpu_file(
        input_path=source,
        output_path=output,
        iterations=50,
        seed=7,
        modulus=1_000_003,
        spec_checksum=SPEC,
        state_output=state_path,
        state_interval_seconds=1.0,
        state_stride=7,
        clock=lambda: float(next(ticks)),
    )
    final = _decode(state_path.read_bytes(), iterations=50, modulus=1_000_003)
    assert final.step == 50 and final.accumulator == baseline.final_accumulator
    assert stat.S_IMODE(os.stat(state_path).st_mode) == 0o640
    assert [p.name for p in state_path.parent.iterdir() if p.name.startswith(".")] == []

    resume = tmp_path / "restore-state.json"
    resume.write_bytes(
        encode_state(
            CpuState(
                step=20,
                accumulator=_accumulator_after(20),
                input_checksum=INPUT_CHECKSUM,
                spec_checksum=SPEC,
            )
        )
    )
    second = tmp_path / "second" / "result.json"
    resumed = run_cpu_file(
        input_path=source,
        output_path=second,
        iterations=50,
        seed=7,
        modulus=1_000_003,
        spec_checksum=SPEC,
        resume_state=resume,
    )
    assert second.read_bytes() == output.read_bytes() == resumed.result_bytes


def test_entrypoint_rejects_resume_state_bound_to_other_input(tmp_path) -> None:
    source = tmp_path / "input.json"
    source.write_bytes(b'{"initial_value":18}')
    resume = tmp_path / "restore-state.json"
    resume.write_bytes(
        encode_state(
            CpuState(step=1, accumulator=1, input_checksum=INPUT_CHECKSUM, spec_checksum=SPEC)
        )
    )
    with pytest.raises((CpuStateError, CpuInputError)):
        run_cpu_file(
            input_path=source,
            output_path=tmp_path / "result.json",
            iterations=5,
            seed=7,
            modulus=101,
            spec_checksum=SPEC,
            resume_state=resume,
        )
    assert not (tmp_path / "result.json").exists()


def test_entrypoint_rejects_oversized_or_symlinked_resume_state(tmp_path) -> None:
    source = tmp_path / "input.json"
    source.write_bytes(INPUT)
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (STATE_MAX_BYTES + 1))
    link = tmp_path / "link.json"
    link.symlink_to(big)
    for candidate in (big, link):
        with pytest.raises(CpuStateError):
            run_cpu_file(
                input_path=source,
                output_path=tmp_path / "result.json",
                iterations=5,
                seed=7,
                modulus=101,
                spec_checksum=SPEC,
                resume_state=candidate,
            )


def test_state_file_read_rejects_a_fifo_without_blocking(tmp_path) -> None:
    # A workload may replace its state file with a FIFO nobody writes; the
    # runner reads it under its lock, so the open itself must not block.
    fifo = tmp_path / "state.json"
    os.mkfifo(fifo)
    outcome = []

    def read():
        try:
            read_state_file(fifo)
        except CpuStateError as exc:
            outcome.append(exc)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(2)
    blocked = reader.is_alive()
    if blocked:
        # Release the blocked open so the test process never hangs.
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        reader.join(2)
    assert not blocked
    assert len(outcome) == 1
