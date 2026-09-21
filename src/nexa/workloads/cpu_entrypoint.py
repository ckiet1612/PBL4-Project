"""Trusted-runner-facing CPU workload file adapter."""

import argparse
from pathlib import Path

from .cpu_iterative import CpuIterativeAdapter, CpuResult


def run_cpu_file(
    *,
    input_path: str | Path,
    output_path: str | Path,
    iterations: int,
    seed: int,
    modulus: int,
    spec_checksum: str,
) -> CpuResult:
    source = Path(input_path)
    target = Path(output_path)
    input_bytes = source.read_bytes()
    result = CpuIterativeAdapter().run(
        input_bytes=input_bytes,
        iterations=iterations,
        seed=seed,
        modulus=modulus,
        spec_checksum=spec_checksum,
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
    args = parser.parse_args()
    run_cpu_file(
        input_path=args.input,
        output_path=args.output,
        iterations=args.iterations,
        seed=args.seed,
        modulus=args.modulus,
        spec_checksum=args.spec_checksum,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
