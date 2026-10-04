"""B19 §7.F: outage test with accepted job IDs (W2 stack, Prometheus scraping).

    PYTHONPATH=src:. uv run --no-sync python scripts/b17_e2e_stack.py run --tier w2 \
        --prometheus --api-env NEXA_METRICS_CACHE_SECONDS=1 \
        --api-env NEXA_METRICS_STATEMENT_TIMEOUT_MS=200 -- \
        .venv/bin/python scripts/b19_outage.py --out FILE

While the submitted jobs run: Prometheus is stopped for good, the API /metrics is
requested at ~20/s for 30 s, and the checkpoint collector is made to fail by
holding `LOCK TABLE checkpoints IN ACCESS EXCLUSIVE MODE` for 3 s from another
connection, several times (the collector's own lock_timeout/statement_timeout of
200 ms, a test setting, gives up first). The accepted IDs from the 202 responses
go to the harness file; after this script the harness runs `nexa-maintenance
consistency-check` and `storage-check` on them. 5xx are counted from the API
metric `nexa_http_requests_total{status_class="5xx"}` and this script's REST calls.
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import threading
import time

import httpx

from scripts.b19_client import (
    PG_CONTAINER,
    TERMINAL,
    Rest,
    docker,
    fixture,
    log,
    metrics,
    now,
    psql,
    row_counts,
    total,
    wait_until,
    write_json,
)

LOCK_SECONDS = 3
LOCKS = 4


def _lock_checkpoints(database: str) -> int:
    """Hold an exclusive lock on checkpoints for LOCK_SECONDS in a separate session."""
    return subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", "postgres", "-d", database, "-q"]
        + ["-v", "ON_ERROR_STOP=1", "-c"]
        + [
            "BEGIN; LOCK TABLE checkpoints IN ACCESS EXCLUSIVE MODE;"
            f" SELECT pg_sleep({LOCK_SECONDS}); COMMIT;"
        ],
        capture_output=True,
        timeout=60,
        check=False,
    ).returncode


def _states(rest_by_tenant: dict, accepted: list[tuple[str, str]]) -> dict[str, str]:
    return {job_id: rest_by_tenant[tenant].job_state(tenant, job_id) for tenant, job_id in accepted}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--iterations", type=int, default=30_000_000)
    # 256 MiB lets several jobs run at once on a small Docker Desktop VM.
    parser.add_argument("--memory-mib", type=int, default=256)
    parser.add_argument("--rate", type=float, default=20)
    parser.add_argument("--seconds", type=float, default=30)
    args = parser.parse_args()
    if args.jobs < 6:
        parser.error("--jobs must be at least 6")
    seed = fixture()
    api_ops = seed["ops"]["api"]
    database = seed["database_name"]
    timeline: list[dict] = []

    def mark(event: str, **detail) -> None:
        timeline.append({"at": now(), "event": event, **detail})
        log(event + (f" {detail}" if detail else ""))

    report: dict = {"config": vars(args), "timeline": timeline}
    rows_before = row_counts(database)
    before = metrics(api_ops)
    report["api_5xx_before"] = total(before, "nexa_http_requests_total", status_class="5xx")

    rests = {
        seed["tenants"]["a"]: Rest(seed, "member_a"),
        seed["tenants"]["b"]: Rest(seed, "member_b"),
    }
    inputs = {
        seed["tenants"]["a"]: seed["artifacts"]["a_input"]["artifact_id"],
        seed["tenants"]["b"]: seed["artifacts"]["b_input"]["artifact_id"],
    }
    accepted: list[tuple[str, str]] = []
    tenants = list(rests)
    for index in range(args.jobs):
        tenant = tenants[index % len(tenants)]
        job_id = rests[tenant].submit_cpu(
            tenant, inputs[tenant], 100 + index, args.iterations, args.memory_mib * 1024**2
        )
        accepted.append((tenant, job_id))
        with open(seed["accepted_ids_file"], "a") as handle:
            handle.write(job_id + "\n")
    mark("submitted", accepted=len(accepted))
    wait_until(lambda: "RUNNING" in _states(rests, accepted).values(), "a RUNNING job", 120, 1)
    mark("first job RUNNING", states=_states(rests, accepted))

    docker("stop", "--time", "5", seed["prometheus"]["container"])
    mark("prometheus stopped")

    hammer: dict = {"codes": {}, "latency_ms": [], "errors": 0}

    def hit_metrics() -> None:
        interval = 1 / args.rate
        deadline = time.monotonic() + args.seconds
        with httpx.Client(timeout=5) as client:
            while time.monotonic() < deadline:
                started = time.monotonic()
                try:
                    code = client.get(api_ops + "/metrics").status_code
                    hammer["codes"][code] = hammer["codes"].get(code, 0) + 1
                    hammer["latency_ms"].append((time.monotonic() - started) * 1000)
                except httpx.RequestError:
                    hammer["errors"] += 1
                time.sleep(max(0.0, interval - (time.monotonic() - started)))

    collector_seen: list[str] = []

    def watch_collector() -> None:
        deadline = time.monotonic() + args.seconds
        with httpx.Client(timeout=5) as client:
            while time.monotonic() < deadline:
                page = client.get(api_ops + "/metrics").text
                if 'nexa_collector_up{collector="checkpoint"} 0.0' in page:
                    if not collector_seen or collector_seen[-1] != "down":
                        collector_seen.append("down")
                        mark("checkpoint collector down (series dropped)")
                elif (
                    'nexa_collector_up{collector="checkpoint"} 1.0' in page
                    and collector_seen
                    and collector_seen[-1] == "down"
                ):
                    collector_seen.append("up")
                    mark("checkpoint collector up again")
                time.sleep(0.25)

    threads = [threading.Thread(target=hit_metrics), threading.Thread(target=watch_collector)]
    mark("metrics hammer start", rate=args.rate, seconds=args.seconds)
    for thread in threads:
        thread.start()
    lock_codes = []
    for _ in range(LOCKS):
        mark("lock checkpoints", seconds=LOCK_SECONDS)
        lock_codes.append(_lock_checkpoints(database))
        time.sleep(LOCK_SECONDS)
    for thread in threads:
        thread.join()
    latencies = sorted(hammer.pop("latency_ms"))
    hammer.update(
        {
            "requests": len(latencies),
            "p50_ms": round(statistics.median(latencies), 1) if latencies else None,
            "p99_ms": round(latencies[int(len(latencies) * 0.99) - 1], 1) if latencies else None,
            "max_ms": round(latencies[-1], 1) if latencies else None,
        }
    )
    mark("metrics hammer end", **{k: v for k, v in hammer.items() if k != "codes"})
    report["hammer"] = {**hammer, "codes": {str(k): v for k, v in hammer["codes"].items()}}
    report["locks"] = lock_codes
    report["collector_transitions"] = collector_seen

    progress: list[str] = []

    def all_terminal() -> dict[str, str] | None:
        states = _states(rests, accepted)
        counts = {
            state: list(states.values()).count(state) for state in sorted(set(states.values()))
        }
        if str(counts) != (progress[-1] if progress else None):
            progress.append(str(counts))
            mark("job states", **counts)
        return states if all(state in TERMINAL for state in states.values()) else None

    final = wait_until(all_terminal, "every accepted job terminal", 900, 2)
    mark("all jobs terminal", states=sorted(set(final.values())))
    report["jobs"] = final
    after = metrics(api_ops)
    report["api_5xx_after"] = total(after, "nexa_http_requests_total", status_class="5xx")
    report["api_requests_by_class"] = {
        cls: total(after, "nexa_http_requests_total", status_class=cls)
        for cls in ("2xx", "3xx", "4xx", "5xx")
    }
    report["api_4xx_by_route"] = {
        series: value
        for series, value in after.items()
        if series.startswith("nexa_http_requests_total{") and 'status_class="4xx"' in series
    }
    report["collector_errors_total"] = total(after, "nexa_collector_errors_total")
    report["script_rest_statuses"] = {}
    for rest in rests.values():
        for code, count in rest.statuses.items():
            key = str(code)
            report["script_rest_statuses"][key] = report["script_rest_statuses"].get(key, 0) + count
        rest.close()
    # Final row counts once the worker has released every allocation.
    wait_until(
        lambda: (
            psql(
                database, "SELECT count(*) FROM allocations WHERE state IN ('HELD', 'QUARANTINED')"
            )[0][0]
            == "0"
        ),
        "the allocation releases",
        120,
    )
    rows_after = row_counts(database)
    report["rows"] = {"before": rows_before, "after": rows_after}
    report["audit_added"] = rows_after["audit_total"] - rows_before["audit_total"]
    report["events_added"] = rows_after["events"] - rows_before["events"]
    write_json(args.out, report)

    failures = []
    if any(state != "SUCCEEDED" for state in final.values()):
        failures.append("jobs")
    if report["api_5xx_after"] != report["api_5xx_before"]:
        failures.append("api 5xx")
    if any(code.startswith("5") for code in report["script_rest_statuses"]):
        failures.append("script 5xx")
    if hammer["errors"] or set(report["hammer"]["codes"]) != {"200"}:
        failures.append("metrics")
    if "down" not in collector_seen or any(lock_codes):
        failures.append("collector")
    log(f"failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
