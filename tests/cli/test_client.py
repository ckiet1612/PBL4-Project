import io

import httpx
import pytest

from nexa.cli.client import NexaClient
from nexa.cli.errors import ApiError, CliError


def test_client_sends_bearer_tenant_and_idempotency_headers():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True}, request=request)

    client = NexaClient(
        "https://nexa.test",
        token="secret",
        tenant_id="tenant-1",
        transport=httpx.MockTransport(respond),
    )
    response = client.request_json(
        "POST",
        "/v1/jobs",
        json_body={"spec": {}},
        mutation=True,
        idempotency_key="job-key-123456",
    )
    assert response.status_code == 200
    request = requests[0]
    assert request.headers["authorization"] == "Bearer secret"
    assert request.headers["x-nexa-tenant-id"] == "tenant-1"
    assert request.headers["idempotency-key"] == "job-key-123456"


def test_client_does_not_allow_reserved_auth_headers_to_be_overridden():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True}, request=request)

    client = NexaClient(
        "https://nexa.test",
        token="secret",
        tenant_id="tenant-1",
        transport=httpx.MockTransport(respond),
    )
    client.request_json(
        "GET", "/v1/jobs", headers={"authorization": "Bearer forged", "x-nexa-tenant-id": "other"}
    )
    assert requests[0].headers.get_list("authorization") == ["Bearer secret"]
    assert requests[0].headers.get_list("x-nexa-tenant-id") == ["tenant-1"]


def test_client_retries_get_on_service_unavailable():
    attempts = 0

    def respond(request):
        nonlocal attempts
        attempts += 1
        status = 503 if attempts == 1 else 200
        return httpx.Response(status, json={"ok": status == 200}, request=request)

    client = NexaClient(
        "https://nexa.test", transport=httpx.MockTransport(respond), retry_backoff=0
    )
    assert client.request_json("GET", "/v1/jobs").body == {"ok": True}
    assert attempts == 2


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_request_json_rejects_redirect_statuses(status):
    def respond(request):
        return httpx.Response(status, headers={"Location": "/elsewhere"}, request=request)

    client = NexaClient("https://nexa.test", transport=httpx.MockTransport(respond))
    with pytest.raises(ApiError) as raised:
        client.request_json("GET", "/v1/jobs")
    assert raised.value.status_code == status


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_stream_upload_rejects_redirect_statuses(status):
    def respond(request):
        return httpx.Response(status, headers={"Location": "/elsewhere"}, request=request)

    client = NexaClient("https://nexa.test", transport=httpx.MockTransport(respond))
    with pytest.raises(ApiError) as raised:
        client.stream_upload(
            "/v1/artifacts",
            io.BytesIO(b"payload"),
            headers={},
            idempotency_key="upload-key-123456",
        )
    assert raised.value.status_code == status


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_stream_upload_file_rejects_redirect_statuses(tmp_path, status):
    source = tmp_path / "input.bin"
    source.write_bytes(b"payload")

    def respond(request):
        return httpx.Response(status, headers={"Location": "/elsewhere"}, request=request)

    client = NexaClient("https://nexa.test", transport=httpx.MockTransport(respond))
    with pytest.raises(ApiError) as raised:
        client.stream_upload_file(
            source,
            headers={},
            idempotency_key="upload-key-123456",
        )
    assert raised.value.status_code == status


def test_client_maps_precondition_required_to_conflict_exit_code():
    def respond(request):
        return httpx.Response(428, json={"code": "precondition_required"}, request=request)

    client = NexaClient("https://nexa.test", transport=httpx.MockTransport(respond))
    with pytest.raises(CliError) as raised:
        client.request_json(
            "PATCH", "/v1/admin/tenants/t1", mutation=True, idempotency_key="key-123456"
        )
    assert raised.value.exit_code == 6


