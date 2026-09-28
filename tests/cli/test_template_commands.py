"""B16: `nexa template list|show|get` read the tenant-context template catalog."""

import json

import pytest

from tests.cli.test_control_commands_b15 import _invoke, _patch, _recorder

TENANT = "01890000-0000-7000-8000-0000000000aa"


def test_template_list_sends_enabled_filter_and_tenant(monkeypatch):
    requests, respond = _recorder(body=[{"template_id": "cpu-iterative"}])
    _patch(monkeypatch, "template", respond)
    assert _invoke("template", "list", "--tenant", TENANT).exit_code == 0
    result = _invoke("template", "list", "--enabled", "false", "--tenant", TENANT)
    assert result.exit_code == 0, result.stdout + result.stderr
    assert [r.url.path for r in requests] == ["/v1/templates"] * 2
    # The server default (enabled=true) applies when the flag is absent.
    assert "enabled" not in requests[0].url.params
    assert requests[1].url.params["enabled"] == "false"
    assert requests[1].headers["x-nexa-tenant-id"] == TENANT
    assert json.loads(result.stdout) == [{"template_id": "cpu-iterative"}]


def test_template_list_rejects_a_non_boolean_filter(monkeypatch):
    requests, respond = _recorder()
    _patch(monkeypatch, "template", respond)
    result = _invoke("template", "list", "--enabled", "maybe")
    assert result.exit_code == 2
    assert requests == []


@pytest.mark.parametrize("command", ["show", "get"])
def test_template_show_reads_one_template(monkeypatch, command):
    requests, respond = _recorder(body={"template_id": "pytorch-cifar10-cnn", "version": 1})
    _patch(monkeypatch, "template", respond)
    result = _invoke("template", command, "pytorch-cifar10-cnn", "--tenant", TENANT)
    assert result.exit_code == 0, result.stdout + result.stderr
    (request,) = requests
    assert (request.method, request.url.path) == ("GET", "/v1/templates/pytorch-cifar10-cnn")
    assert request.headers["x-nexa-tenant-id"] == TENANT
