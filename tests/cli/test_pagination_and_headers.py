import httpx
import pytest
from typer.testing import CliRunner

from nexa.cli.app import app


def test_admin_page_size_above_contract_maximum_is_local_error():
    result = CliRunner().invoke(app, ["admin", "tenant", "list", "--page-size", "101"])
    assert result.exit_code == 2


def test_admin_if_match_is_required_locally_for_mutation():
    result = CliRunner().invoke(
        app,
        ["admin", "tenant", "update", "t1", "--display-name", "New"],
    )
    assert result.exit_code == 2
    assert "if-match" in result.stderr


@pytest.mark.parametrize(
    ("args", "missing"),
    [
        (["admin", "audit", "list", "--to", "end"], "--from"),
        (["admin", "audit", "list", "--from", "start"], "--to"),
    ],
)
def test_admin_required_filters_fail_before_transport(monkeypatch, args, missing):
    monkeypatch.setattr(
        "nexa.cli.commands.admin._client",
        lambda _ctx: pytest.fail("required filter reached transport"),
    )
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2, result.stdout + result.stderr
    assert missing in result.stderr


def test_admin_audit_filters_are_forwarded_unchanged(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, json={"items": [], "page": {"next_cursor": None}}, request=request
        )

    from nexa.cli.client import NexaClient

    monkeypatch.setattr(
        "nexa.cli.commands.admin.NexaClient",
        lambda *args, **kwargs: NexaClient(
            *args, transport=httpx.MockTransport(respond), retry_backoff=0, **kwargs
        ),
    )
    result = CliRunner().invoke(
        app,
        [
            "admin",
            "audit",
            "list",
            "--from",
            "2026-01-01T00:00:00Z",
            "--to",
            "2026-01-02T00:00:00Z",
            "--cursor",
            "a+/=",
            "--page-size",
            "80",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert requests[0].url.path == "/v1/admin/audit"
    assert requests[0].url.params["from"] == "2026-01-01T00:00:00Z"
    assert requests[0].url.params["to"] == "2026-01-02T00:00:00Z"
    assert requests[0].url.params["cursor"] == "a+/="
    assert requests[0].url.params["page_size"] == "80"


@pytest.mark.parametrize(
    "args",
    [
        ["admin", "job", "list"],
        ["admin", "worker", "list"],
        ["admin", "allocation", "list"],
        ["admin", "fairness", "query"],
        ["admin", "recovery-event", "list"],
    ],
)
def test_unwired_admin_groups_are_not_exposed(args):
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2
    assert "No such command" in result.stderr
