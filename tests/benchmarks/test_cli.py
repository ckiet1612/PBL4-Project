import csv
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
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


def test_run_command_writes_canonical_raw_result(tmp_path: Path) -> None:
    output = tmp_path / "run.json"

    completed = _run_cli(
        "run",
        "--trace",
        str(TRACE),
        "--seed",
        "17",
        "--baseline",
        "fifo",
        "--output",
        str(output),
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["baseline"]["name"] == "fifo"
    assert payload["provenance"]["seed"] == 17
    assert output.read_bytes().endswith(b"\n")


def test_report_command_generates_parseable_csv_and_svg(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    csv_path = tmp_path / "comparison.csv"
    svg_path = tmp_path / "comparison.svg"
    comparison = _run_cli(
        "compare",
        "--trace",
        str(TRACE),
        "--seeds",
        "7,11",
        "--output",
        str(bundle),
    )
    assert comparison.returncode == 0, comparison.stderr

    report = _run_cli(
        "report",
        "--input",
        str(bundle),
        "--csv",
        str(csv_path),
        "--svg",
        str(svg_path),
    )

    assert report.returncode == 0, report.stderr
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 10
    assert {row["baseline"] for row in rows} == {"fifo", "rr", "wrr", "drr", "drf"}
    root = ET.parse(svg_path).getroot()
    assert root.tag.endswith("svg")
    rendered_text = " ".join(element.text or "" for element in root.iter())
    assert "Max wait (ms)" in rendered_text
    assert "Per-tenant averages" in rendered_text
    assert "Allocation timeline (seed 7)" in rendered_text
    assert "Weighted Jain" in rendered_text
    assert len([element for element in root.iter() if element.tag.endswith("line")]) >= 12
    tenant_rows = [
        element for element in root.iter() if element.attrib.get("data-kind") == "tenant-summary"
    ]
    assert {element.attrib["data-tenant"] for element in tenant_rows} == {
        "tenant-a",
        "tenant-b",
    }
    assert all("data-p95-wait-ms" in element.attrib for element in tenant_rows)
    assert all("data-dominant-resource-time" in element.attrib for element in tenant_rows)
    assert all("/" not in "".join(element.itertext()) for element in tenant_rows)
    axis_maximum_labels = [
        element for element in root.iter() if element.attrib.get("data-kind") == "axis-maximum"
    ]
    assert len(axis_maximum_labels) == 4
    assert all("data-exact-value" in element.attrib for element in axis_maximum_labels)
    assert all("/" not in (element.text or "") for element in axis_maximum_labels)
    assert all(len(element.text or "") <= 12 for element in axis_maximum_labels)
    allocation_bars = [
        element
        for element in root.iter()
        if element.attrib.get("data-kind") == "allocation-interval"
    ]
    assert allocation_bars
    assert all(
        int(element.attrib["data-release-ms"]) > int(element.attrib["data-start-ms"])
        for element in allocation_bars
    )


def test_report_renders_undefined_jain_as_na(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle.json"
    csv_path = tmp_path / "comparison.csv"
    svg_path = tmp_path / "comparison.svg"
    comparison = _run_cli(
        "compare",
        "--trace",
        str(TRACE),
        "--seeds",
        "7,11",
        "--output",
        str(bundle_path),
    )
    assert comparison.returncode == 0, comparison.stderr
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert all(run["fairness"]["weighted_jain"] is None for run in bundle["runs"])
    next(
        run
        for run in bundle["runs"]
        if run["baseline"]["name"] == "fifo" and run["provenance"]["seed"] == 7
    )["fairness"]["weighted_jain"] = "1"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    report = _run_cli(
        "report",
        "--input",
        str(bundle_path),
        "--csv",
        str(csv_path),
        "--svg",
        str(svg_path),
    )

    assert report.returncode == 0, report.stderr
    root = ET.parse(svg_path).getroot()
    undefined_jain = [
        element
        for element in root.iter()
        if element.attrib.get("data-kind") == "undefined-metric"
        and element.attrib.get("data-metric") == "jain"
    ]
    assert {element.attrib["data-baseline"] for element in undefined_jain} == {
        "fifo",
        "rr",
        "wrr",
        "drr",
        "drf",
    }
    assert all(element.text == "N/A" for element in undefined_jain)


def test_invalid_cli_input_returns_nonzero_without_output(tmp_path: Path) -> None:
    output = tmp_path / "invalid.json"

    completed = _run_cli(
        "run",
        "--trace",
        str(TRACE),
        "--seed",
        "not-an-integer",
        "--baseline",
        "unknown",
        "--output",
        str(output),
    )

    assert completed.returncode != 0
    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_invalid_report_bundle_leaves_no_partial_outputs(tmp_path: Path) -> None:
    bundle = tmp_path / "invalid-bundle.json"
    csv_path = tmp_path / "comparison.csv"
    svg_path = tmp_path / "comparison.svg"
    bundle.write_text('{"runs":[]}', encoding="utf-8")

    completed = _run_cli(
        "report",
        "--input",
        str(bundle),
        "--csv",
        str(csv_path),
        "--svg",
        str(svg_path),
    )

    assert completed.returncode != 0
    assert not csv_path.exists()
    assert not svg_path.exists()
    assert not list(tmp_path.glob("*.tmp"))
