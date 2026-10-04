"""B19 §7.D: real scrape of the three ops listeners and one live alert.

    PYTHONPATH=src:. uv run --no-sync python scripts/b17_e2e_stack.py run --tier w2 \
        --prometheus --alert-for 20s -- .venv/bin/python scripts/b19_scrape.py --out FILE

Steps: Prometheus reports `up` = 1 for nexa-api, nexa-coordinator and nexa-worker;
one cpu-iterative job runs to SUCCEEDED; at least one series of each main group is
queried back from Prometheus; every nexa_* label value is checked for IDs; 100
/metrics and /readyz requests write no audit or event row (AC-14); Caddy does not
serve /metrics; then the worker container is stopped and NexaTargetDown /
NexaWorkerNotReady go pending and firing under the generated short-`for` rules.

B19-RV02: before any /readyz request, Prometheus (which scrapes only /metrics) must
already see `nexa_ready` = 1 for the three jobs. Then the API staging directory is
made read-only (mode 0500) so the storage readiness probe cannot create its file:
with no /readyz request at all, `nexa_ready{check="storage"}` drops to 0 and
NexaNotReady fires for nexa-api; mode 0700 is restored and the alert resolves.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import httpx

from scripts.b19_client import (
    UUID,
    Rest,
    docker,
    fixture,
    log,
    metrics,
    now,
    prometheus_query,
    psql,
    row_counts,
    total,
    wait_until,
    write_json,
)

JOBS = ("nexa-api", "nexa-coordinator", "nexa-worker")
# One query per main group of PLAN §9 / docs/observability.md §1.
GROUPS = {
    "http": 'sum by (job) (nexa_http_requests_total{job="nexa-api"})',
    "admission": "sum by (kind, outcome) (nexa_admission_total)",
    "queue": "nexa_queue_outstanding",
    "jobs": "nexa_jobs",
    "allocation": "nexa_allocations",
    # The Jain index is left out when no tenant was charged in the window (by design).
    "fairness": "nexa_fairness_dominant_resource_seconds",
    "checkpoint": "nexa_checkpoint_age_seconds",
    "workers": "nexa_workers",
    "mode": "nexa_operational_mode",
    "storage": "nexa_storage_used_ratio",
    "watermark": "nexa_storage_watermark_ratio",
    "collectors": "nexa_collector_up",
    "gc": "nexa_gc_last_run_timestamp_seconds",
    "readiness": "nexa_ready",
    "coordinator_leader": "nexa_coordinator_leader",
    "coordinator_decisions": "nexa_coordinator_decisions_total",
    "coordinator_tick": "nexa_coordinator_tick_duration_seconds_count",
    "worker_ready": "nexa_worker_ready",
    "worker_executions": "nexa_worker_executions_total",
}


def _up(prometheus: str) -> dict[str, str]:
    return {
        sample["metric"]["job"]: sample["value"][1]
        for sample in prometheus_query(prometheus, 'up{job=~"nexa-.*"}')
    }


def _label_audit(prometheus: str) -> dict:
    response = httpx.get(
        prometheus + "/api/v1/series", params={"match[]": '{__name__=~"nexa_.*"}'}, timeout=10
    )
    response.raise_for_status()
    series = response.json()["data"]
    by_job: dict[str, int] = {}
    leaked = []
    for labels in series:
        by_job[labels.get("job", "?")] = by_job.get(labels.get("job", "?"), 0) + 1
        for key, value in labels.items():
            if key not in {"__name__", "job", "instance"} and UUID.search(value):
                leaked.append(f"{labels['__name__']} {key}")
    return {
        "series_by_job": by_job,
        "id_labels": leaked,
        "names": len({s["__name__"] for s in series}),
    }


def _alerts(prometheus: str) -> list[dict]:
    response = httpx.get(prometheus + "/api/v1/alerts", timeout=10)
    response.raise_for_status()
    return [
        {
            "alert": alert["labels"]["alertname"],
            "job": alert["labels"].get("job"),
            "state": alert["state"],
            "active_at": alert.get("activeAt"),
        }
        for alert in response.json()["data"]["alerts"]
    ]


def _ready_by_job(prometheus: str) -> dict[str, str]:
    return {
        sample["metric"]["job"]: sample["value"][1]
        for sample in prometheus_query(prometheus, "min by (job) (nexa_ready)")
    }


def _storage_not_ready(prometheus: str) -> dict:
    """RV02 live: a failing API storage check fires NexaNotReady with no /readyz call."""
    staging = Path(os.environ["NEXA_B17_FIXTURE"]).parent / "artifacts" / "staging"
    mode = staging.stat().st_mode & 0o777
    started = time.monotonic()
    timeline: list[dict] = []
    seen: set[str] = set()

    def note(state: str) -> None:
        if state not in seen:
            seen.add(state)
            timeline.append({"state": state, "after_s": round(time.monotonic() - started, 1)})
            log(f"RV02 {state}")

    def api_alert() -> str | None:
        for alert in _alerts(prometheus):
            if alert["alert"] == "NexaNotReady" and alert["job"] == "nexa-api":
                return alert["state"]
        return None

    def storage_ready() -> str | None:
        samples = prometheus_query(prometheus, 'nexa_ready{job="nexa-api",check="storage"}')
        return samples[0]["value"][1] if samples else None

    def firing() -> bool:
        state = api_alert()
        if state:
            note(state)  # pending, then firing
        return state == "firing"

    staging.chmod(0o500)
    try:
        wait_until(lambda: storage_ready() == "0", "nexa_ready storage 0 in Prometheus", 60)
        note("storage_ready_0")
        wait_until(firing, "NexaNotReady firing for nexa-api", 120, 1)
    finally:
        staging.chmod(mode)
    note("storage_restored")
    wait_until(lambda: storage_ready() == "1", "nexa_ready storage 1 in Prometheus", 60)
    note("storage_ready_1")
    wait_until(lambda: api_alert() is None, "NexaNotReady resolved for nexa-api", 120, 1)
    note("resolved")
    return {"staging_mode_restored": oct(mode), "timeline": timeline}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    parser.add_argument("--iterations", type=int, default=10)
    args = parser.parse_args()
    seed = fixture()
    prometheus = seed["prometheus"]["url"]
    ops = seed["ops"]
    report: dict = {"started": now(), "alert_for": seed["prometheus"]["alert_for"]}

    report["up"] = wait_until(
        lambda: (up := _up(prometheus)) and all(up.get(job) == "1" for job in JOBS) and up,
        "up = 1 for the three targets",
        120,
    )
    log(f"targets up: {report['up']}")
    # RV02: readiness reaches Prometheus through /metrics alone (no /readyz yet).
    report["ready_from_metrics_only"] = wait_until(
        lambda: (
            (ready := _ready_by_job(prometheus))
            and all(ready.get(job) == "1" for job in JOBS)
            and ready
        ),
        "nexa_ready = 1 for the three jobs before any /readyz request",
        60,
    )
    log(f"ready via /metrics only: {report['ready_from_metrics_only']}")
    report["readyz"] = {
        name: httpx.get(url + "/readyz", timeout=5).status_code for name, url in ops.items()
    }

    rest = Rest(seed, "member_a")
    tenant = seed["tenants"]["a"]
    job_id = rest.submit_cpu(
        tenant, seed["artifacts"]["a_input"]["artifact_id"], 1, args.iterations
    )
    with open(seed["accepted_ids_file"], "a") as accepted:
        accepted.write(job_id + "\n")
    state = wait_until(
        lambda: (s := rest.job_state(tenant, job_id)) in {"SUCCEEDED", "FAILED", "CANCELLED"} and s,
        "job terminal",
        300,
        1,
    )
    report["job"] = {"job_id": job_id, "state": state}
    log(f"job {job_id} {state}")
    rest.close()

    worker_direct = metrics(ops["worker"])
    report["worker_succeeded_direct"] = total(
        worker_direct, "nexa_worker_executions_total", outcome="SUCCEEDED"
    )
    wait_until(
        lambda: prometheus_query(
            prometheus, 'nexa_worker_executions_total{outcome="SUCCEEDED"} >= 1'
        ),
        "Prometheus to scrape the worker execution",
        60,
    )
    report["groups"] = {}
    for group, query in GROUPS.items():
        samples = prometheus_query(prometheus, query)
        report["groups"][group] = [
            {"metric": sample["metric"], "value": sample["value"][1]} for sample in samples
        ]
    missing = [group for group, samples in report["groups"].items() if not samples]
    report["groups_missing"] = missing
    report["labels"] = _label_audit(prometheus)
    log(f"groups missing={missing} id_labels={report['labels']['id_labels']}")

    report["not_ready_via_metrics"] = _storage_not_ready(prometheus)

    # AC-14: scrapes and readiness probes write nothing to the database. The worker
    # releases the allocation after the result, so wait until no allocation is held.
    wait_until(
        lambda: (
            psql(
                seed["database_name"],
                "SELECT count(*) FROM allocations WHERE state IN ('HELD', 'QUARANTINED')",
            )[0][0]
            == "0"
        ),
        "the allocation release",
        60,
    )
    before = row_counts(seed["database_name"])
    probes = {}
    for _ in range(50):
        for name, url in ops.items():
            for path in ("/metrics", "/readyz"):
                code = httpx.get(url + path, timeout=5).status_code
                probes[f"{name}{path} {code}"] = probes.get(f"{name}{path} {code}", 0) + 1
    after = row_counts(seed["database_name"])
    report["probe_writes"] = {"probes": probes, "before": before, "after": after}

    # Caddy serves the SPA and /v1 only; /metrics is not proxied to any listener.
    with httpx.Client(verify=seed["ca_file"], timeout=5) as client:
        page = client.get(seed["base_url"] + "/metrics")
    report["caddy_metrics"] = {
        "status": page.status_code,
        "prometheus_text": "nexa_http_requests_total" in page.text,
    }

    # Live alert: stop the worker; its target goes down and no ENABLED worker stays READY.
    report["alerts_before_stop"] = _alerts(prometheus)
    stopped = time.monotonic()
    report["worker_stopped_at"] = now()
    docker("stop", "--time", "10", seed["worker_container"])
    log("worker container stopped")
    timeline: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def firing() -> bool:
        for alert in _alerts(prometheus):
            key = (alert["alert"], alert["state"])
            if key not in seen:
                seen.add(key)
                timeline.append(
                    {**alert, "seen_at": now(), "after_s": round(time.monotonic() - stopped, 1)}
                )
                log(f"alert {alert['alert']} {alert['state']} job={alert['job']}")
        return ("NexaTargetDown", "firing") in seen and ("NexaWorkerNotReady", "firing") in seen

    try:
        wait_until(firing, "NexaTargetDown and NexaWorkerNotReady firing", 240, 1)
    finally:
        report["alert_timeline"] = timeline
        report["finished"] = now()
        write_json(args.out, report)
    failures = []
    if any(code != 200 for code in report["readyz"].values()):
        failures.append("readyz")
    states = [step["state"] for step in report["not_ready_via_metrics"]["timeline"]]
    if not {"storage_ready_0", "firing", "storage_ready_1", "resolved"} <= set(states):
        failures.append("not_ready_via_metrics")
    if state != "SUCCEEDED" or report["worker_succeeded_direct"] < 1:
        failures.append("job")
    if missing or report["labels"]["id_labels"]:
        failures.append("series")
    if before != after or any(not key.endswith(" 200") for key in probes):
        failures.append("probes")
    if report["caddy_metrics"]["prometheus_text"]:
        failures.append("caddy")
    log(f"failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
