"""Shared helpers of the B19 scrape and outage scripts (run under b17_e2e_stack.py).

They read the harness fixture (NEXA_B17_FIXTURE) and talk to the stack only over
its public surfaces: REST through Caddy, the loopback ops listeners, the
Prometheus HTTP API and read-only `psql` counts in the test database container.
Nothing secret is printed or written: passwords stay in the fixture, cookies and
CSRF tokens in memory.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

GIB = 1024**3
PG_CONTAINER = os.environ.get("NEXA_B19_PG_CONTAINER", "nexa_b13_pg")
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def log(message: str) -> None:
    print(f"[b19] {now()} {message}", flush=True)


def now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3] + "Z"


def fixture() -> dict:
    return json.loads(Path(os.environ["NEXA_B17_FIXTURE"]).read_text())


def wait_until(predicate, detail: str, timeout: float, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise RuntimeError(f"timed out waiting for {detail}")


class Rest:
    """One logged-in browser-style session through Caddy (TLS, Origin, CSRF)."""

    def __init__(self, seed: dict, user: str) -> None:
        self.client = httpx.Client(
            base_url=seed["base_url"],
            verify=seed["ca_file"],
            timeout=30,
            headers={"Origin": seed["base_url"]},
        )
        account = seed["users"][user]
        login = self.client.post(
            "/v1/auth/login",
            json={"username": account["username"], "password": account["password"]},
        )
        if login.status_code != 200:
            raise RuntimeError(f"login {user}: HTTP {login.status_code}")
        self.csrf = login.json()["csrf_token"]
        self.statuses: dict[int, int] = {}

    def request(self, method: str, path: str, tenant_id: str, **kwargs) -> httpx.Response:
        headers = {"X-Nexa-Tenant-Id": tenant_id, **kwargs.pop("headers", {})}
        if method != "GET":
            headers.update(
                {"X-CSRF-Token": self.csrf, "Idempotency-Key": f"b19-{secrets.token_hex(8)}"}
            )
        response = self.client.request(method, path, headers=headers, **kwargs)
        self.statuses[response.status_code] = self.statuses.get(response.status_code, 0) + 1
        return response

    def submit_cpu(
        self,
        tenant_id: str,
        input_artifact_id: str,
        seed: int,
        iterations: int,
        memory_bytes: int = GIB,
    ):
        spec = {
            "template_id": "cpu-iterative",
            "template_version": 1,
            "input_artifact_id": input_artifact_id,
            "resources": {"cpu_millis": 1000, "memory_bytes": memory_bytes, "gpu_count": 0},
            "priority": 1,
            "runtime_limit_seconds": 300,
            "checkpoint_interval_seconds": 30,
            "parameters": {"iterations": iterations, "seed": seed, "modulus": 1_000_003},
        }
        response = self.request("POST", "/v1/jobs", tenant_id, json={"spec": spec})
        if response.status_code != 202:
            raise RuntimeError(f"submit: HTTP {response.status_code} {response.text[:200]}")
        return response.json()["job_id"]

    def job_state(self, tenant_id: str, job_id: str) -> str:
        response = self.request("GET", f"/v1/jobs/{job_id}", tenant_id)
        if response.status_code != 200:
            raise RuntimeError(f"read job: HTTP {response.status_code}")
        body = response.json()
        return body.get("job", body)["state"]

    def close(self) -> None:
        self.client.close()


def metrics(url: str) -> dict[str, float]:
    """Parse a Prometheus text page into {"name{labels}": value} (no help/type lines)."""
    page = httpx.get(url + "/metrics", timeout=5)
    page.raise_for_status()
    values = {}
    for line in page.text.splitlines():
        if line and not line.startswith("#"):
            series, _, value = line.rpartition(" ")
            values[series] = float(value)
    return values


def total(values: dict[str, float], name: str, **labels: str) -> float:
    """Sum the samples of `name` whose labels include `labels`."""
    result = 0.0
    for series, value in values.items():
        named = series == name or series.startswith(name + "{")
        if named and all(f'{key}="{label}"' in series for key, label in labels.items()):
            result += value
    return result


def prometheus_query(url: str, query: str) -> list[dict]:
    response = httpx.get(url + "/api/v1/query", params={"query": query}, timeout=10)
    response.raise_for_status()
    return response.json()["data"]["result"]


def psql(database: str, sql: str) -> list[list[str]]:
    """Read-only query in the test DB container (its local socket; no password used)."""
    if not database.startswith("nexa_b05_test_"):
        raise SystemExit("refusing a non-test database")
    result = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", "postgres", "-d", database, "-AtF", "\t"]
        + ["-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql failed ({result.returncode}): {result.stderr[-300:]}")
    return [line.split("\t") for line in result.stdout.splitlines() if line]


def row_counts(database: str) -> dict:
    """Audit rows by actor/action and the event total (B19-R16, AC-14)."""
    audit = psql(
        database,
        "SELECT actor_type, action, count(*) FROM audit_records GROUP BY 1, 2 ORDER BY 1, 2",
    )
    events = psql(database, "SELECT count(*) FROM events")
    return {
        "audit": {f"{actor} {action}": int(count) for actor, action, count in audit},
        "audit_total": sum(int(count) for _, _, count in audit),
        "events": int(events[0][0]),
    }


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=60, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr[-300:]}")
    return result.stdout.strip()


def write_json(path: str | None, payload: dict) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path:
        Path(path).write_text(text)
        log(f"wrote {path}")
    else:
        print(text)
