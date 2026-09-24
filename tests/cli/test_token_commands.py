import json

import httpx
from typer.testing import CliRunner

from nexa.cli.app import app
from nexa.cli.config import ConfigStore


def test_token_create_persists_first_successful_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            201,
            json={"token_id": "id-1", "token": "opaque"},
            headers={"Location": "/v1/tokens/id-1"},
            request=request,
        )

    monkeypatch.setattr(
        "nexa.cli.commands.token.NexaClient",
        lambda *a, **kw: __import__("nexa.cli.client", fromlist=["NexaClient"]).NexaClient(
            *a,
            transport=httpx.MockTransport(respond),
            retry_backoff=0,
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )
    result = CliRunner().invoke(
        app,
        [
            "--output",
            "json",
            "token",
            "create",
            "--scope",
            "jobs:read",
            "--persist",
            "--idempotency-key",
            "fixed-key-123456",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["token_id"] == "id-1"
    assert ConfigStore(tmp_path / "nexa").get_token("default") == "opaque"
    assert requests[0].headers["idempotency-key"] == "fixed-key-123456"


def test_token_scope_validation_is_local(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["token", "create", "--scope", "unknown:scope"])
    assert result.exit_code == 2
    assert "invalid token scope" in result.stderr


def test_token_list_forwards_cursor_and_page_size(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"items": [], "page": {}}, request=request)

    from nexa.cli.client import NexaClient

    monkeypatch.setattr(
        "nexa.cli.commands.token.NexaClient",
        lambda *a, **kw: NexaClient(
            *a, transport=httpx.MockTransport(respond), retry_backoff=0, **kw
        ),
    )
    result = CliRunner().invoke(
        app,
        [
            "--output",
            "json",
            "token",
            "list",
            "--cursor",
            "opaque-cursor",
            "--page-size",
            "50",
        ],
    )
    assert result.exit_code == 0
    assert requests[0].url.params["cursor"] == "opaque-cursor"
    assert requests[0].url.params["page_size"] == "50"


def test_token_from_environment_is_sent_as_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("NEXA_TOKEN", "environment-token")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"items": [], "page": {}}, request=request)

    from nexa.cli.client import NexaClient

    monkeypatch.setattr(
        "nexa.cli.commands.token.NexaClient",
        lambda *a, **kw: NexaClient(
            *a, transport=httpx.MockTransport(respond), retry_backoff=0, **kw
        ),
    )
    result = CliRunner().invoke(app, ["token", "list"])
    assert result.exit_code == 0
    assert requests[0].headers["authorization"] == "Bearer environment-token"


def test_token_from_stdin_is_sent_as_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"items": [], "page": {}}, request=request)

    from nexa.cli.client import NexaClient

    monkeypatch.setattr(
        "nexa.cli.commands.token.NexaClient",
        lambda *a, **kw: NexaClient(
            *a, transport=httpx.MockTransport(respond), retry_backoff=0, **kw
        ),
    )
    result = CliRunner().invoke(app, ["--token-stdin", "token", "list"], input="stdin-token\n")
    assert result.exit_code == 0
    assert requests[0].headers["authorization"] == "Bearer stdin-token"


def test_token_persist_failure_still_displays_one_time_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def respond(request):
        return httpx.Response(201, json={"token_id": "id-1", "token": "one-time"}, request=request)

    from nexa.cli.client import NexaClient
    from nexa.cli.errors import CliError

    monkeypatch.setattr(
        "nexa.cli.commands.token.NexaClient",
        lambda *a, **kw: NexaClient(
            *a, transport=httpx.MockTransport(respond), retry_backoff=0, **kw
        ),
    )
    monkeypatch.setattr(
        "nexa.cli.commands.token.ConfigStore.save_token",
        lambda *a, **kw: (_ for _ in ()).throw(CliError("credentials unavailable", exit_code=2)),
    )
    result = CliRunner().invoke(
        app,
        ["--output", "json", "token", "create", "--scope", "jobs:read", "--persist"],
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["token"] == "one-time"
    assert "could not be persisted" in result.stderr
    assert "one-time" not in result.stderr


def test_token_response_loss_replays_same_key_without_persisting_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    seen = []
    attempts = 0

    def respond(request):
        nonlocal attempts
        attempts += 1
        seen.append(request.headers["idempotency-key"])
        if attempts == 1:
            raise httpx.ReadError("connection dropped", request=request)
        return httpx.Response(
            409,
            json={"code": "one_time_secret_unavailable"},
            headers={"Location": "/v1/tokens/id-1"},
            request=request,
        )

    from nexa.cli.client import NexaClient

    monkeypatch.setattr(
        "nexa.cli.commands.token.NexaClient",
        lambda *a, **kw: NexaClient(
            *a, transport=httpx.MockTransport(respond), retry_backoff=0, **kw
        ),
    )
    result = CliRunner().invoke(
        app, ["token", "create", "--scope", "jobs:read", "--idempotency-key", "fixed-key-123456"]
    )
    assert result.exit_code == 6
    assert "one_time_secret_unavailable" in result.stderr
    assert seen == ["fixed-key-123456", "fixed-key-123456"]
    assert ConfigStore(tmp_path / "nexa").get_token("default") is None
