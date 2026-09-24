import hashlib

import httpx
from typer.testing import CliRunner

from nexa.cli.app import app


def _patch_client(monkeypatch, responder):
    from nexa.cli.client import NexaClient

    monkeypatch.setattr(
        "nexa.cli.commands.artifact.NexaClient",
        lambda *args, **kwargs: NexaClient(
            *args, transport=httpx.MockTransport(responder), retry_backoff=0, **kwargs
        ),
    )


def test_artifact_upload_hashes_and_sends_contract_headers(monkeypatch, tmp_path):
    source = tmp_path / "input.bin"
    source.write_bytes(b"abc")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            201,
            json={
                "artifact_id": "a1",
                "size_bytes": 3,
                "checksum": "sha256:" + hashlib.sha256(b"abc").hexdigest(),
            },
            request=request,
        )

    _patch_client(monkeypatch, respond)
    result = CliRunner().invoke(
        app,
        [
            "artifact",
            "upload",
            str(source),
            "--tenant",
            "t1",
            "--kind",
            "INPUT",
            "--media-type",
            "application/octet-stream",
            "--idempotency-key",
            "upload-key-123456",
        ],
    )
    assert result.exit_code == 0
    request = requests[0]
    assert request.headers["x-artifact-size"] == "3"
    assert request.headers["x-artifact-checksum"] == "sha256:" + hashlib.sha256(b"abc").hexdigest()
    assert request.headers["x-artifact-kind"] == "INPUT"
    assert request.headers["x-artifact-media-type"] == "application/octet-stream"
    assert request.headers["content-type"] == "application/octet-stream"
    assert request.headers["x-nexa-tenant-id"] == "t1"
    assert request.headers["idempotency-key"] == "upload-key-123456"
    assert request.content == b"abc"


def test_artifact_upload_reopens_file_for_retry(monkeypatch, tmp_path):
    source = tmp_path / "input.bin"
    source.write_bytes(b"abc")
    requests = []

    def respond(request):
        requests.append(request)
        status = 503 if len(requests) == 1 else 201
        return httpx.Response(status, json={"size_bytes": 3}, request=request)

    _patch_client(monkeypatch, respond)
    result = CliRunner().invoke(
        app,
        [
            "artifact",
            "upload",
            str(source),
            "--kind",
            "INPUT",
            "--media-type",
            "application/octet-stream",
            "--idempotency-key",
            "upload-key-123456",
        ],
    )
    assert result.exit_code == 0
    assert len(requests) == 2
    assert [request.content for request in requests] == [b"abc", b"abc"]


def test_artifact_download_refuses_existing_file_without_force(monkeypatch, tmp_path):
    destination = tmp_path / "out.bin"
    destination.write_bytes(b"old")
    result = CliRunner().invoke(
        app, ["artifact", "download", "a1", "--output-file", str(destination)]
    )
    assert result.exit_code == 2
    assert destination.read_bytes() == b"old"


def test_artifact_list_caps_page_size_and_passes_cursor(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, json={"items": [], "page": {"next_cursor": None}}, request=request
        )

    _patch_client(monkeypatch, respond)
    result = CliRunner().invoke(
        app,
        ["artifact", "list", "--cursor", "opaque-cursor-value", "--page-size", "101"],
    )
    assert result.exit_code == 0
    assert requests[0].url.params["cursor"] == "opaque-cursor-value"
    assert requests[0].url.params["page_size"] == "100"


def test_artifact_download_passes_single_range_and_verifies_checksum(monkeypatch, tmp_path):
    destination = tmp_path / "out.bin"
    payload = b"payload"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()

    def respond(request):
        assert request.headers["range"] == "bytes=0-6"
        return httpx.Response(
            206,
            content=payload,
            headers={"ETag": f'"{digest}"', "Content-Length": str(len(payload))},
            request=request,
        )

    _patch_client(monkeypatch, respond)
    result = CliRunner().invoke(
        app,
        [
            "artifact",
            "download",
            "a1",
            "--output-file",
            str(destination),
            "--range",
            "bytes=0-6",
            "--checksum",
            digest,
        ],
    )
    assert result.exit_code == 0
    assert destination.read_bytes() == payload


def test_artifact_ranged_download_rejects_checksum_mismatch(monkeypatch, tmp_path):
    destination = tmp_path / "out.bin"
    payload = b"payload"
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()

    def respond(request):
        return httpx.Response(
            206,
            content=payload,
            headers={"ETag": f'"sha256:{"0" * 64}"'},
            request=request,
        )

    _patch_client(monkeypatch, respond)
    result = CliRunner().invoke(
        app,
        [
            "artifact",
            "download",
            "a1",
            "--output-file",
            str(destination),
            "--range",
            "bytes=0-6",
            "--checksum",
            expected,
        ],
    )
    assert result.exit_code == 10
    assert not destination.exists()


def test_artifact_ranged_download_requires_checksum_etag(monkeypatch, tmp_path):
    destination = tmp_path / "out.bin"
    payload = b"payload"
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()

    def respond(request):
        return httpx.Response(
            206,
            content=payload,
            headers={"X-Artifact-Checksum": expected},
            request=request,
        )

    _patch_client(monkeypatch, respond)
    result = CliRunner().invoke(
        app,
        [
            "artifact",
            "download",
            "a1",
            "--output-file",
            str(destination),
            "--range",
            "bytes=0-6",
            "--checksum",
            expected,
        ],
    )
    assert result.exit_code == 10
    assert not destination.exists()
