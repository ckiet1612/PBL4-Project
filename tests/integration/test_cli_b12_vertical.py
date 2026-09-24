"""Opt-in product CLI to HTTP API and PostgreSQL contract slice."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from nexa.infrastructure.persistence.schema import cli_tokens, jobs
from tests.api.test_http_contract import _bootstrap_and_login, _client
from tests.integration.test_jobs_b08 import (
    _running_api_process,
    _seed_cpu_inputs,
    _submit_body,
)

pytestmark = pytest.mark.postgres


def _token(client, csrf: str, *, name: str, scopes: list[str], key: str) -> str:
    response = client.post(
        "/v1/tokens",
        headers={
            "Origin": "https://nexa.test",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": key,
        },
        json={"name": name, "scopes": scopes, "expires_in_seconds": 300},
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def _cli(base_url: str, token: str, tmp_path: Path, *args: str, input_text: str | None = None):
    __tracebackhide__ = True
    repository = Path(__file__).resolve().parents[2]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("NEXA_")}
    environment.update(
        {
            "PYTHONPATH": str(repository / "src"),
            "XDG_CONFIG_HOME": str(tmp_path / "cli-config"),
            "NEXA_ENDPOINT": base_url,
            "NEXA_TOKEN": token,
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "nexa.cli.app", "--output", "json", *args],
        cwd=repository,
        env=environment,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, json.loads(result.stdout if result.returncode == 0 else result.stderr)


def _success(base_url: str, token: str, tmp_path: Path, *args: str, input_text: str | None = None):
    __tracebackhide__ = True
    result, payload = _cli(base_url, token, tmp_path, *args, input_text=input_text)
    assert result.returncode == 0, payload
    return payload


def test_product_cli_http_postgres_vertical_and_authorization(
    migrated_postgres_engine, tmp_path: Path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as bootstrap:
        login, _ = _bootstrap_and_login(bootstrap)
        admin_token = _token(
            bootstrap,
            login["csrf_token"],
            name="b12-admin",
            scopes=["admin:read", "admin:write"],
            key="b12-cli-admin-token-0001",
        )
        admin_read_token = _token(
            bootstrap,
            login["csrf_token"],
            name="b12-admin-read",
            scopes=["admin:read"],
            key="b12-cli-admin-read-token-0001",
        )

    with _running_api_process(migrated_postgres_engine, tmp_path) as (base_url, _process):
        tenant_args = (
            "admin",
            "tenant",
            "create",
            "--slug",
            "b12-cli-team",
            "--display-name",
            "B12 CLI Team",
            "--idempotency-key",
            "b12-cli-tenant-create-0001",
        )
        tenant = _success(base_url, admin_token, tmp_path, *tenant_args)
        tenant_replay = _success(base_url, admin_token, tmp_path, *tenant_args)
        assert tenant_replay["tenant_id"] == tenant["tenant_id"]
        tenant_id = tenant["tenant_id"]
        fetched_tenant = _success(
            base_url, admin_token, tmp_path, "admin", "tenant", "get", tenant_id
        )
        assert fetched_tenant["etag"] == '"v1"'
        assert (
            _success(base_url, admin_read_token, tmp_path, "admin", "tenant", "get", tenant_id)[
                "tenant_id"
            ]
            == tenant_id
        )
        denied_admin_write, admin_write_error = _cli(
            base_url,
            admin_read_token,
            tmp_path,
            "admin",
            "tenant",
            "create",
            "--slug",
            "b12-cli-denied",
            "--display-name",
            "Denied",
            "--idempotency-key",
            "b12-cli-denied-admin-write-0001",
        )
        assert denied_admin_write.returncode == 4
        assert admin_write_error["status"] == 403
        invalid_page, page_error = _cli(
            base_url,
            admin_read_token,
            tmp_path,
            "admin",
            "tenant",
            "list",
            "--page-size",
            "101",
        )
        assert invalid_page.returncode == 2
        assert page_error["exit_code"] == 2

        stale, stale_error = _cli(
            base_url,
            admin_token,
            tmp_path,
            "admin",
            "tenant",
            "update",
            tenant_id,
            "--display-name",
            "Updated B12 Team",
            "--if-match",
            '"v2"',
            "--idempotency-key",
            "b12-cli-tenant-stale-0001",
        )
        assert stale.returncode == 6
        assert stale_error["status"] == 412
        updated = _success(
            base_url,
            admin_token,
            tmp_path,
            "admin",
            "tenant",
            "update",
            tenant_id,
            "--display-name",
            "Updated B12 Team",
            "--if-match",
            fetched_tenant["etag"],
            "--idempotency-key",
            "b12-cli-tenant-update-0001",
        )
        assert updated["etag"] == '"v2"'
        assert (
            _success(base_url, admin_token, tmp_path, "admin", "tenant", "get", tenant_id)[
                "display_name"
            ]
            == "Updated B12 Team"
        )

        user = _success(
            base_url,
            admin_token,
            tmp_path,
            "admin",
            "user",
            "create",
            "--username",
            "b12-cli-member@example.test",
            "--display-name",
            "B12 CLI Member",
            "--password-stdin",
            "--idempotency-key",
            "b12-cli-user-create-0001",
            input_text="b12-cli-member-password\n",
        )
        user_id = user["user_id"]
        membership_set = _success(
            base_url, admin_token, tmp_path, "admin", "membership", "list", tenant_id
        )
        assert membership_set["etag"] == '"v1"'
        membership = _success(
            base_url,
            admin_token,
            tmp_path,
            "admin",
            "membership",
            "upsert",
            tenant_id,
            "--user-id",
            user_id,
            "--role",
            "MEMBER",
            "--if-match",
            membership_set["etag"],
            "--idempotency-key",
            "b12-cli-membership-upsert-0001",
        )
        assert membership["etag"] == '"v2"'
        _seed_cpu_inputs(migrated_postgres_engine, tenant_id)

        with _client(migrated_postgres_engine, tmp_path) as member:
            login = member.post(
                "/v1/auth/login",
                headers={"Origin": "https://nexa.test"},
                json={
                    "username": "b12-cli-member@example.test",
                    "password": "b12-cli-member-password",
                },
            )
            assert login.status_code == 200, login.text
            member_token = _token(
                member,
                login.json()["csrf_token"],
                name="b12-member",
                scopes=["jobs:read", "jobs:write", "artifacts:read", "artifacts:write"],
                key="b12-cli-member-token-0001",
            )
            read_token = _token(
                member,
                login.json()["csrf_token"],
                name="b12-read-only",
                scopes=["jobs:read"],
                key="b12-cli-read-token-0001",
            )

        source = tmp_path / "input.bin"
        source.write_bytes(b"b12-cli-input")
        upload_args = (
            "artifact",
            "upload",
            str(source),
            "--kind",
            "INPUT",
            "--media-type",
            "application/vnd.nexa.cpu-iterative-input+json",
            "--tenant",
            tenant_id,
            "--idempotency-key",
            "b12-cli-upload-0001",
        )
        uploaded = _success(base_url, member_token, tmp_path, *upload_args)
        upload_replay = _success(base_url, member_token, tmp_path, *upload_args)
        assert upload_replay["artifact_id"] == uploaded["artifact_id"]
        assert upload_replay["checksum"] == uploaded["checksum"]
        artifact_id = uploaded["artifact_id"]

        spec_file = tmp_path / "job-spec.json"
        spec_file.write_text(json.dumps(_submit_body(artifact_id)["spec"]), encoding="utf-8")
        submit_args = (
            "job",
            "submit",
            "--spec-file",
            str(spec_file),
            "--tenant",
            tenant_id,
            "--idempotency-key",
            "b12-cli-submit-0001",
        )
        submitted = _success(base_url, member_token, tmp_path, *submit_args)
        submit_replay = _success(base_url, member_token, tmp_path, *submit_args)
        assert submit_replay["job_id"] == submitted["job_id"]
        assert submit_replay["etag"] == submitted["etag"]
        job_id = submitted["job_id"]
        assert submitted["state"] == "QUEUED"
        assert submitted["etag"] == '"v1"'
        assert (
            _success(base_url, member_token, tmp_path, "job", "get", job_id, "--tenant", tenant_id)[
                "job_id"
            ]
            == job_id
        )
        assert (
            _success(
                base_url,
                member_token,
                tmp_path,
                "job",
                "session",
                submitted["session_id"],
                "--tenant",
                tenant_id,
            )["session_id"]
            == submitted["session_id"]
        )
        events = _success(
            base_url, member_token, tmp_path, "job", "events", job_id, "--tenant", tenant_id
        )
        assert events["items"][0]["sequence"] == 1
        pending_result, pending_error = _cli(
            base_url, member_token, tmp_path, "job", "result", job_id, "--tenant", tenant_id
        )
        assert pending_result.returncode == 5
        assert pending_error["status"] == 404

        destination = tmp_path / "downloaded.bin"
        downloaded = _success(
            base_url,
            member_token,
            tmp_path,
            "artifact",
            "download",
            artifact_id,
            "--output-file",
            str(destination),
            "--checksum",
            uploaded["checksum"],
            "--tenant",
            tenant_id,
        )
        assert downloaded["size"] == len(source.read_bytes())
        assert destination.read_bytes() == source.read_bytes()
        bad_destination = tmp_path / "bad-checksum.bin"
        bad_download, bad_error = _cli(
            base_url,
            member_token,
            tmp_path,
            "artifact",
            "download",
            artifact_id,
            "--output-file",
            str(bad_destination),
            "--checksum",
            "sha256:" + "0" * 64,
            "--tenant",
            tenant_id,
        )
        assert bad_download.returncode == 10
        assert bad_error["exit_code"] == 10
        assert not bad_destination.exists()

        denied_admin, admin_error = _cli(
            base_url, member_token, tmp_path, "admin", "tenant", "list"
        )
        assert denied_admin.returncode == 4
        assert admin_error["status"] == 403
        denied_submit, submit_error = _cli(base_url, read_token, tmp_path, *submit_args)
        assert denied_submit.returncode == 4
        assert submit_error["status"] == 403

        other_tenant = _success(
            base_url,
            admin_token,
            tmp_path,
            "admin",
            "tenant",
            "create",
            "--slug",
            "b12-cli-other",
            "--display-name",
            "B12 Other",
            "--idempotency-key",
            "b12-cli-other-tenant-0001",
        )
        denied_job, job_error = _cli(
            base_url,
            member_token,
            tmp_path,
            "job",
            "get",
            job_id,
            "--tenant",
            other_tenant["tenant_id"],
        )
        assert denied_job.returncode in {4, 5}
        assert job_error["status"] in {403, 404}
        assert denied_job.stdout == ""

        loss_args = (*submit_args[:-1], "b12-cli-submit-response-loss-0001")
        with _running_api_process(
            migrated_postgres_engine,
            tmp_path,
            app_target="tests.integration.response_loss_app:app",
        ) as (loss_url, loss_process):
            lost, lost_error = _cli(loss_url, member_token, tmp_path, *loss_args)
            assert lost.returncode == 8
            assert lost_error["exit_code"] == 8
            assert loss_process.wait(timeout=5) == 91
        recovered = _success(base_url, member_token, tmp_path, *loss_args)
        assert recovered["job_id"] != job_id
        assert (
            _success(base_url, member_token, tmp_path, *loss_args)["job_id"] == recovered["job_id"]
        )
        with migrated_postgres_engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(jobs)).scalar_one() == 2

        deleted = _success(
            base_url,
            admin_token,
            tmp_path,
            "admin",
            "membership",
            "delete",
            tenant_id,
            user_id,
            "--if-match",
            membership["etag"],
            "--idempotency-key",
            "b12-cli-membership-delete-0001",
        )
        assert deleted["etag"] == '"v3"'
        assert (
            _success(base_url, admin_token, tmp_path, "admin", "membership", "list", tenant_id)[
                "etag"
            ]
            == deleted["etag"]
        )


def test_cli_token_response_loss_does_not_mint_a_second_secret(
    migrated_postgres_engine, tmp_path: Path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as bootstrap:
        login, _ = _bootstrap_and_login(bootstrap)
        manager_token = _token(
            bootstrap,
            login["csrf_token"],
            name="b12-token-manager",
            scopes=["tokens:write"],
            key="b12-cli-manager-token-0001",
        )

    args = (
        "token",
        "create",
        "--scope",
        "tokens:write",
        "--name",
        "b12-lost-token",
        "--idempotency-key",
        "b12-cli-token-response-loss-0001",
    )
    with _running_api_process(migrated_postgres_engine, tmp_path) as (base_url, _process):
        with _running_api_process(
            migrated_postgres_engine,
            tmp_path,
            app_target="tests.integration.token_response_loss_app:app",
        ) as (loss_url, loss_process):
            lost, lost_error = _cli(loss_url, manager_token, tmp_path, *args)
            assert lost.returncode == 8
            assert lost.stdout == ""
            assert lost_error["exit_code"] == 8
            assert loss_process.wait(timeout=5) == 91
        replay, replay_error = _cli(base_url, manager_token, tmp_path, *args)
        assert replay.returncode == 6
        assert replay.stdout == ""
        assert replay_error["status"] == 409
        assert replay_error["code"] == "one_time_secret_unavailable"
        with migrated_postgres_engine.connect() as connection:
            count = connection.execute(
                select(func.count())
                .select_from(cli_tokens)
                .where(cli_tokens.c.name == "b12-lost-token")
            ).scalar_one()
            assert count == 1
