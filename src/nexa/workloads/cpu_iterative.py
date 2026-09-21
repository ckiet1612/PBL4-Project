"""Deterministic CPU iterative workload adapter (contract version 1)."""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass


class CpuInputError(ValueError):
    pass


_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class CpuResult:
    iterations: int
    final_accumulator: int
    input_checksum: str
    spec_checksum: str
    result_bytes: bytes


class CpuIterativeAdapter:
    media_type = "application/vnd.nexa.cpu-iterative-input+json"
    result_media_type = "application/vnd.nexa.cpu-iterative-result+json"

    def validate_input(self, input_bytes: bytes) -> int:
        try:
            payload = json.loads(input_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CpuInputError("input is not valid UTF-8 JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {"initial_value"}:
            raise CpuInputError("input must contain exactly initial_value")
        initial_value = payload["initial_value"]
        if (
            isinstance(initial_value, bool)
            or not isinstance(initial_value, int)
            or not 0 <= initial_value <= 2_147_483_647
        ):
            raise CpuInputError("initial_value is outside the contract range")
        return initial_value

    def run(
        self,
        *,
        input_bytes: bytes,
        iterations: int,
        seed: int,
        modulus: int,
        spec_checksum: str,
        progress: Callable[[int, int], None] | None = None,
    ) -> CpuResult:
        initial_value = self.validate_input(input_bytes)
        if (
            isinstance(iterations, bool)
            or not isinstance(iterations, int)
            or not 1 <= iterations <= 1_000_000_000
        ):
            raise CpuInputError("iterations is outside the contract range")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2_147_483_647:
            raise CpuInputError("seed is outside the contract range")
        if (
            isinstance(modulus, bool)
            or not isinstance(modulus, int)
            or not 2 <= modulus <= 2_147_483_647
        ):
            raise CpuInputError("modulus is outside the contract range")
        if not _CHECKSUM.fullmatch(spec_checksum):
            raise CpuInputError("spec_checksum is invalid")

        accumulator = (initial_value + seed) % modulus
        for step in range(iterations):
            accumulator = (accumulator * 1_664_525 + 1_013_904_223 + step) % modulus
            if progress is not None:
                progress(step + 1, iterations)
        input_checksum = "sha256:" + hashlib.sha256(input_bytes).hexdigest()
        result = {
            "iterations": iterations,
            "final_accumulator": accumulator,
            "input_checksum": input_checksum,
            "spec_checksum": spec_checksum,
        }
        result_bytes = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return CpuResult(
            iterations=iterations,
            final_accumulator=accumulator,
            input_checksum=input_checksum,
            spec_checksum=spec_checksum,
            result_bytes=result_bytes,
        )


__all__ = ["CpuInputError", "CpuIterativeAdapter", "CpuResult"]
