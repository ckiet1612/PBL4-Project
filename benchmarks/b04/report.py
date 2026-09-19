import csv
import io
from fractions import Fraction
from html import escape


class ReportError(ValueError):
    """Raised when a B04 raw bundle cannot be rendered safely."""


def _runs(bundle: object) -> list[dict[str, object]]:
    if not isinstance(bundle, dict) or not isinstance(bundle.get("runs"), list):
        raise ReportError("report input must contain a runs array")
    runs = bundle["runs"]
    if not all(isinstance(run, dict) for run in runs):
        raise ReportError("report runs must be objects")
    return runs


def _fraction(value: object) -> Fraction | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReportError("fraction value must be a string or null")
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ReportError("fraction value is invalid") from exc


def _fairness_status(run: dict[str, object]) -> str:
    if not run.get("valid_fairness"):
        return "not_applicable"
    fairness = run.get("fairness")
    if not isinstance(fairness, dict):
        raise ReportError("run fairness must be an object")
    value = _fraction(fairness.get("weighted_jain"))
    threshold = _fraction(run.get("jain_threshold"))
    if value is None or threshold is None:
        return "fail"
    return "pass" if value >= threshold else "fail"


def _run_has_complete_invariants(run: dict[str, object]) -> bool:
    violations = run.get("invariant_violations")
    starvation = run.get("starvation")
    counts = run.get("counts")
    if not isinstance(violations, list) or not isinstance(starvation, dict):
        raise ReportError("run invariant fields are malformed")
    if not isinstance(starvation.get("undispatched_job_ids"), list) or not isinstance(counts, dict):
        raise ReportError("run completion fields are malformed")
    accepted = counts.get("accepted")
    completed = counts.get("completed")
    return (
        not violations
        and not starvation["undispatched_job_ids"]
        and isinstance(accepted, int)
        and completed == accepted
    )


def _gpu_allocations_are_exclusive(run: dict[str, object]) -> bool:
    allocations = run.get("allocation_timeline")
    if not isinstance(allocations, list):
        raise ReportError("run allocation timeline must be an array")
    intervals_by_uuid: dict[str, list[tuple[int, int]]] = {}
    for allocation in allocations:
        if not isinstance(allocation, dict):
            raise ReportError("allocation timeline entries must be objects")
        start_ms = allocation.get("start_ms")
        release_ms = allocation.get("release_ms")
        gpu_uuids = allocation.get("gpu_uuids")
        if not isinstance(start_ms, int) or not isinstance(release_ms, int):
            raise ReportError("allocation interval must use integer timestamps")
        if not isinstance(gpu_uuids, list) or not all(isinstance(item, str) for item in gpu_uuids):
            raise ReportError("allocation GPU UUIDs must be strings")
        for gpu_uuid in gpu_uuids:
            intervals_by_uuid.setdefault(gpu_uuid, []).append((start_ms, release_ms))
    for intervals in intervals_by_uuid.values():
        ordered = sorted(intervals)
        if any(
            previous_end > current_start
            for (_, previous_end), (current_start, _) in zip(ordered, ordered[1:], strict=False)
        ):
            return False
    return True


