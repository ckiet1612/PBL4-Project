import json
from getpass import fallback_getpass

import httpx
import pytest
from typer.testing import CliRunner

from nexa.cli.app import app


def _patch_client(monkeypatch, responder):
    from nexa.cli.client import NexaClient

    monkeypatch.setattr(
        "nexa.cli.commands.admin.NexaClient",
        lambda *args, **kwargs: NexaClient(
            *args, transport=httpx.MockTransport(responder), retry_backoff=0, **kwargs
        ),
    )


def test_admin_update_tenant_forwards_exact_etag_and_key(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"tenant_id": "t1", "version": 2}, request=request)

    _patch_client(monkeypatch, respond)
    result = CliRunner().invoke(
        app,
        [
            "--output",
            "json",
            "admin",
            "tenant",
            "update",
            "t1",
            "--display-name",
            "New",
            "--if-match",
            '"v1"',
            "--idempotency-key",
            "tenant-key-123456",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert requests[0].method == "PATCH"
    assert requests[0].url.path == "/v1/admin/tenants/t1"
    assert requests[0].headers["if-match"] == '"v1"'
    assert requests[0].headers["idempotency-key"] == "tenant-key-123456"
    assert json.loads(requests[0].content) == {"display_name": "New"}


JOB_ID = "01890a5d-ac96-7000-8000-000000000002"
TENANT_ID = "01890a5d-ac96-7000-8000-000000000001"


def _recording(monkeypatch, status=200, body=None):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json=body if body is not None else {}, request=request)

    _patch_client(monkeypatch, respond)
    return requests


def test_admin_job_list_forwards_filters_without_tenant_header(monkeypatch):
    requests = _recording(monkeypatch, body={"items": [], "page": {"page_size": 25}})
    result = CliRunner().invoke(
        app,
        [
            "--output",
            "json",
            "admin",
            "job",
            "list",
            "--tenant-id",
            TENANT_ID,
            "--state",
            "QUEUED",
            "--waiting-reason",
            "waiting_for_quota",
            "--created-after",
            "2026-09-30T00:00:00Z",
            "--page-size",
            "25",
            "--cursor",
            "a+/=",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    request = requests[0]
    assert (request.method, request.url.path) == ("GET", "/v1/admin/jobs")
    assert dict(request.url.params) == {
        "tenant_id": TENANT_ID,
        "state": "QUEUED",
        "waiting_reason": "waiting_for_quota",
        "created_after": "2026-09-30T00:00:00Z",
        "page_size": "25",
        "cursor": "a+/=",
    }
    assert "x-nexa-tenant-id" not in request.headers
    assert "idempotency-key" not in request.headers


def test_admin_job_get_reads_one_job(monkeypatch):
    requests = _recording(monkeypatch, body={"job_id": JOB_ID, "version": 3})
    result = CliRunner().invoke(app, ["--output", "json", "admin", "job", "get", JOB_ID])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (requests[0].method, requests[0].url.path) == ("GET", f"/v1/admin/jobs/{JOB_ID}")


def test_admin_fairness_query_forwards_the_window(monkeypatch):
    requests = _recording(monkeypatch, body={"buckets": []})
    result = CliRunner().invoke(
        app,
        [
            "--output",
            "json",
            "admin",
            "fairness",
            "query",
            "--from",
            "2026-09-30T00:00:00Z",
            "--to",
            "2026-09-30T01:00:00+07:00",
            "--bucket-seconds",
            "300",
            "--tenant-id",
            TENANT_ID,
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert requests[0].url.path == "/v1/admin/fairness"
    assert dict(requests[0].url.params) == {
        "from": "2026-09-30T00:00:00Z",
        "to": "2026-09-30T01:00:00+07:00",
        "bucket_seconds": "300",
        "tenant_id": TENANT_ID,
    }


@pytest.mark.parametrize(
    "args",
    [
        ["admin", "fairness", "query", "--to", "2026-09-30T01:00:00Z", "--bucket-seconds", "60"],
        ["admin", "fairness", "query", "--from", "2026-09-30T00:00:00Z", "--bucket-seconds", "60"],
        ["admin", "fairness", "query", "--from", "2026-09-30T00:00:00Z", "--to", "2026-09-30T01Z"],
        ["admin", "fairness", "query", "--from", "a", "--to", "b", "--bucket-seconds", "x"],
        ["admin", "job", "get"],
        ["admin", "job", "list", "--page-size", "0"],
        ["admin", "job", "list", "--page-size", "101"],
    ],
)
def test_admin_read_commands_reject_bad_input_before_transport(monkeypatch, args):
    requests = _recording(monkeypatch)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2, result.stdout + result.stderr
    assert requests == []


def test_admin_user_create_reads_password_from_stdin_and_defaults_system_roles(
    monkeypatch,
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(201, json={"user_id": "u1"}, request=request)

    _patch_client(monkeypatch, respond)
    result = CliRunner().invoke(
        app,
        [
            "--output",
            "json",
            "admin",
            "user",
            "create",
            "--username",
            "user@example.test",
            "--display-name",
            "User",
            "--password-stdin",
        ],
        input="correct-horse-battery-staple\n",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "correct-horse-battery-staple" not in result.stdout + result.stderr
    assert json.loads(requests[0].content) == {
        "username": "user@example.test",
        "display_name": "User",
        "password": "correct-horse-battery-staple",
        "system_roles": [],
    }


def test_admin_user_create_does_not_accept_password_command_option():
    result = CliRunner().invoke(
        app,
        [
            "admin",
            "user",
            "create",
            "--username",
            "user@example.test",
            "--display-name",
            "User",
            "--password",
            "unsafe-command-line-secret",
        ],
    )
    assert result.exit_code == 2
    assert "No such option" in result.stderr


def test_admin_user_create_uses_hidden_confirmation_prompt(monkeypatch):
    requests = []
    prompts = []

    def respond(request):
        requests.append(request)
        return httpx.Response(201, json={"user_id": "u1"}, request=request)

    def hidden_prompt(label):
        prompts.append(label)
        return "correct-horse-battery-staple"

    _patch_client(monkeypatch, respond)
    monkeypatch.setattr("nexa.cli.commands.admin.getpass", hidden_prompt)
    result = CliRunner().invoke(
        app,
        [
            "--output",
            "json",
            "admin",
            "user",
            "create",
            "--username",
            "user@example.test",
            "--display-name",
            "User",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert prompts == ["Password: ", "Confirm password: "]
    assert json.loads(requests[0].content)["password"] == "correct-horse-battery-staple"


def test_admin_user_create_refuses_getpass_echo_fallback_before_reading(monkeypatch):
    secret = "unsafe-visible-password"
    monkeypatch.setattr("nexa.cli.commands.admin.getpass", fallback_getpass)
    monkeypatch.setattr(
        "nexa.cli.commands.admin._client",
        lambda _ctx: pytest.fail("password prompt failure reached HTTP transport"),
    )
    result = CliRunner().invoke(
        app,
        [
            "admin",
            "user",
            "create",
            "--username",
            "user@example.test",
            "--display-name",
            "User",
        ],
        input=f"{secret}\n{secret}\n",
    )
    assert result.exit_code == 2, result.stdout + result.stderr
    assert secret not in result.stdout + result.stderr
    assert "unable to read password securely" in result.stderr


def test_admin_delete_membership_preserves_etag_on_empty_response(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(204, headers={"ETag": '"v2"'}, request=request)

    _patch_client(monkeypatch, respond)
    result = CliRunner().invoke(
        app,
        [
            "--output",
            "json",
            "admin",
            "membership",
            "delete",
            "tenant-1",
            "user-1",
            "--if-match",
            '"v1"',
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert requests[0].headers["if-match"] == '"v1"'
    assert json.loads(result.stdout) == {"etag": '"v2"'}


def test_admin_create_tenant_body_file_rejects_unknown_top_level_field(monkeypatch, tmp_path):
    body = tmp_path / "tenant.json"
    body.write_text('{"slug":"tenant-one","display_name":"Tenant","unexpected":true}')
    result = CliRunner().invoke(app, ["admin", "tenant", "create", "--body-file", str(body)])
    assert result.exit_code == 2
    assert "unknown field" in result.stderr


@pytest.mark.parametrize(
    "body",
    [
        '{"slug":"tenant-one","slug":"tenant-two","display_name":"Tenant"}',
        '{"slug":"tenant-one","display_name":"Tenant","extra":{"x":1,"x":2}}',
        '{"slug":"tenant-one","display_name":"Tenant","extra":NaN}',
        '{"slug":"tenant-one","display_name":"Tenant","extra":Infinity}',
        '{"slug":"tenant-one","display_name":"Tenant","extra":1e9999}',
    ],
)
def test_admin_body_file_rejects_ambiguous_or_nonfinite_json_before_transport(
    monkeypatch, tmp_path, body
):
    monkeypatch.setattr(
        "nexa.cli.commands.admin._client",
        lambda _ctx: pytest.fail("invalid JSON reached transport"),
    )
    path = tmp_path / "tenant.json"
    path.write_text(body, encoding="utf-8")
    result = CliRunner().invoke(app, ["admin", "tenant", "create", "--body-file", str(path)])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "request body JSON is invalid" in result.stderr


@pytest.mark.parametrize(
    "resource",
    [
        '{"cpu_millis":1000,"cpu_millis":2000}',
        '{"cpu_millis":NaN}',
        '{"cpu_millis":-Infinity}',
        '{"cpu_millis":1e9999}',
    ],
)
def test_admin_resource_limit_rejects_ambiguous_or_nonfinite_json_before_transport(
    monkeypatch, resource
):
    monkeypatch.setattr(
        "nexa.cli.commands.admin._client",
        lambda _ctx: pytest.fail("invalid JSON reached transport"),
    )
    result = CliRunner().invoke(
        app,
        [
            "admin",
            "tenant-policy",
            "update",
            "t1",
            "--resource-limit",
            resource,
            "--if-match",
            '"v1"',
        ],
    )
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "resource-limit JSON is invalid" in result.stderr
