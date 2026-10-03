"""B18 `nexa admin job` and `nexa admin fairness` over the HTTP API and PostgreSQL."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import update

from nexa.infrastructure.persistence import schema as s
from tests.api.test_http_contract import _bootstrap_and_login, _client
from tests.integration._factories import seed_job, seed_tenant_graph
from tests.integration.test_cli_b12_vertical import _cli, _success, _token
from tests.integration.test_jobs_b08 import _running_api_process, _submit_body

pytestmark = pytest.mark.postgres


def test_admin_read_token_lists_jobs_and_queries_fairness(
    migrated_postgres_engine, tmp_path: Path
) -> None:
    engine = migrated_postgres_engine
    with engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="b18-cli", template_id="cpu-iterative")
        spec = _submit_body(graph["artifact_id"])["spec"]
        job_id = str(seed_job(connection, graph, state="QUEUED", canonical_spec=spec)["job_id"])
        connection.execute(
            update(s.jobs)
            .where(s.jobs.c.job_id == job_id)
            .values(event_sequence=1, waiting_reason="waiting_for_capacity")
        )
    with _client(engine, tmp_path) as bootstrap:
        login, _ = _bootstrap_and_login(bootstrap)
        read_token = _token(
            bootstrap,
            login["csrf_token"],
            name="b18-admin-read",
            scopes=["admin:read"],
            key="b18-cli-admin-read-token-0001",
        )
        # Admin scopes are exact: admin:write alone does not grant the read operations.
        write_token = _token(
            bootstrap,
            login["csrf_token"],
            name="b18-admin-write",
            scopes=["admin:write"],
            key="b18-cli-admin-write-token-0001",
        )

    with _running_api_process(engine, tmp_path) as (base_url, _process):
        listing = _success(
            base_url,
            read_token,
            tmp_path,
            "admin",
            "job",
            "list",
            "--tenant-id",
            str(graph["tenant_id"]),
            "--state",
            "QUEUED",
        )
        assert [item["job_id"] for item in listing["items"]] == [job_id]
        assert listing["items"][0]["waiting_reason"] == "waiting_for_capacity"

        detail = _success(base_url, read_token, tmp_path, "admin", "job", "get", job_id)
        assert detail["job_id"] == job_id
        assert detail["etag"] == f'"v{detail["version"]}"'

        report = _success(
            base_url,
            read_token,
            tmp_path,
            "admin",
            "fairness",
            "query",
            "--from",
            "2026-09-30T00:00:00Z",
            "--to",
            "2026-09-30T01:00:00Z",
            "--bucket-seconds",
            "300",
        )
        assert report["bucket_seconds"] == 300
        assert report["buckets"] == []

        too_long, too_long_error = _cli(
            base_url,
            read_token,
            tmp_path,
            "admin",
            "fairness",
            "query",
            "--from",
            "2026-08-01T00:00:00Z",
            "--to",
            "2026-09-30T00:00:00Z",
            "--bucket-seconds",
            "86400",
        )
        assert too_long.returncode == 9
        assert too_long_error["status"] == 400

        for args in (
            ("admin", "job", "list"),
            ("admin", "job", "get", job_id),
            ("admin", "fairness", "query", "--from", "2026-09-30T00:00:00Z")
            + ("--to", "2026-09-30T01:00:00Z", "--bucket-seconds", "300"),
        ):
            denied, error = _cli(base_url, write_token, tmp_path, *args)
            assert denied.returncode == 4, error
            assert error["status"] == 403
