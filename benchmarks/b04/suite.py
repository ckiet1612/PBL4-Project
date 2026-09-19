import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SuiteError(ValueError):
    """Raised when the fixed B04 benchmark suite is malformed."""


@dataclass(frozen=True, slots=True)
class SuiteProfile:
    profile_id: str
    trace_path: Path
    profile_kind: str
    valid_fairness: bool
    jain_threshold: str | None


@dataclass(frozen=True, slots=True)
class BenchmarkSuite:
    version: int
    policy_version: str
    seeds: tuple[int, ...]
    profiles: tuple[SuiteProfile, ...]


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SuiteError(f"{label} must be an object")
    return value


def _closed(value: Mapping[str, Any], required: frozenset[str], label: str) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required)
    if missing:
        raise SuiteError(f"{label} is missing field(s): {', '.join(missing)}")
    if unknown:
        raise SuiteError(f"{label} has unknown field(s): {', '.join(unknown)}")


def load_suite(path: Path) -> BenchmarkSuite:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SuiteError(f"cannot read suite: {path.name}") from exc
    data = _mapping(raw, "suite")
    _closed(data, frozenset({"version", "policy_version", "seeds", "profiles"}), "suite")
    if data["version"] != 1:
        raise SuiteError("suite.version must be 1")
    if not isinstance(data["policy_version"], str) or not data["policy_version"]:
        raise SuiteError("suite.policy_version must be a non-empty string")
    if not isinstance(data["seeds"], list) or not data["seeds"]:
        raise SuiteError("suite.seeds must be a non-empty array")
    seeds = tuple(data["seeds"])
    if any(type(seed) is not int for seed in seeds) or len(set(seeds)) != len(seeds):
        raise SuiteError("suite.seeds must contain unique integers")
    if not isinstance(data["profiles"], list) or not data["profiles"]:
        raise SuiteError("suite.profiles must be a non-empty array")

    profiles: list[SuiteProfile] = []
    required = frozenset(
        {"profile_id", "trace", "profile_kind", "valid_fairness", "jain_threshold"}
    )
    for index, raw_profile in enumerate(data["profiles"]):
        profile = _mapping(raw_profile, f"suite.profiles[{index}]")
        _closed(profile, required, f"suite.profiles[{index}]")
        profile_id = profile["profile_id"]
        trace_name = profile["trace"]
        profile_kind = profile["profile_kind"]
        valid_fairness = profile["valid_fairness"]
        threshold = profile["jain_threshold"]
        if not isinstance(profile_id, str) or not profile_id:
            raise SuiteError("profile_id must be a non-empty string")
        if not isinstance(trace_name, str) or Path(trace_name).name != trace_name:
            raise SuiteError("profile trace must be a local fixture filename")
        if profile_kind not in {"fairness", "reservation", "diagnostic"}:
            raise SuiteError("profile_kind is invalid")
        if type(valid_fairness) is not bool:
            raise SuiteError("valid_fairness must be boolean")
        if valid_fairness and (not isinstance(threshold, str) or not threshold):
            raise SuiteError("valid fairness profile requires jain_threshold")
        if not valid_fairness and threshold is not None:
            raise SuiteError("non-fairness profile must not set jain_threshold")
        profiles.append(
            SuiteProfile(
                profile_id=profile_id,
                trace_path=path.parent / trace_name,
                profile_kind=profile_kind,
                valid_fairness=valid_fairness,
                jain_threshold=threshold,
            )
        )
    if len({profile.profile_id for profile in profiles}) != len(profiles):
        raise SuiteError("suite contains duplicate profile IDs")
    return BenchmarkSuite(1, data["policy_version"], seeds, tuple(profiles))
