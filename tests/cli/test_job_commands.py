import hashlib
import json

import httpx
from typer.testing import CliRunner

from nexa.cli.app import app


def _patch_client(monkeypatch, responder):
    from nexa.cli.client import NexaClient

    monkeypatch.setattr(
        "nexa.cli.commands.job.NexaClient",
        lambda *args, **kwargs: NexaClient(
            *args, transport=httpx.MockTransport(responder), retry_backoff=0, **kwargs
        ),
    )


def test_job_list_passes_opaque_cursor_and_caps_page_size(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"items": [], "page": {"next_cursor": None, "page_size": 100}},
            request=request,
        )

    _patch_client(monkeypatch, respond)
    result = CliRunner().invoke(
        app, ["job", "list", "--cursor", "opaque-cursor-value", "--page-size", "101"]
    )
    assert result.exit_code == 0
    assert requests[0].url.path == "/v1/jobs"
    assert requests[0].url.params["cursor"] == "opaque-cursor-value"
    assert requests[0].url.params["page_size"] == "100"


def test_job_submit_spec_file_is_sent_unchanged(monkeypatch, tmp_path):
    requests = []
    spec = {
        "template_id": "cpu-iterative",
        "template_version": 1,
        "input_artifact_id": "input-1",
        "resources": {"cpu_millis": 1000, "memory_bytes": 1, "gpu_count": 0},
        "parameters": {"iterations": 3, "unknown": {"unit": "raw"}},
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")

    def respond(request):
        requests.append(request)
        return httpx.Response(
            202,
            json={
                "job_id": "j1",
                "session_id": "s1",
                "state": "QUEUED",
                "desired_state": "RUNNING",
            },
            headers={"Location": "/v1/jobs/j1", "ETag": '"v1"', "X-Request-Id": "req-1"},
            request=request,
        )

    _patch_client(monkeypatch, respond)
    result = CliRunner().invoke(
        app,
        ["job", "submit", "--spec-file", str(path), "--idempotency-key", "job-key-123456"],
    )
    assert result.exit_code == 0
    assert requests[0].headers["idempotency-key"] == "job-key-123456"
    assert json.loads(requests[0].content)["spec"] == spec
    assert "j1" in result.stdout
    assert "req-1" in result.stdout


def test_job_submit_rejects_duplicate_spec_members(monkeypatch, tmp_path):
    path = tmp_path / "spec.json"
    path.write_text('{"template_id":"cpu-iterative","template_id":"other"}', encoding="utf-8")
    result = CliRunner().invoke(app, ["job", "submit", "--spec-file", str(path)])
    assert result.exit_code == 2
    assert "job spec JSON is invalid" in result.stderr


def test_job_submit_rejects_non_finite_spec_numbers(monkeypatch, tmp_path):
    path = tmp_path / "spec.json"
    path.write_text('{"template_id":"cpu-iterative","value":NaN}', encoding="utf-8")
    result = CliRunner().invoke(app, ["job", "submit", "--spec-file", str(path)])
    assert result.exit_code == 2
    assert "job spec JSON is invalid" in result.stderr


def test_job_submit_rejects_overflowed_spec_numbers(monkeypatch, tmp_path):
    path = tmp_path / "spec.json"
    path.write_text('{"template_id":"cpu-iterative","value":1e9999}', encoding="utf-8")
    result = CliRunner().invoke(app, ["job", "submit", "--spec-file", str(path)])
    assert result.exit_code == 2
    assert "job spec JSON is invalid" in result.stderr


def test_job_submit_rejects_spec_file_with_explicit_spec_options(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text('{"template_id":"cpu-iterative"}', encoding="utf-8")
    result = CliRunner().invoke(
        app, ["job", "submit", "--spec-file", str(path), "--template-id", "other"]
    )
    assert result.exit_code == 2
    assert "cannot be combined" in result.stderr


def test_job_result_download_reads_result_then_artifact_content(monkeypatch, tmp_path):
    requests = []
    destination = tmp_path / "manifest.json"

    def respond(request):
        requests.append(request)
        if request.url.path == "/v1/jobs/j1/result":
            return httpx.Response(200, json={"manifest_artifact_id": "a1"}, request=request)
        assert request.url.path == "/v1/artifacts/a1/content"
        digest = hashlib.sha256(b"{}").hexdigest()
        return httpx.Response(
            200, content=b"{}", headers={"ETag": f'"sha256:{digest}"'}, request=request
        )

    _patch_client(monkeypatch, respond)
    result = CliRunner().invoke(
        app, ["job", "result-download", "j1", "--output-file", str(destination)]
    )
    assert result.exit_code == 0
    assert [request.url.path for request in requests] == [
        "/v1/jobs/j1/result",
        "/v1/artifacts/a1/content",
    ]
    assert destination.read_bytes() == b"{}"


def test_job_checkpoints_reads_the_owned_job_page(monkeypatch):
    requests = []
    item = {
        "checkpoint_id": "018f0d60-7b6a-7a61-9d82-1aa39c4f30b7",
        "job_id": "018f0d60-7b6a-7a62-9d82-1aa39c4f30b7",
        "attempt_id": "018f0d60-7b6a-7a63-9d82-1aa39c4f30b7",
        "sequence": 2,
        "manifest_artifact_id": "018f0d60-7b6a-7a64-9d82-1aa39c4f30b7",
        "manifest_checksum": "sha256:" + "a" * 64,
        "state": "CORRUPT",
        "created_at": "2026-09-26T00:00:00Z",
    }

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"items": [item], "page": {"next_cursor": None, "page_size": 100}},
            request=request,
        )

    _patch_client(monkeypatch, respond)
    result = CliRunner().invoke(
        app,
        [
            "--output",
            "json",
            "job",
            "checkpoints",
            item["job_id"],
            "--cursor",
            "opaque-cursor-value",
            "--page-size",
            "101",
            "--tenant",
            "018f0d60-7b6a-7a65-9d82-1aa39c4f30b7",
        ],
    )
    assert result.exit_code == 0, result.output
    (request,) = requests
    assert request.method == "GET"
    assert request.url.path == f"/v1/jobs/{item['job_id']}/checkpoints"
    assert dict(request.url.params) == {"cursor": "opaque-cursor-value", "page_size": "100"}
    assert request.headers["X-Nexa-Tenant-Id"] == "018f0d60-7b6a-7a65-9d82-1aa39c4f30b7"
    assert json.loads(result.output)["items"] == [item]
