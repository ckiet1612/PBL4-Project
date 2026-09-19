import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from benchmarks.b04 import B04_SIMULATOR_VERSION
from benchmarks.b04.checks import build_policy_contract_checks
from benchmarks.b04.engine import ProductPolicySimulator, ProductSimulationResult
from benchmarks.b04.report import ReportError, comparison_csv, comparison_svg
from benchmarks.b04.suite import BenchmarkSuite, SuiteError, SuiteProfile, load_suite
from benchmarks.simulator.baselines import (
    DeficitRoundRobinPolicy,
    DominantResourceFairnessPolicy,
    FifoPolicy,
    RoundRobinPolicy,
    WeightedRoundRobinPolicy,
)
from benchmarks.simulator.engine import SimulationResult, Simulator, TimelineEvent
from benchmarks.simulator.metrics import build_raw_result
from benchmarks.simulator.model import ResourceVector
from benchmarks.simulator.policy import SchedulerPolicy
from benchmarks.simulator.trace import (
    TraceError,
    canonical_json_bytes,
    load_trace,
    materialize_trace,
)
from nexa.scheduler.policy import WeightedDominantResourceTimePolicy

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


def _resource_data(resources: object) -> dict[str, int]:
    return {
        "cpu_millis": resources.cpu_millis,
        "memory_bytes": resources.memory_bytes,
        "gpu_count": resources.gpu_count,
    }


