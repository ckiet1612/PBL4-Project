"""B15 CLI control contract: job cancel/pause/resume/retry/attempts and admin recovery."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from nexa.cli.app import app

JOB_ID = "01890000-0000-7000-8000-000000000001"
CHECKPOINT_ID = "01890000-0000-7000-8000-0000000000c1"
KEY = "control-key-1234567"
SIGNED = ("--reason", "r", "--if-match", '"v1"', "--idempotency-key")


def _patch(monkeypatch, module, responder):
    from nexa.cli.client import NexaClient

    monkeypatch.setattr(
        f"nexa.cli.commands.{module}.NexaClient",
        lambda *args, **kwargs: NexaClient(
            *args, transport=httpx.MockTransport(responder), retry_backoff=0, **kwargs
        ),
    )


def _recorder(status=200, body=None, headers=None):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            json=body if body is not None else {"job_id": JOB_ID, "state": "RUNNING"},
            headers=headers or {"ETag": '"v3"', "X-Request-Id": "req-b15"},
            request=request,
        )

    return requests, respond


def _invoke(*args):
    return CliRunner().invoke(app, ["--output", "json", *args])


@pytest.mark.parametrize("command", ["cancel", "pause", "resume"])
def test_job_control_sends_reason_etag_key_and_tenant(monkeypatch, command):
    requests, respond = _recorder()
    _patch(monkeypatch, "job", respond)
    result = _invoke(
        "job",
        command,
        JOB_ID,
        "--reason",
        "operator request",
        "--if-match",
        '"v2"',
        "--idempotency-key",
        KEY,
        "--tenant",
        "01890000-0000-7000-8000-0000000000aa",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    (request,) = requests
    assert request.method == "POST"
    assert request.url.path == f"/v1/jobs/{JOB_ID}/{command}"
    assert request.headers["if-match"] == '"v2"'
    assert request.headers["idempotency-key"] == KEY
    assert request.headers["x-nexa-tenant-id"] == "01890000-0000-7000-8000-0000000000aa"
    assert json.loads(request.content) == {"reason": "operator request"}
    output = json.loads(result.stdout)
    assert output["etag"] == '"v3"'
    assert output["request_id"] == "req-b15"


def test_job_control_generates_a_contract_idempotency_key(monkeypatch):
    requests, respond = _recorder()
    _patch(monkeypatch, "job", respond)
    result = _invoke("job", "cancel", JOB_ID, "--reason", "r", "--if-match", '"v1"')
    assert result.exit_code == 0, result.stdout + result.stderr
    key = requests[0].headers["idempotency-key"]
    assert 16 <= len(key) <= 128


def test_job_retry_sends_null_checkpoint_unless_selected(monkeypatch):
    requests, respond = _recorder(status=202)
    _patch(monkeypatch, "job", respond)
    base = ("job", "retry", JOB_ID, "--reason", "again", "--if-match", '"v7"')
    assert _invoke(*base).exit_code == 0
    assert _invoke(*base, "--checkpoint-id", CHECKPOINT_ID).exit_code == 0
    assert [r.url.path for r in requests] == [f"/v1/jobs/{JOB_ID}/retry"] * 2
    assert json.loads(requests[0].content) == {"reason": "again", "checkpoint_id": None}
    assert json.loads(requests[1].content) == {"reason": "again", "checkpoint_id": CHECKPOINT_ID}
    # Each invocation is its own logical mutation.
    assert requests[0].headers["idempotency-key"] != requests[1].headers["idempotency-key"]


def test_job_attempts_passes_cursor_and_page_size(monkeypatch):
    requests, respond = _recorder(body={"items": [], "page": {"next_cursor": "next-opaque"}})
    _patch(monkeypatch, "job", respond)
    result = _invoke(
        "job", "attempts", JOB_ID, "--cursor", "opaque-cursor-value", "--page-size", "100"
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert requests[0].method == "GET"
    assert requests[0].url.path == f"/v1/jobs/{JOB_ID}/attempts"
    assert requests[0].url.params["cursor"] == "opaque-cursor-value"
    assert requests[0].url.params["page_size"] == "100"
    assert json.loads(result.stdout)["page"]["next_cursor"] == "next-opaque"


@pytest.mark.parametrize(
    "args",
    [
        ("job", "cancel", JOB_ID, "--reason", "r"),
        ("job", "pause", JOB_ID, "--reason", "r"),
        ("job", "resume", JOB_ID, "--reason", "r"),
        ("job", "retry", JOB_ID, "--reason", "r"),
        ("job", "cancel", JOB_ID, "--if-match", '"v1"'),
        ("job", "cancel", JOB_ID, "--reason", "", "--if-match", '"v1"'),
        ("job", "pause", JOB_ID, "--reason", "x" * 257, "--if-match", '"v1"'),
        ("job", "retry", JOB_ID, *SIGNED, "short"),
        ("job", "resume", JOB_ID, *SIGNED, "k" * 129),
        ("job", "cancel", JOB_ID, *SIGNED, "sp ace-key-123456"),
        ("job", "attempts", JOB_ID, "--page-size", "0"),
        ("job", "attempts", JOB_ID, "--page-size", "101"),
        ("admin", "allocations", "--page-size", "101"),
        ("admin", "recovery-events", "--from", "2026-09-26T00:00:00Z"),
        ("admin", "worker", "drain", JOB_ID, "--reason", "r"),
        ("admin", "worker", "disable", JOB_ID, "--reason", "", "--if-match", '"v1"'),
        ("admin", "worker", "enable", JOB_ID, "--reason", "x" * 257, "--if-match", '"v1"'),
    ],
)
def test_local_input_errors_exit_2_before_any_request(monkeypatch, args):
    requests, respond = _recorder()
    _patch(monkeypatch, "job", respond)
    _patch(monkeypatch, "admin", respond)
    result = _invoke(*args)
    assert result.exit_code == 2, result.stdout + result.stderr
    assert requests == []


def test_reason_of_256_characters_is_accepted(monkeypatch):
    requests, respond = _recorder()
    _patch(monkeypatch, "job", respond)
    result = _invoke("job", "pause", JOB_ID, "--reason", "x" * 256, "--if-match", '"v1"')
    assert result.exit_code == 0, result.stdout + result.stderr
    assert len(json.loads(requests[0].content)["reason"]) == 256


@pytest.mark.parametrize(
    ("status", "code", "exit_code"),
    [
        (401, "unauthenticated", 3),
        (403, "forbidden", 4),
        (404, "not_found", 5),
        (409, "invalid_state_transition", 6),
        (409, "admission_off", 6),
        (409, "write_frozen", 6),
        (412, "precondition_failed", 6),
        (428, "precondition_required", 6),
        (429, "rate_limited", 7),
        (503, "dependency_unavailable", 8),
        (422, "validation_failed", 9),
    ],
)
def test_control_api_errors_map_to_contract_exit_codes(monkeypatch, status, code, exit_code):
    requests, respond = _recorder(
        status=status,
        body={"code": code, "message": "m", "request_id": "req-err"},
        headers={"X-Request-Id": "req-err"},
    )
    _patch(monkeypatch, "job", respond)
    result = _invoke(
        "job", "cancel", JOB_ID, "--reason", "r", "--if-match", '"v1"', "--idempotency-key", KEY
    )
    assert result.exit_code == exit_code, result.stdout + result.stderr
    # A retried mutation reuses the same key; it never mints a second logical request.
    assert {r.headers["idempotency-key"] for r in requests} == {KEY}


def test_control_error_output_does_not_echo_the_free_text_reason(monkeypatch):
    requests, respond = _recorder(
        status=409, body={"code": "invalid_state_transition", "message": "m"}
    )
    _patch(monkeypatch, "job", respond)
    result = _invoke(
        "job", "cancel", JOB_ID, "--reason", "secret-ish reason text", "--if-match", '"v1"'
    )
    assert result.exit_code == 6
    assert "secret-ish reason text" not in result.stdout + result.stderr


@pytest.mark.parametrize("action", ["drain", "disable", "enable"])
def test_admin_worker_actions_forward_reason_etag_and_key(monkeypatch, action):
    requests, respond = _recorder(body={"worker_id": JOB_ID, "state": "DRAINING"})
    _patch(monkeypatch, "admin", respond)
    result = _invoke(
        "admin",
        "worker",
        action,
        JOB_ID,
        "--reason",
        "maintenance",
        "--if-match",
        '"v4"',
        "--idempotency-key",
        KEY,
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    (request,) = requests
    assert request.method == "POST"
    assert request.url.path == f"/v1/admin/workers/{JOB_ID}/{action}"
    assert request.headers["if-match"] == '"v4"'
    assert request.headers["idempotency-key"] == KEY
    assert json.loads(request.content) == {"reason": "maintenance"}
    assert json.loads(result.stdout)["etag"] == '"v3"'


def test_admin_worker_list_and_get(monkeypatch):
    requests, respond = _recorder(body={"items": [], "page": {"next_cursor": None}})
    _patch(monkeypatch, "admin", respond)
    assert _invoke("admin", "worker", "list", "--page-size", "10").exit_code == 0
    assert _invoke("admin", "worker", "get", JOB_ID).exit_code == 0
    assert [(r.method, r.url.path) for r in requests] == [
        ("GET", "/v1/admin/workers"),
        ("GET", f"/v1/admin/workers/{JOB_ID}"),
    ]
    assert requests[0].url.params["page_size"] == "10"


def test_admin_allocations_filters_by_state(monkeypatch):
    requests, respond = _recorder(body={"items": [], "page": {"next_cursor": None}})
    _patch(monkeypatch, "admin", respond)
    result = _invoke("admin", "allocations", "--state", "QUARANTINED", "--cursor", "c" * 16)
    assert result.exit_code == 0, result.stdout + result.stderr
    assert requests[0].url.path == "/v1/admin/allocations"
    assert dict(requests[0].url.params) == {
        "page_size": "50",
        "state": "QUARANTINED",
        "cursor": "c" * 16,
    }


def test_admin_recovery_events_passes_the_window_unchanged(monkeypatch):
    requests, respond = _recorder(body={"items": [], "page": {"next_cursor": None}})
    _patch(monkeypatch, "admin", respond)
    result = _invoke(
        "admin",
        "recovery-events",
        "--from",
        "2026-09-26T00:00:00Z",
        "--to",
        "2026-09-27T00:00:00+07:00",
        "--page-size",
        "5",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert requests[0].url.path == "/v1/admin/recovery-events"
    assert requests[0].url.params["from"] == "2026-09-26T00:00:00Z"
    assert requests[0].url.params["to"] == "2026-09-27T00:00:00+07:00"
    assert requests[0].url.params["page_size"] == "5"


def test_unrouted_admin_groups_stay_hidden():
    for group in ("job", "fairness", "allocation", "recovery"):
        result = CliRunner().invoke(app, ["admin", group, "list"])
        assert result.exit_code == 2
        assert "No such command" in result.stderr
