import copy
import csv
import io

from benchmarks.b04.report import comparison_csv, comparison_svg


def _run(
    profile_id: str,
    policy: str,
    seed: int,
    jain: str | None,
    *,
    reservation: bool = False,
) -> dict[str, object]:
    profile_kind = "reservation" if reservation else "diagnostic"
    if not reservation and profile_id != "diagnostic":
        profile_kind = "fairness"
    decision_timeline = []
    jobs = []
    if reservation:
        decision_timeline = [
            {
                "time_ms": 121000,
                "decision_type": "create_reservation",
                "job_id": "large",
                "tenant_id": "large-tenant",
                "reason": "eligible_wait_threshold",
                "reservation_id": "reservation-1",
                "eligible_wait_seconds": 120,
            },
            {
                "time_ms": 150000,
                "decision_type": "drain_for_reservation",
                "job_id": "large",
                "tenant_id": "large-tenant",
                "reason": "waiting_for_free_resources",
                "reservation_id": "reservation-1",
                "fitting_other_candidate_ids": ["small-ready"],
                "reservation_drain_candidate_states": [
                    ["large", "large-tenant", None, False],
                    ["small-ready", "small-tenant", None, True],
                ],
            },
            {
                "time_ms": 200000,
                "decision_type": "dispatch",
                "job_id": "large",
                "tenant_id": "large-tenant",
                "reason": "reservation_fit",
                "reservation_id": "reservation-1",
            },
        ]
        jobs = [
            {"job_id": "held-a", "tenant_id": "small-tenant", "arrival_ms": 0},
            {"job_id": "held-b", "tenant_id": "small-tenant", "arrival_ms": 0},
            {"job_id": "large", "tenant_id": "large-tenant", "arrival_ms": 1000},
            {"job_id": "small-ready", "tenant_id": "small-tenant", "arrival_ms": 130000},
            {"job_id": "small-after", "tenant_id": "small-tenant", "arrival_ms": 210000},
        ]
    return {
        "profile_id": profile_id,
        "profile_kind": profile_kind,
        "valid_fairness": profile_id == "uniform",
        "jain_threshold": "19/20" if profile_id == "uniform" else None,
        "baseline": {"name": policy, "cost": {"candidate_evaluations": 10}},
        "provenance": {"seed": seed, "materialized_checksum": "sha256:x"},
        "fairness": {
            "weighted_jain": jain,
            "included_tenants": ["a", "b"] if jain is not None else [],
            "excluded_tenants": {} if jain is not None else {"a": "quota_limited"},
        },
        "metrics": {
            "p95_wait_ms": 20,
            "max_wait_ms": 30,
            "throughput_jobs_per_second": "2",
        },
        "counts": {"accepted": 2, "completed": 2},
        "starvation": {"undispatched_job_ids": []},
        "invariant_violations": [],
        "decision_timeline": decision_timeline,
        "jobs": jobs,
        "allocation_timeline": (
            [
                {"job_id": "held-a", "start_ms": 0, "release_ms": 150000, "gpu_uuids": []},
                {"job_id": "held-b", "start_ms": 0, "release_ms": 200000, "gpu_uuids": []},
            ]
            if reservation
            else []
        ),
    }


def _bundle() -> dict[str, object]:
    return {
        "schema_version": 1,
        "suite": {"seeds": [7], "profiles": ["uniform", "diagnostic", "reservation"]},
        "runs": [
            _run("uniform", "nexa", 7, "1"),
            _run("uniform", "fifo", 7, "3/4"),
            _run("diagnostic", "nexa", 7, None),
            _run("reservation", "nexa", 7, None, reservation=True),
        ],
    }


def test_csv_preserves_each_seed_and_undefined_jain() -> None:
    content = comparison_csv(_bundle())
    rows = list(csv.DictReader(io.StringIO(content)))

    assert len(rows) == 4
    diagnostic = next(row for row in rows if row["profile_id"] == "diagnostic")
    assert diagnostic["weighted_jain"] == "N/A"
    assert diagnostic["fairness_status"] == "not_applicable"
    assert diagnostic["profile_status"] == "pass"
    product = next(
        row for row in rows if row["profile_id"] == "uniform" and row["policy"] == "nexa"
    )
    assert product["fairness_status"] == "pass"
    assert product["profile_status"] == "pass"
    reservation = next(row for row in rows if row["profile_id"] == "reservation")
    assert reservation["fairness_status"] == "not_applicable"
    assert reservation["profile_status"] == "pass"


def test_profile_status_fails_when_non_jain_rule_fails() -> None:
    bundle = copy.deepcopy(_bundle())
    uniform = next(
        run
        for run in bundle["runs"]
        if run["profile_id"] == "uniform" and run["baseline"]["name"] == "nexa"
    )
    uniform["starvation"]["undispatched_job_ids"] = ["job-late"]
    reservation = next(run for run in bundle["runs"] if run["profile_id"] == "reservation")
    reservation["decision_timeline"] = [
        event
        for event in reservation["decision_timeline"]
        if event["decision_type"] != "drain_for_reservation"
    ]

    rows = list(csv.DictReader(io.StringIO(comparison_csv(bundle))))

    uniform_row = next(
        row for row in rows if row["profile_id"] == "uniform" and row["policy"] == "nexa"
    )
    assert uniform_row["fairness_status"] == "pass"
    assert uniform_row["profile_status"] == "fail"
    assert (
        next(row for row in rows if row["profile_id"] == "reservation")["profile_status"] == "fail"
    )


