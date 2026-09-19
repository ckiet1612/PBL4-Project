import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRACE = ROOT / "benchmarks/fixtures/small-trace.json"


def _run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "benchmarks.simulator.cli", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_identical_trace_seed_and_baseline_produce_identical_bytes(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    arguments = (
        "run",
        "--trace",
        str(TRACE),
        "--seed",
        "17",
        "--baseline",
        "drr",
    )

    first_run = _run_cli(*arguments, "--output", str(first))
    second_run = _run_cli(*arguments, "--output", str(second))

    assert first_run.returncode == 0, first_run.stderr
    assert second_run.returncode == 0, second_run.stderr
    assert first.read_bytes() == second.read_bytes()


def test_comparison_uses_identical_materialized_workload_for_every_baseline(
    tmp_path: Path,
) -> None:
    output = tmp_path / "comparison.json"

    completed = _run_cli(
        "compare",
        "--trace",
        str(TRACE),
        "--seeds",
        "7,11",
        "--output",
        str(output),
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    runs = payload["runs"]
    assert [(run["provenance"]["seed"], run["baseline"]["name"]) for run in runs] == [
        (7, "drf"),
        (7, "drr"),
        (7, "fifo"),
        (7, "rr"),
        (7, "wrr"),
        (11, "drf"),
        (11, "drr"),
        (11, "fifo"),
        (11, "rr"),
        (11, "wrr"),
    ]
    for seed in (7, 11):
        matching = [run for run in runs if run["provenance"]["seed"] == seed]
        assert len({run["provenance"]["materialized_checksum"] for run in matching}) == 1
        assert len({json.dumps(run["config"], sort_keys=True) for run in matching}) == 1
    checksums = {
        run["provenance"]["materialized_checksum"]
        for run in runs
        if run["baseline"]["name"] == "fifo"
    }
    assert len(checksums) == 2
