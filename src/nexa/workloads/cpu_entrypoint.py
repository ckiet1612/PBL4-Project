"""Trusted-runner-facing CPU workload file adapter."""

import argparse
import hashlib
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from .cpu_iterative import CpuIterativeAdapter, CpuResult
from .cpu_state import CpuState, decode_state, encode_state, read_state_file


def _write_state(path: Path, payload: bytes) -> None:
    # Atomic replace keeps the runner from ever observing a torn snapshot.
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_cpu_file(
    *,
    input_path: str | Path,
    output_path: str | Path,
    iterations: int,
    seed: int,
    modulus: int,
    spec_checksum: str,
    state_output: str | Path | None = None,
    resume_state: str | Path | None = None,
    state_interval_seconds: float = 1.0,
    state_stride: int = 65_536,
    clock: Callable[[], float] = time.monotonic,
) -> CpuResult:
    source = Path(input_path)
    target = Path(output_path)
    input_bytes = source.read_bytes()
    input_checksum = "sha256:" + hashlib.sha256(input_bytes).hexdigest()
    resume = None
    if resume_state is not None:
        # Worker and runner already verified these bytes; the workload re-checks
        # them against the input it actually reads before any compute.
        resume = decode_state(
            read_state_file(resume_state),
            iterations=iterations,
            modulus=modulus,
            input_checksum=input_checksum,
            spec_checksum=spec_checksum,
        )
    observer = None
    if state_output is not None:
        state_path = Path(state_output)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        last_written: list[float] = []

        def observer(step: int, accumulator: int) -> None:
            now = clock()
            if (
                last_written
                and step != iterations
                and now - last_written[0] < state_interval_seconds
            ):
                return
            _write_state(
                state_path,
                encode_state(
                    CpuState(
                        step=step,
                        accumulator=accumulator,
                        input_checksum=input_checksum,
                        spec_checksum=spec_checksum,
                    )
                ),
            )
            last_written[:] = [now]

    result = CpuIterativeAdapter().run(
        input_bytes=input_bytes,
        iterations=iterations,
        seed=seed,
        modulus=modulus,
        spec_checksum=spec_checksum,
        resume=resume,
        observer=observer,
        observer_stride=state_stride,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(result.result_bytes)
    return result


__all__ = ["run_cpu_file"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the allowlisted CPU iterative workload")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--iterations", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--modulus", type=int, required=True)
    parser.add_argument("--spec-checksum", required=True)
    parser.add_argument("--state-output")
    parser.add_argument("--resume-state")
    args = parser.parse_args()
    run_cpu_file(
        input_path=args.input,
        output_path=args.output,
        iterations=args.iterations,
        seed=args.seed,
        modulus=args.modulus,
        spec_checksum=args.spec_checksum,
        state_output=args.state_output,
        resume_state=args.resume_state,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
