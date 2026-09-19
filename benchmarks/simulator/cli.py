import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from benchmarks.simulator import SIMULATOR_VERSION
from benchmarks.simulator.baselines import (
    DeficitRoundRobinPolicy,
    DominantResourceFairnessPolicy,
    FifoPolicy,
    RoundRobinPolicy,
    WeightedRoundRobinPolicy,
)
from benchmarks.simulator.engine import Simulator
from benchmarks.simulator.metrics import build_raw_result
from benchmarks.simulator.policy import SchedulerPolicy
from benchmarks.simulator.report import ReportError, comparison_csv, comparison_svg
from benchmarks.simulator.trace import (
    TraceError,
    canonical_json_bytes,
    load_trace,
    materialize_trace,
)

BASELINES: dict[str, Callable[[], SchedulerPolicy]] = {
    "fifo": FifoPolicy,
    "rr": RoundRobinPolicy,
    "wrr": WeightedRoundRobinPolicy,
    "drr": DeficitRoundRobinPolicy,
    "drf": DominantResourceFairnessPolicy,
}


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _json_write(path: Path, value: object) -> None:
    _atomic_write(path, canonical_json_bytes(value) + b"\n")


def _run(trace_path: Path, seed: int, baseline_name: str) -> dict[str, object]:
    try:
        factory = BASELINES[baseline_name]
    except KeyError as exc:
        raise TraceError(f"unknown baseline: {baseline_name}") from exc
    trace = materialize_trace(load_trace(trace_path), seed)
    return build_raw_result(Simulator().run(trace, factory()))


def _parse_seeds(value: str) -> tuple[int, ...]:
    try:
        seeds = tuple(int(item) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("seeds must be comma-separated integers") from exc
    if not seeds:
        raise argparse.ArgumentTypeError("at least one seed is required")
    return seeds


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nexa-b03-simulator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--trace", type=Path, required=True)
    run_parser.add_argument("--seed", type=int, required=True)
    run_parser.add_argument("--baseline", choices=tuple(BASELINES), required=True)
    run_parser.add_argument("--output", type=Path, required=True)

    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--trace", type=Path, required=True)
    compare_parser.add_argument("--seeds", type=_parse_seeds, required=True)
    compare_parser.add_argument("--output", type=Path, required=True)

    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--input", type=Path, required=True)
    report_parser.add_argument("--csv", type=Path, required=True)
    report_parser.add_argument("--svg", type=Path, required=True)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(arguments)
    try:
        if args.command == "run":
            _json_write(args.output, _run(args.trace, args.seed, args.baseline))
            return 0
        if args.command == "compare":
            runs = [
                _run(args.trace, seed, baseline_name)
                for seed in sorted(args.seeds)
                for baseline_name in sorted(BASELINES)
            ]
            bundle = {
                "schema_version": 1,
                "simulator_version": SIMULATOR_VERSION,
                "trace": {
                    "path": args.trace.name,
                    "seeds": list(sorted(args.seeds)),
                    "baselines": list(sorted(BASELINES)),
                },
                "runs": runs,
            }
            _json_write(args.output, bundle)
            return 0
        if args.command == "report":
            bundle = json.loads(args.input.read_text(encoding="utf-8"))
            csv_content = comparison_csv(bundle).encode("utf-8")
            svg_content = comparison_svg(bundle).encode("utf-8")
            _atomic_write(args.csv, csv_content)
            _atomic_write(args.svg, svg_content)
            return 0
    except (OSError, json.JSONDecodeError, TraceError, ReportError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
