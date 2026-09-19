import json
from pathlib import Path

import pytest

from benchmarks.b04.cli import compare_suite
from benchmarks.b04.suite import SuiteError, load_suite
from benchmarks.simulator.trace import canonical_json_bytes

_SUITE = Path("benchmarks/fixtures/b04-suite.json")


def test_suite_freezes_profiles_seeds_policy_and_threshold_before_measurement() -> None:
    suite = load_suite(_SUITE)

    assert suite.version == 1
    assert suite.policy_version == "1"
    assert suite.seeds == (7, 11, 19, 23, 29)
    assert tuple(profile.profile_id for profile in suite.profiles) == (
        "uniform-equal",
        "mixed-equal",
        "weighted-124",
        "gpu-slots",
        "large-reservation",
        "constrained-diagnostic",
    )
    assert all(profile.jain_threshold == "19/20" for profile in suite.profiles[:4])
    assert not suite.profiles[-1].valid_fairness


def test_compare_uses_one_materialized_workload_for_all_policies() -> None:
    bundle = compare_suite(_SUITE, profile_ids=("uniform-equal",), seeds=(7,))

    assert len(bundle["runs"]) == 6
    assert {run["baseline"]["name"] for run in bundle["runs"]} == {
        "nexa",
        "fifo",
        "rr",
        "wrr",
        "drr",
        "drf",
    }
    assert len({run["provenance"]["materialized_checksum"] for run in bundle["runs"]}) == 1
    product = next(run for run in bundle["runs"] if run["baseline"]["name"] == "nexa")
    assert product["policy_state"]["tenant_ledgers"]
    assert product["decision_timeline"]
    assert product["accounting_timeline"]
    assert product["config"]["user_mapping"] == [
        {
            "tenant_id": "tenant-a",
            "user_id": "tenant-a:user",
            "max_active_attempts": 4,
        },
        {
            "tenant_id": "tenant-b",
            "user_id": "tenant-b:user",
            "max_active_attempts": 4,
        },
        {
            "tenant_id": "tenant-c",
            "user_id": "tenant-c:user",
            "max_active_attempts": 4,
        },
    ]
    assert product["accounting_timeline_schema"] == {
        "row_fields": [
            "interval_start_ms",
            "interval_end_ms",
            "held_resources_by_tenant",
            "dominant_shares",
            "tenant_ledgers",
            "virtual_floor",
            "eligible_tenant_ids",
        ],
        "held_resource_fields": [
            "tenant_id",
            "cpu_millis",
            "memory_bytes",
            "gpu_count",
        ],
        "dominant_share_fields": ["tenant_id", "dominant_share"],
        "tenant_ledger_fields": [
            "tenant_id",
            "virtual_score",
            "accounted_through_ms",
            "had_eligible_demand",
        ],
    }
    accounting = product["accounting_timeline"][0]
    assert len(accounting) == 7
    assert all(len(resources) == 4 for resources in accounting[2])
    assert all(len(share) == 2 for share in accounting[3])
    assert all(len(ledger) == 4 for ledger in accounting[4])
    assert any(
        row[6] == ["tenant-a", "tenant-b", "tenant-c"] for row in product["accounting_timeline"]
    )
    decision = product["decision_timeline"][0]
    assert set(decision) == {
        "time_ms",
        "decision_type",
        "job_id",
        "tenant_id",
        "reason",
        "reservation_id",
        "fitting_other_candidate_ids",
        "eligible_wait_seconds",
        "effective_priority",
        "virtual_floor",
        "tenant_states",
        "selected_tenant_state",
    }
    assert product["decision_timeline_schema"]["decision_fields"] == [
        "time_ms",
        "decision_type",
        "job_id",
        "tenant_id",
        "reason",
        "reservation_id",
        "fitting_other_candidate_ids",
        "eligible_wait_seconds",
        "effective_priority",
        "virtual_floor",
        "tenant_states",
        "selected_tenant_state",
    ]
    assert isinstance(decision["fitting_other_candidate_ids"], list)
    assert product["decision_timeline_schema"]["tenant_state_fields"] == [
        "tenant_id",
        "virtual_score",
        "dominant_share",
        "weight",
        "held_resources",
        "oldest_ready_sequence",
        "normal_candidate_count",
        "eligible_candidate_count",
        "fitting_candidate_count",
        "ineligibility_reason_counts",
    ]
    assert len(decision["tenant_states"][0]) == 10
    selected = decision["selected_tenant_state"]
    assert set(selected) == {
        "normal_candidate_ids",
        "oldest_eligible_job_id",
        "eligible_candidate_ids",
        "fitting_candidate_ids",
        "continuation_cursor",
        "candidate_states",
    }
    assert product["decision_timeline_schema"]["candidate_state_fields"] == [
        "job_id",
        "eligible_wait_seconds",
        "effective_priority",
        "eligibility_reason",
        "fits_free_resources",
    ]
    assert len(selected["candidate_states"][0]) == 5
    assert "event_timeline" not in product
    assert "dispatch_timeline" not in product
    assert set(product["jobs"][0]) == {
        "job_id",
        "tenant_id",
        "ready_sequence",
        "arrival_ms",
        "resources",
        "duration_ms",
        "state",
        "dispatch_ms",
        "completion_ms",
        "wait_ms",
        "infeasible_reason",
    }
    assert bundle["policy_contract_checks"]["aging"] == [
        {"eligible_wait_seconds": 59, "effective_priority": 0},
        {"eligible_wait_seconds": 60, "effective_priority": 1},
        {"eligible_wait_seconds": 119, "effective_priority": 1},
        {"eligible_wait_seconds": 120, "effective_priority": 2},
        {"eligible_wait_seconds": 121, "effective_priority": 2},
    ]
    assert {item["reason"] for item in bundle["policy_contract_checks"]["invalidation"]} == {
        "cancelled",
        "no_longer_eligible",
        "policy_incompatible",
        "capability_incompatible",
    }
    assert bundle["policy_contract_checks"]["reservation_boundary"] == [
        {"eligible_wait_seconds": 119, "decision_type": "dispatch"},
        {"eligible_wait_seconds": 120, "decision_type": "create_reservation"},
    ]