def test_upload_reads_in_bounded_chunks():
    class BoundedStream(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 64 * 1024
            return super().read(size)

    def respond(request):
        assert request.content == b"payload"
        return httpx.Response(201, json={"id": "artifact-1"}, request=request)

    client = NexaClient("https://nexa.test", transport=httpx.MockTransport(respond))
    response = client.stream_upload(
        "/v1/artifacts",
        BoundedStream(b"payload"),
        headers={"X-Artifact-Size": "7"},
        idempotency_key="upload-key-123456",
    )
    assert response.status_code == 201


def test_download_rejects_wrong_checksum_etag(tmp_path):
    def respond(request):
        return httpx.Response(
            200,
            content=b"payload",
            headers={"ETag": '"sha256:' + "0" * 64 + '"'},
            request=request,
        )

    client = NexaClient("https://nexa.test", transport=httpx.MockTransport(respond))
    destination = tmp_path / "artifact.bin"
    with pytest.raises(CliError, match="checksum"):
        client.stream_download("/v1/artifacts/a/content", destination, headers={})
    assert not destination.exists()


def test_download_fails_closed_for_malformed_etag(tmp_path):
    def respond(request):
        return httpx.Response(200, content=b"payload", headers={"ETag": '"weak"'}, request=request)

    client = NexaClient("https://nexa.test", transport=httpx.MockTransport(respond))
    with pytest.raises(CliError, match="checksum"):
        client.stream_download("/v1/artifacts/a/content", tmp_path / "artifact.bin", headers={})


def test_download_rejects_empty_checksum_etag(tmp_path):
    def respond(request):
        return httpx.Response(
            200, content=b"payload", headers={"ETag": '"sha256:"'}, request=request
        )

    client = NexaClient("https://nexa.test", transport=httpx.MockTransport(respond))
    with pytest.raises(CliError, match="checksum"):
        client.stream_download("/v1/artifacts/a/content", tmp_path / "artifact.bin", headers={})


def test_download_retries_service_unavailable(tmp_path):
    attempts = 0

    def respond(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, content=b"busy", request=request)
        return httpx.Response(200, content=b"payload", request=request)

    client = NexaClient(
        "https://nexa.test", transport=httpx.MockTransport(respond), retry_backoff=0
    )
    result = client.stream_download(
        "/v1/artifacts/a/content", tmp_path / "artifact.bin", headers={}
    )
    assert result.size == 7
    assert attempts == 2


def test_download_error_decodes_streamed_json_and_preserves_metadata(tmp_path):
    def respond(request):
        return httpx.Response(
            403,
            content=b'{"code":"permission_denied","message":"bad"}',
            headers={
                "content-type": "application/json",
                "X-Request-Id": "req-2",
                "Location": "/v1/help",
                "ETag": '"v1"',
            },
            request=request,
        )

    client = NexaClient("https://nexa.test", transport=httpx.MockTransport(respond))
    with pytest.raises(Exception) as raised:
        client.stream_download("/v1/artifacts/a/content", tmp_path / "artifact.bin", headers={})
    error = raised.value
    assert error.request_id == "req-2"
    assert error.location == "/v1/help"
    assert error.etag == '"v1"'


@pytest.mark.parametrize("status", [204, 302])
def test_download_rejects_non_content_success_status(tmp_path, status):
    def respond(request):
        return httpx.Response(status, headers={"Location": "/elsewhere"}, request=request)

    client = NexaClient("https://nexa.test", transport=httpx.MockTransport(respond))
    with pytest.raises(ApiError) as raised:
        client.stream_download("/v1/artifacts/a/content", tmp_path / "artifact.bin", headers={})
    assert raised.value.status_code == status
    assert not (tmp_path / "artifact.bin").exists()


def test_download_does_not_follow_preexisting_staging_symlink(tmp_path):
    destination = tmp_path / "artifact.bin"
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"keep")
    (tmp_path / ".artifact.bin.part").symlink_to(victim)

    def respond(request):
        return httpx.Response(200, content=b"payload", request=request)

    client = NexaClient("https://nexa.test", transport=httpx.MockTransport(respond))
    result = client.stream_download("/v1/artifacts/a/content", destination, headers={})
    assert result.size == 7
    assert destination.read_bytes() == b"payload"
    assert victim.read_bytes() == b"keep"