def test_reservation_profile_rejects_creation_before_eligible_age_threshold() -> None:
    bundle = copy.deepcopy(_bundle())
    reservation = next(run for run in bundle["runs"] if run["profile_id"] == "reservation")
    created = next(
        event
        for event in reservation["decision_timeline"]
        if event["decision_type"] == "create_reservation"
    )
    created["time_ms"] = 1000
    created["eligible_wait_seconds"] = 0

    row = next(
        row
        for row in csv.DictReader(io.StringIO(comparison_csv(bundle)))
        if row["profile_id"] == "reservation"
    )

    assert row["profile_status"] == "fail"


def test_reservation_profile_rejects_unexpected_creation_reason() -> None:
    bundle = copy.deepcopy(_bundle())
    reservation = next(run for run in bundle["runs"] if run["profile_id"] == "reservation")
    created = next(
        event
        for event in reservation["decision_timeline"]
        if event["decision_type"] == "create_reservation"
    )
    created["reason"] = "arrival_age_threshold"

    row = next(
        row
        for row in csv.DictReader(io.StringIO(comparison_csv(bundle)))
        if row["profile_id"] == "reservation"
    )

    assert row["profile_status"] == "fail"


def test_reservation_profile_rejects_drain_without_withholding_a_fitting_job() -> None:
    bundle = copy.deepcopy(_bundle())
    reservation = next(run for run in bundle["runs"] if run["profile_id"] == "reservation")
    drained = next(
        event
        for event in reservation["decision_timeline"]
        if event["decision_type"] == "drain_for_reservation"
    )
    drained["fitting_other_candidate_ids"] = []

    row = next(
        row
        for row in csv.DictReader(io.StringIO(comparison_csv(bundle)))
        if row["profile_id"] == "reservation"
    )

    assert row["profile_status"] == "fail"


def test_reservation_profile_rejects_fabricated_withheld_candidate_id() -> None:
    bundle = copy.deepcopy(_bundle())
    reservation = next(run for run in bundle["runs"] if run["profile_id"] == "reservation")
    drained = next(
        event
        for event in reservation["decision_timeline"]
        if event["decision_type"] == "drain_for_reservation"
    )
    drained["fitting_other_candidate_ids"] = ["fabricated"]

    row = next(
        row
        for row in csv.DictReader(io.StringIO(comparison_csv(bundle)))
        if row["profile_id"] == "reservation"
    )

    assert row["profile_status"] == "fail"


def test_reservation_profile_rejects_non_staggered_releases() -> None:
    bundle = copy.deepcopy(_bundle())
    reservation = next(run for run in bundle["runs"] if run["profile_id"] == "reservation")
    for allocation in reservation["allocation_timeline"]:
        allocation["release_ms"] = 200000

    row = next(
        row
        for row in csv.DictReader(io.StringIO(comparison_csv(bundle)))
        if row["profile_id"] == "reservation"
    )

    assert row["profile_status"] == "fail"


def test_reservation_profile_rejects_unrelated_post_create_release() -> None:
    bundle = copy.deepcopy(_bundle())
    reservation = next(run for run in bundle["runs"] if run["profile_id"] == "reservation")
    reservation["allocation_timeline"][0]["release_ms"] = 200000
    reservation["allocation_timeline"][1] = {
        "job_id": "unrelated",
        "start_ms": 160000,
        "release_ms": 180000,
        "gpu_uuids": [],
    }

    row = next(
        row
        for row in csv.DictReader(io.StringIO(comparison_csv(bundle)))
        if row["profile_id"] == "reservation"
    )

    assert row["profile_status"] == "fail"


def test_reservation_profile_rejects_arrivals_that_stop_before_dispatch() -> None:
    bundle = copy.deepcopy(_bundle())
    reservation = next(run for run in bundle["runs"] if run["profile_id"] == "reservation")
    reservation["jobs"] = [job for job in reservation["jobs"] if job["arrival_ms"] <= 200000]

    row = next(
        row
        for row in csv.DictReader(io.StringIO(comparison_csv(bundle)))
        if row["profile_id"] == "reservation"
    )

    assert row["profile_status"] == "fail"


def test_gpu_profile_status_detects_overlapping_uuid_allocations() -> None:
    run = _run("gpu-slots", "nexa", 7, "1")
    run["valid_fairness"] = True
    run["jain_threshold"] = "19/20"
    run["allocation_timeline"] = [
        {"job_id": "a", "start_ms": 0, "release_ms": 10, "gpu_uuids": ["GPU-a"]},
        {"job_id": "b", "start_ms": 5, "release_ms": 15, "gpu_uuids": ["GPU-a"]},
    ]

    row = next(csv.DictReader(io.StringIO(comparison_csv({"runs": [run]}))))

    assert row["fairness_status"] == "pass"
    assert row["profile_status"] == "fail"


def test_svg_marks_na_and_draws_reservation_timeline_from_raw_events() -> None:
    content = comparison_svg(_bundle())

    assert 'data-jain="N/A"' in content
    assert 'data-kind="reservation-event"' in content
    assert 'data-job-id="large"' in content
    assert "B04 fairness, aging, and reservation" in content


def test_report_rendering_is_deterministic() -> None:
    assert comparison_csv(_bundle()) == comparison_csv(_bundle())
    assert comparison_svg(_bundle()) == comparison_svg(_bundle())