def _product_raw(result: ProductSimulationResult) -> dict[str, object]:
    jobs = {job.job_id: job for job in result.trace.jobs}
    allocations = {allocation.job_id: allocation for allocation in result.allocations}
    metric_timeline = tuple(
        TimelineEvent(
            time_ms=event.time_ms,
            sequence=event.sequence,
            event_type=event.event_type,
            job_id=event.job_id,
            tenant_id=event.tenant_id,
            resources=(
                jobs[event.job_id].resources if event.job_id in jobs else ResourceVector.zero()
            ),
            gpu_uuids=(allocations[event.job_id].gpu_uuids if event.job_id in allocations else ()),
            reason=event.reason,
        )
        for event in result.timeline
    )
    raw = build_raw_result(
        SimulationResult(
            baseline_name=result.policy_name,
            baseline_version=result.policy_version,
            trace=result.trace,
            job_records=result.job_records,
            allocations=result.allocations,
            timeline=metric_timeline,
            final_allocated=ResourceVector.zero(),
            policy_decisions=result.policy_decisions,
            candidate_evaluations=result.candidate_evaluations,
            invariant_violations=result.invariant_violations,
        )
    )
    raw["b04_simulator_version"] = B04_SIMULATOR_VERSION
    raw["policy_state"] = {
        "virtual_floor": str(result.final_virtual_floor),
        "tenant_ledgers": [
            {
                "tenant_id": ledger.tenant_id,
                "virtual_score": str(ledger.virtual_score),
                "accounted_through_ms": ledger.accounted_through_ms,
                "had_eligible_demand": ledger.had_eligible_demand,
            }
            for ledger in result.final_tenant_ledgers
        ],
    }
    raw["accounting_timeline_schema"] = {
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
    raw["accounting_timeline"] = [
        [
            record.interval_start_ms,
            record.interval_end_ms,
            [
                [
                    tenant_id,
                    resources.cpu_millis,
                    resources.memory_bytes,
                    resources.gpu_count,
                ]
                for tenant_id, resources in record.held_resources_by_tenant
            ],
            [[tenant_id, str(share)] for tenant_id, share in record.dominant_shares],
            [
                [
                    ledger.tenant_id,
                    str(ledger.virtual_score),
                    ledger.accounted_through_ms,
                    ledger.had_eligible_demand,
                ]
                for ledger in record.tenant_ledgers
            ],
            str(record.virtual_floor),
            list(record.eligible_tenant_ids),
        ]
        for record in result.accounting_timeline
    ]
    raw["decision_timeline_schema"] = {
        "decision_fields": [
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
        ],
        "tenant_state_fields": [
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
        ],
        "candidate_state_fields": [
            "job_id",
            "eligible_wait_seconds",
            "effective_priority",
            "eligibility_reason",
            "fits_free_resources",
        ],
        "reservation_drain_candidate_state_fields": [
            "job_id",
            "tenant_id",
            "eligibility_reason",
            "fits_free_resources",
        ],
    }
    decision_timeline: list[dict[str, object]] = []
    for record in result.decision_timeline:
        selected_candidate = next(
            (
                candidate
                for tenant in record.tenant_state
                for candidate in tenant.candidates
                if candidate.job_id == record.job_id
            ),
            None,
        )
        selected_tenant = next(
            (tenant for tenant in record.tenant_state if tenant.tenant_id == record.tenant_id),
            None,
        )
        tenant_states = []
        for tenant in record.tenant_state:
            eligible = [
                candidate for candidate in tenant.candidates if candidate.eligibility_reason is None
            ]
            reasons = {
                reason: sum(
                    candidate.eligibility_reason == reason for candidate in tenant.candidates
                )
                for reason in sorted(
                    {
                        candidate.eligibility_reason
                        for candidate in tenant.candidates
                        if candidate.eligibility_reason is not None
                    }
                )
            }
            tenant_states.append(
                [
                    tenant.tenant_id,
                    str(tenant.virtual_score),
                    str(tenant.dominant_share),
                    str(tenant.weight),
                    _resource_data(tenant.held_resources),
                    tenant.oldest_ready_sequence,
                    len(tenant.normal_candidate_ids),
                    len(eligible),
                    sum(candidate.fits_free_resources for candidate in eligible),
                    reasons,
                ]
            )
        selected_tenant_state = None
        if selected_tenant is not None and record.decision_type in {
            "dispatch",
            "create_reservation",
        }:
            selected_tenant_state = {
                "normal_candidate_ids": list(selected_tenant.normal_candidate_ids),
                "oldest_eligible_job_id": selected_tenant.oldest_eligible_job_id,
                "eligible_candidate_ids": [
                    candidate.job_id
                    for candidate in selected_tenant.candidates
                    if candidate.eligibility_reason is None
                ],
                "fitting_candidate_ids": [
                    candidate.job_id
                    for candidate in selected_tenant.candidates
                    if candidate.eligibility_reason is None and candidate.fits_free_resources
                ],
                "continuation_cursor": (
                    selected_tenant.continuation_cursor.decode("utf-8")
                    if selected_tenant.continuation_cursor is not None
                    else None
                ),
                "candidate_states": [
                    [
                        candidate.job_id,
                        candidate.eligible_wait_seconds,
                        candidate.effective_priority,
                        candidate.eligibility_reason,
                        candidate.fits_free_resources,
                    ]
                    for candidate in selected_tenant.candidates
                ],
            }
        fitting_other_candidate_ids = sorted(
            candidate.job_id
            for tenant in record.tenant_state
            for candidate in tenant.candidates
            if candidate.job_id != record.job_id
            and candidate.eligibility_reason is None
            and candidate.fits_free_resources
        )
        decision = {
            "time_ms": record.time_ms,
            "decision_type": record.decision_type,
            "job_id": record.job_id,
            "tenant_id": record.tenant_id,
            "reason": record.reason,
            "reservation_id": record.reservation_id,
            "fitting_other_candidate_ids": fitting_other_candidate_ids,
            "eligible_wait_seconds": (
                selected_candidate.eligible_wait_seconds if selected_candidate is not None else None
            ),
            "effective_priority": (
                selected_candidate.effective_priority if selected_candidate is not None else None
            ),
            "virtual_floor": str(record.virtual_floor),
            "tenant_states": tenant_states,
            "selected_tenant_state": selected_tenant_state,
        }
        if record.decision_type == "drain_for_reservation":
            decision["reservation_drain_candidate_states"] = [
                [
                    candidate.job_id,
                    tenant.tenant_id,
                    candidate.eligibility_reason,
                    candidate.fits_free_resources,
                ]
                for tenant in record.tenant_state
                for candidate in tenant.candidates
                if candidate.eligibility_reason is None and candidate.fits_free_resources
            ]
        decision_timeline.append(decision)
    raw["decision_timeline"] = decision_timeline
    return raw


def _annotate(raw: dict[str, object], profile: SuiteProfile) -> dict[str, object]:
    raw["profile_id"] = profile.profile_id
    raw["profile_kind"] = profile.profile_kind
    raw["valid_fairness"] = profile.valid_fairness
    raw["jain_threshold"] = profile.jain_threshold
    config = raw.get("config")
    if not isinstance(config, dict) or not isinstance(config.get("tenants"), list):
        raise RuntimeError("raw run is missing tenant configuration")
    config["user_mapping"] = [
        {
            "tenant_id": tenant["tenant_id"],
            "user_id": f"{tenant['tenant_id']}:user",
            "max_active_attempts": tenant["max_running_jobs"],
        }
        for tenant in config["tenants"]
    ]
    return raw


def _compact_raw(raw: dict[str, object]) -> dict[str, object]:
    jobs = raw.get("jobs")
    if not isinstance(jobs, list):
        raise RuntimeError("raw run is missing jobs")
    raw["jobs"] = [
        {
            key: job[key]
            for key in (
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
            )
        }
        for job in jobs
    ]
    raw.pop("event_timeline", None)
    raw.pop("dispatch_timeline", None)
    raw.pop("gpu_allocation_timeline", None)
    return raw


def _selected_profiles(
    suite: BenchmarkSuite, profile_ids: tuple[str, ...] | None
) -> tuple[SuiteProfile, ...]:
    if profile_ids is None:
        return suite.profiles
    selected = tuple(profile for profile in suite.profiles if profile.profile_id in profile_ids)
    if {profile.profile_id for profile in selected} != set(profile_ids):
        raise SuiteError("unknown selected profile")
    return selected


def compare_suite(
    suite_path: Path,
    *,
    profile_ids: tuple[str, ...] | None = None,
    seeds: tuple[int, ...] | None = None,
) -> dict[str, object]:
    suite = load_suite(suite_path)
    product_policy = WeightedDominantResourceTimePolicy()
    if suite.policy_version != product_policy.version:
        raise SuiteError("suite policy version does not match the instantiated Nexa policy version")
    selected_seeds = suite.seeds if seeds is None else seeds
    if not selected_seeds or not set(selected_seeds).issubset(suite.seeds):
        raise SuiteError("selected seeds must be a non-empty subset of the frozen suite")
    runs: list[dict[str, object]] = []
    for profile in _selected_profiles(suite, profile_ids):
        definition = load_trace(profile.trace_path)
        for seed in sorted(selected_seeds):
            trace = materialize_trace(definition, seed)
            product = ProductPolicySimulator().run(trace, product_policy)
            runs.append(_annotate(_compact_raw(_product_raw(product)), profile))
            for baseline_name in sorted(BASELINES):
                baseline = Simulator().run(trace, BASELINES[baseline_name]())
                runs.append(_annotate(_compact_raw(build_raw_result(baseline)), profile))
    return {
        "schema_version": 1,
        "b04_simulator_version": B04_SIMULATOR_VERSION,
        "suite": {
            "path": suite_path.name,
            "version": suite.version,
            "policy_version": suite.policy_version,
            "seeds": list(sorted(selected_seeds)),
            "profiles": [profile.profile_id for profile in _selected_profiles(suite, profile_ids)],
            "policies": ["nexa", *sorted(BASELINES)],
        },
        "policy_contract_checks": build_policy_contract_checks(),
        "runs": runs,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nexa-b04-benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--suite", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    report = subparsers.add_parser("report")
    report.add_argument("--input", type=Path, required=True)
    report.add_argument("--csv", type=Path, required=True)
    report.add_argument("--svg", type=Path, required=True)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(arguments)
    try:
        if args.command == "compare":
            _atomic_write(
                args.output,
                canonical_json_bytes(compare_suite(args.suite)) + b"\n",
            )
            return 0
        if args.command == "report":
            bundle = json.loads(args.input.read_text(encoding="utf-8"))
            csv_content = comparison_csv(bundle).encode()
            svg_content = comparison_svg(bundle).encode()
            _atomic_write(args.csv, csv_content)
            _atomic_write(args.svg, svg_content)
            return 0
    except (
        OSError,
        json.JSONDecodeError,
        RuntimeError,
        SuiteError,
        TraceError,
        ReportError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
