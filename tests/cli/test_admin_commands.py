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


def test_unwired_admin_job_group_is_not_exposed():
    result = CliRunner().invoke(
        app,
        [
            "admin",
            "job",
            "list",
        ],
    )
    assert result.exit_code == 2
    assert "No such command" in result.stderr


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
