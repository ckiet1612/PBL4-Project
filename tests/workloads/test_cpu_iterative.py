import hashlib
import json

import pytest

from nexa.workloads.cpu_iterative import CpuInputError, CpuIterativeAdapter


def test_cpu_iterative_matches_known_oracle_and_exact_result_fields() -> None:
    adapter = CpuIterativeAdapter()
    result = adapter.run(
        input_bytes=b'{"initial_value":17}',
        iterations=3,
        seed=7,
        modulus=1_000_000_007,
        spec_checksum="sha256:" + "c" * 64,
    )
    assert result.final_accumulator == 915488392
    assert list(json.loads(result.result_bytes)) == [
        "iterations",
        "final_accumulator",
        "input_checksum",
        "spec_checksum",
    ]
    assert result.input_checksum == "sha256:" + hashlib.sha256(b'{"initial_value":17}').hexdigest()


def test_cpu_iterative_is_deterministic_and_reports_progress() -> None:
    adapter = CpuIterativeAdapter()
    progress: list[tuple[int, int]] = []
    result1 = adapter.run(
        input_bytes=b'{"initial_value":1}',
        iterations=5,
        seed=0,
        modulus=97,
        spec_checksum="sha256:" + "d" * 64,
        progress=lambda step, total: progress.append((step, total)),
    )
    result2 = adapter.run(
        input_bytes=b'{"initial_value":1}',
        iterations=5,
        seed=0,
        modulus=97,
        spec_checksum="sha256:" + "d" * 64,
    )
    assert result1.result_bytes == result2.result_bytes
    assert progress == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]


@pytest.mark.parametrize(
    "input_bytes,iterations,seed,modulus",
    [
        (b'{"initial_value":-1}', 1, 0, 97),
        (b'{"initial_value":1,"extra":2}', 1, 0, 97),
        (b"not-json", 1, 0, 97),
        (b'{"initial_value":1}', 0, 0, 97),
        (b'{"initial_value":1}', 1, 0, 1),
    ],
)
def test_cpu_iterative_rejects_invalid_input_or_parameters(
    input_bytes, iterations, seed, modulus
) -> None:
    with pytest.raises(CpuInputError):
        CpuIterativeAdapter().run(
            input_bytes=input_bytes,
            iterations=iterations,
            seed=seed,
            modulus=modulus,
            spec_checksum="sha256:" + "e" * 64,
        )