def test_selected_comparison_is_byte_deterministic() -> None:
    first = compare_suite(_SUITE, profile_ids=("large-reservation",), seeds=(7,))
    second = compare_suite(_SUITE, profile_ids=("large-reservation",), seeds=(7,))

    assert canonical_json_bytes(first) == canonical_json_bytes(second)


def test_compare_serializes_candidate_evidence_for_reservation_drain() -> None:
    bundle = compare_suite(_SUITE, profile_ids=("large-reservation",), seeds=(7,))
    product = next(run for run in bundle["runs"] if run["baseline"]["name"] == "nexa")
    drained = next(
        event
        for event in product["decision_timeline"]
        if event["decision_type"] == "drain_for_reservation"
        and event["fitting_other_candidate_ids"]
    )

    assert product["decision_timeline_schema"]["reservation_drain_candidate_state_fields"] == [
        "job_id",
        "tenant_id",
        "eligibility_reason",
        "fits_free_resources",
    ]
    candidate_states = drained["reservation_drain_candidate_states"]
    assert all(len(state) == 4 for state in candidate_states)
    assert all(state[2] is None and state[3] is True for state in candidate_states)
    assert set(drained["fitting_other_candidate_ids"]) == {
        state[0] for state in candidate_states if state[2] is None and state[3] is True
    }


def test_compare_rejects_suite_policy_version_mismatch(tmp_path: Path) -> None:
    suite = json.loads(_SUITE.read_text(encoding="utf-8"))
    suite["policy_version"] = "999"
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps(suite), encoding="utf-8")

    with pytest.raises(SuiteError, match="policy version"):
        compare_suite(suite_path, profile_ids=("uniform-equal",), seeds=(7,))