def _reservation_lifecycle_passes(run: dict[str, object]) -> bool:
    decisions = run.get("decision_timeline")
    jobs = run.get("jobs")
    allocations = run.get("allocation_timeline")
    if (
        not isinstance(decisions, list)
        or not isinstance(jobs, list)
        or not isinstance(allocations, list)
    ):
        raise ReportError("reservation run must retain decisions, jobs, and allocations")
    jobs_by_id: dict[str, dict[str, object]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            return False
        job_id = job.get("job_id")
        tenant_id = job.get("tenant_id")
        arrival_ms = job.get("arrival_ms")
        if (
            not isinstance(job_id, str)
            or job_id in jobs_by_id
            or not isinstance(tenant_id, str)
            or not isinstance(arrival_ms, int)
        ):
            return False
        jobs_by_id[job_id] = job
    creates = [
        event
        for event in decisions
        if isinstance(event, dict)
        and event.get("decision_type") == "create_reservation"
        and isinstance(event.get("reservation_id"), str)
        and event.get("reason") == "eligible_wait_threshold"
        and type(event.get("eligible_wait_seconds")) is int
        and event["eligible_wait_seconds"] >= 120
    ]
    for created in creates:
        reservation_id = created["reservation_id"]
        created_ms = created.get("time_ms")
        job_id = created.get("job_id")
        if not isinstance(created_ms, int) or not isinstance(job_id, str):
            continue
        drains = [
            event
            for event in decisions
            if isinstance(event, dict)
            and event.get("decision_type") == "drain_for_reservation"
            and event.get("reservation_id") == reservation_id
            and isinstance(event.get("time_ms"), int)
        ]
        dispatches = [
            event
            for event in decisions
            if isinstance(event, dict)
            and event.get("decision_type") == "dispatch"
            and event.get("reservation_id") == reservation_id
            and event.get("job_id") == job_id
            and isinstance(event.get("time_ms"), int)
        ]
        for dispatched in dispatches:
            dispatched_ms = dispatched["time_ms"]
            lifecycle_drains = [
                event for event in drains if created_ms <= event["time_ms"] < dispatched_ms
            ]
            has_withheld_fitting_job = False
            for event in lifecycle_drains:
                claimed = event.get("fitting_other_candidate_ids")
                candidate_states = event.get("reservation_drain_candidate_states")
                if (
                    not isinstance(claimed, list)
                    or not claimed
                    or not all(isinstance(candidate_id, str) for candidate_id in claimed)
                    or len(set(claimed)) != len(claimed)
                    or not isinstance(candidate_states, list)
                ):
                    continue
                evidenced_fitting: set[str] = set()
                seen_candidates: set[str] = set()
                valid_evidence = True
                for state in candidate_states:
                    if not isinstance(state, list) or len(state) != 4:
                        valid_evidence = False
                        break
                    candidate_id, candidate_tenant_id, eligibility_reason, fits_free = state
                    if (
                        not isinstance(candidate_id, str)
                        or candidate_id in seen_candidates
                        or not isinstance(candidate_tenant_id, str)
                        or (
                            eligibility_reason is not None
                            and not isinstance(eligibility_reason, str)
                        )
                        or type(fits_free) is not bool
                    ):
                        valid_evidence = False
                        break
                    candidate_job = jobs_by_id.get(candidate_id)
                    if (
                        candidate_job is None
                        or candidate_job["tenant_id"] != candidate_tenant_id
                        or candidate_job["arrival_ms"] > event["time_ms"]
                    ):
                        valid_evidence = False
                        break
                    seen_candidates.add(candidate_id)
                    if eligibility_reason is None and fits_free:
                        evidenced_fitting.add(candidate_id)
                if valid_evidence and job_id not in claimed and set(claimed) == evidenced_fitting:
                    has_withheld_fitting_job = True
                    break
            has_arrival_before_dispatch = any(
                isinstance(job, dict)
                and job.get("job_id") != job_id
                and isinstance(job.get("arrival_ms"), int)
                and created_ms < job["arrival_ms"] < dispatched_ms
                for job in jobs
            )
            has_arrival_after_dispatch = any(
                isinstance(job, dict)
                and job.get("job_id") != job_id
                and isinstance(job.get("arrival_ms"), int)
                and job["arrival_ms"] > dispatched_ms
                for job in jobs
            )
            release_times = {
                allocation["release_ms"]
                for allocation in allocations
                if isinstance(allocation, dict)
                and isinstance(allocation.get("job_id"), str)
                and allocation["job_id"] in jobs_by_id
                and isinstance(allocation.get("start_ms"), int)
                and isinstance(allocation.get("release_ms"), int)
                and allocation["start_ms"] <= created_ms < allocation["release_ms"] <= dispatched_ms
            }
            if (
                has_withheld_fitting_job
                and len(release_times) >= 2
                and has_arrival_before_dispatch
                and has_arrival_after_dispatch
            ):
                return True
    return False


def _profile_status(run: dict[str, object]) -> str:
    if not _run_has_complete_invariants(run):
        return "fail"
    profile_kind = run.get("profile_kind")
    profile_id = run.get("profile_id")
    baseline = run.get("baseline")
    if not isinstance(baseline, dict):
        raise ReportError("run baseline must be an object")
    if profile_kind == "fairness":
        if _fairness_status(run) != "pass":
            return "fail"
        if profile_id == "gpu-slots" and not _gpu_allocations_are_exclusive(run):
            return "fail"
        return "pass"
    if profile_kind == "reservation":
        if baseline.get("name") != "nexa":
            return "not_applicable"
        return "pass" if _reservation_lifecycle_passes(run) else "fail"
    if profile_kind == "diagnostic":
        fairness = run.get("fairness")
        if not isinstance(fairness, dict):
            raise ReportError("run fairness must be an object")
        exclusions = fairness.get("excluded_tenants")
        return (
            "pass"
            if fairness.get("weighted_jain") is None
            and isinstance(exclusions, dict)
            and bool(exclusions)
            else "fail"
        )
    raise ReportError("run profile kind is invalid")


def comparison_csv(bundle: object) -> str:
    output = io.StringIO(newline="")
    fields = [
        "profile_id",
        "profile_kind",
        "seed",
        "policy",
        "weighted_jain",
        "fairness_status",
        "profile_status",
        "included_tenants",
        "excluded_tenants",
        "p95_wait_ms",
        "max_wait_ms",
        "throughput_jobs_per_second",
        "completed",
        "candidate_evaluations",
        "invariant_violations",
        "undispatched_jobs",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for run in _runs(bundle):
        baseline = run.get("baseline")
        provenance = run.get("provenance")
        fairness = run.get("fairness")
        metrics = run.get("metrics")
        counts = run.get("counts")
        starvation = run.get("starvation")
        if not all(
            isinstance(value, dict)
            for value in (baseline, provenance, fairness, metrics, counts, starvation)
        ):
            raise ReportError("run is missing structured metrics")
        cost = baseline.get("cost")
        if not isinstance(cost, dict):
            raise ReportError("run baseline cost must be an object")
        weighted_jain = fairness.get("weighted_jain")
        writer.writerow(
            {
                "profile_id": run.get("profile_id"),
                "profile_kind": run.get("profile_kind"),
                "seed": provenance.get("seed"),
                "policy": baseline.get("name"),
                "weighted_jain": weighted_jain if weighted_jain is not None else "N/A",
                "fairness_status": _fairness_status(run),
                "profile_status": _profile_status(run),
                "included_tenants": ";".join(fairness.get("included_tenants", [])),
                "excluded_tenants": ";".join(
                    f"{tenant}:{reason}"
                    for tenant, reason in sorted(fairness.get("excluded_tenants", {}).items())
                ),
                "p95_wait_ms": metrics.get("p95_wait_ms"),
                "max_wait_ms": metrics.get("max_wait_ms"),
                "throughput_jobs_per_second": metrics.get("throughput_jobs_per_second"),
                "completed": counts.get("completed"),
                "candidate_evaluations": cost.get("candidate_evaluations"),
                "invariant_violations": len(run.get("invariant_violations", [])),
                "undispatched_jobs": len(starvation.get("undispatched_job_ids", [])),
            }
        )
    return output.getvalue()


def comparison_svg(bundle: object) -> str:
    runs = sorted(
        _runs(bundle),
        key=lambda run: (
            str(run.get("profile_id")),
            int(run.get("provenance", {}).get("seed", 0)),
            str(run.get("baseline", {}).get("name")),
        ),
    )
    reservation_events = [
        (run, event)
        for run in runs
        if run.get("baseline", {}).get("name") == "nexa"
        and run.get("profile_kind") == "reservation"
        for event in run.get("decision_timeline", [])
        if event.get("decision_type") in {"create_reservation", "drain_for_reservation", "dispatch"}
        and event.get("reservation_id") is not None
    ]
    width = 1200
    table_start = 72
    reservation_start = table_start + len(runs) * 20 + 60
    height = reservation_start + max(1, len(reservation_events)) * 28 + 50
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="20" y="30" font-family="sans-serif" font-size="20" fill="#111827">'
        "B04 fairness, aging, and reservation</text>",
        '<text x="20" y="52" font-family="monospace" font-size="11" fill="#475569">'
        "profile             seed policy  Jain       fair         profile      "
        "p95 wait  max wait  cost</text>",
    ]
    for index, run in enumerate(runs):
        fairness = run["fairness"]
        metrics = run["metrics"]
        baseline = run["baseline"]
        provenance = run["provenance"]
        jain = fairness.get("weighted_jain")
        display_jain = jain if jain is not None else "N/A"
        y = table_start + index * 20
        parts.append(
            f'<text data-kind="run-summary" data-profile="{escape(str(run.get("profile_id")))}" '
            f'data-policy="{escape(str(baseline.get("name")))}" x="20" y="{y}" '
            f'data-jain="{escape(str(display_jain))}" '
            f'data-profile-status="{escape(_profile_status(run))}" '
            'font-family="monospace" font-size="11" fill="#334155">'
            f"{escape(str(run.get('profile_id'))):19} "
            f"{escape(str(provenance.get('seed'))):4} "
            f"{escape(str(baseline.get('name'))):7} "
            f"{escape(str(display_jain)):10} "
            f"{escape(_fairness_status(run)):12} "
            f"{escape(_profile_status(run)):12} "
            f"{escape(str(metrics.get('p95_wait_ms'))):9} "
            f"{escape(str(metrics.get('max_wait_ms'))):9} "
            f"{escape(str(baseline.get('cost', {}).get('candidate_evaluations')))}</text>"
        )
    parts.append(
        f'<text x="20" y="{reservation_start - 24}" font-family="sans-serif" '
        'font-size="16" fill="#111827">Reservation timeline</text>'
    )
    for index, (run, event) in enumerate(reservation_events):
        y = reservation_start + index * 28
        parts.extend(
            [
                f'<circle data-kind="reservation-event" '
                f'data-profile="{escape(str(run.get("profile_id")))}" '
                f'data-job-id="{escape(str(event.get("job_id")))}" '
                f'data-decision="{escape(str(event.get("decision_type")))}" '
                f'cx="30" cy="{y - 4}" r="5" fill="#0f766e"/>',
                f'<text x="44" y="{y}" font-family="monospace" font-size="11" '
                f'fill="#334155">{escape(str(run.get("profile_id")))} seed '
                f"{escape(str(run.get('provenance', {}).get('seed')))} at "
                f"{escape(str(event.get('time_ms')))} ms: "
                f"{escape(str(event.get('decision_type')))} "
                f"{escape(str(event.get('job_id')))}</text>",
            ]
        )
    if not reservation_events:
        parts.append(
            f'<text x="20" y="{reservation_start}" font-family="sans-serif" '
            'font-size="12" fill="#64748b">N/A</text>'
        )
    parts.append("</svg>\n")
    return "\n".join(parts)
