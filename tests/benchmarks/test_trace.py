import json
import random
from pathlib import Path

import pytest

from benchmarks.simulator.trace import TraceError, load_trace, materialize_trace


def _write_trace(path: Path, overrides: dict[str, object] | None = None) -> Path:
    trace: dict[str, object] = {
        "version": 1,
        "trace_id": "unit-trace",
        "capacity": {
            "cpu_millis": 2_000,
            "memory_bytes": 4_096,
            "gpu_uuids": ["gpu-b", "gpu-a"],
        },
        "tenants": [
            {
                "tenant_id": "tenant-a",
                "weight": 1,
                "quota": {"cpu_millis": 2_000, "memory_bytes": 4_096, "gpu_count": 2},
                "max_running_jobs": 2,
                "order": 0,
            }
        ],
        "fairness_window": {"start_ms": 0, "end_ms": 10_000, "tenant_ids": ["tenant-a"]},
        "jobs": [
            {
                "job_id": "explicit-1",
                "tenant_id": "tenant-a",
                "ready_sequence": 1,
                "arrival_ms": 0,
                "resources": {"cpu_millis": 500, "memory_bytes": 512, "gpu_count": 0},
                "duration_ms": 1_000,
                "priority": 1,
            }
        ],
        "generator_groups": [
            {
                "group_id": "generated",
                "tenant_id": "tenant-a",
                "count": 4,
                "ready_sequence_start": 10,
                "arrival_start_ms": 0,
                "arrival_step_ms": 1_000,
                "arrival_jitter_ms": 250,
                "duration_choices_ms": [1_000, 5_000],
                "resource_choices": [
                    {"cpu_millis": 500, "memory_bytes": 512, "gpu_count": 0},
                    {"cpu_millis": 1_000, "memory_bytes": 1_024, "gpu_count": 1},
                ],
                "priority": 1,
            }
        ],
    }
    if overrides:
        trace.update(overrides)
    path.write_text(json.dumps(trace), encoding="utf-8")
    return path


def test_same_trace_and_seed_materialize_identically(tmp_path: Path) -> None:
    definition = load_trace(_write_trace(tmp_path / "trace.json"))

    first = materialize_trace(definition, seed=17)
    second = materialize_trace(definition, seed=17)
    other = materialize_trace(definition, seed=23)

    assert first == second
    assert first.materialized_checksum == second.materialized_checksum
    assert first.jobs != other.jobs
    assert first.materialized_checksum != other.materialized_checksum


def test_trace_checksum_ignores_json_key_and_whitespace_order(tmp_path: Path) -> None:
    first_path = _write_trace(tmp_path / "first.json")
    payload = json.loads(first_path.read_text(encoding="utf-8"))
    second_path = tmp_path / "second.json"
    second_path.write_text(json.dumps(payload, indent=4, sort_keys=True), encoding="utf-8")

    assert load_trace(first_path).trace_checksum == load_trace(second_path).trace_checksum


def test_materialization_does_not_use_global_random_state(tmp_path: Path) -> None:
    definition = load_trace(_write_trace(tmp_path / "trace.json"))
    random.seed(101)
    expected = random.random()
    random.seed(101)

    materialize_trace(definition, seed=17)

    assert random.random() == expected


def test_trace_parser_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    path = _write_trace(tmp_path / "trace.json", {"unexpected": True})

    with pytest.raises(TraceError, match="unknown field"):
        load_trace(path)


def test_trace_parser_rejects_unsupported_version(tmp_path: Path) -> None:
    path = _write_trace(tmp_path / "trace.json", {"version": 2})

    with pytest.raises(TraceError, match="version"):
        load_trace(path)


def test_trace_parser_rejects_fairness_window_unknown_tenant(tmp_path: Path) -> None:
    path = _write_trace(
        tmp_path / "trace.json",
        {"fairness_window": {"start_ms": 0, "end_ms": 10, "tenant_ids": ["missing"]}},
    )

    with pytest.raises(TraceError, match="fairness window"):
        load_trace(path)


def test_materialization_rejects_duplicate_job_id(tmp_path: Path) -> None:
    path = _write_trace(
        tmp_path / "trace.json",
        {
            "jobs": [
                {
                    "job_id": "generated-0000",
                    "tenant_id": "tenant-a",
                    "ready_sequence": 1,
                    "arrival_ms": 0,
                    "resources": {
                        "cpu_millis": 500,
                        "memory_bytes": 512,
                        "gpu_count": 0,
                    },
                    "duration_ms": 1_000,
                    "priority": 1,
                }
            ]
        },
    )

    definition = load_trace(path)
    with pytest.raises(TraceError, match="duplicate job ID"):
        materialize_trace(definition, seed=1)


@pytest.mark.parametrize(
    ("field", "value"),
    [("arrival_start_ms", -1), ("count", 0), ("duration_choices_ms", [])],
)
def test_trace_parser_rejects_invalid_generator_bounds(
    tmp_path: Path, field: str, value: object
) -> None:
    path = _write_trace(tmp_path / "trace.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["generator_groups"][0][field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TraceError, match=field):
        load_trace(path)
