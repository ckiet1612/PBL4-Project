"""B16: `nexa sweep submit|show` drive submitSweep and getSweep."""

import json

from tests.cli.test_control_commands_b15 import _invoke, _patch, _recorder

TENANT = "01890000-0000-7000-8000-0000000000aa"
SWEEP_ID = "01890000-0000-7000-8000-0000000000ee"
KEY = "sweep-key-0000000001"
REQUEST = {
    "base_spec": {"template_id": "pytorch-cifar10-cnn", "parameters": {"learning_rate": 0.01}},
    "dimensions": [{"name": "learning_rate", "values": [0.01, 0.001]}],
}


def _write(tmp_path, content: str):
    path = tmp_path / "sweep.json"
    path.write_text(content, encoding="utf-8")
    return path


def test_sweep_submit_posts_the_file_with_key_and_tenant(monkeypatch, tmp_path):
    requests, respond = _recorder(
        status=207,
        body={"sweep_id": SWEEP_ID, "accepted_count": 2},
        headers={"Location": f"/v1/sweeps/{SWEEP_ID}", "X-Request-Id": "req-b16"},
    )
    _patch(monkeypatch, "job", respond)
    path = _write(tmp_path, json.dumps(REQUEST))
    result = _invoke(
        "sweep", "submit", "--file", str(path), "--idempotency-key", KEY, "--tenant", TENANT
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    (request,) = requests
    assert (request.method, request.url.path) == ("POST", "/v1/sweeps")
    assert request.headers["idempotency-key"] == KEY
    assert request.headers["x-nexa-tenant-id"] == TENANT
    assert json.loads(request.content) == REQUEST
    output = json.loads(result.stdout)
    assert output["location"] == f"/v1/sweeps/{SWEEP_ID}"
    assert output["sweep_id"] == SWEEP_ID


def test_sweep_submit_generates_a_key_when_absent(monkeypatch, tmp_path):
    requests, respond = _recorder(status=207, body={"sweep_id": SWEEP_ID})
    _patch(monkeypatch, "job", respond)
    result = _invoke("sweep", "submit", "--file", str(_write(tmp_path, json.dumps(REQUEST))))
    assert result.exit_code == 0, result.stdout + result.stderr
    (request,) = requests
    assert 16 <= len(request.headers["idempotency-key"]) <= 128


def test_sweep_submit_rejects_invalid_input_before_any_request(monkeypatch, tmp_path):
    requests, respond = _recorder()
    _patch(monkeypatch, "job", respond)
    for content in ('{"base_spec": {}, "base_spec": {}}', "[1]", '{"x": NaN}'):
        path = _write(tmp_path, content)
        assert _invoke("sweep", "submit", "--file", str(path)).exit_code == 2
    path = _write(tmp_path, json.dumps(REQUEST))
    result = _invoke("sweep", "submit", "--file", str(path), "--idempotency-key", "short")
    assert result.exit_code == 2
    assert requests == []


def test_sweep_submit_renders_a_server_error(monkeypatch, tmp_path):
    requests, respond = _recorder(
        status=422,
        body={"code": "validation_failed", "message": "bad", "request_id": "req-1"},
    )
    _patch(monkeypatch, "job", respond)
    result = _invoke("sweep", "submit", "--file", str(_write(tmp_path, json.dumps(REQUEST))))
    assert result.exit_code != 0
    assert len(requests) == 1


def test_sweep_show_pages_by_cursor(monkeypatch):
    requests, respond = _recorder(body={"sweep_id": SWEEP_ID, "children": []})
    _patch(monkeypatch, "job", respond)
    result = _invoke(
        "sweep",
        "show",
        SWEEP_ID,
        "--cursor",
        "c" * 16,
        "--page-size",
        "10",
        "--tenant",
        TENANT,
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    (request,) = requests
    assert (request.method, request.url.path) == ("GET", f"/v1/sweeps/{SWEEP_ID}")
    assert request.url.params["cursor"] == "c" * 16
    assert request.url.params["page_size"] == "10"
    assert request.headers["x-nexa-tenant-id"] == TENANT
    assert "idempotency-key" not in request.headers
    assert _invoke("sweep", "show", SWEEP_ID, "--page-size", "101").exit_code == 2
    assert len(requests) == 1
