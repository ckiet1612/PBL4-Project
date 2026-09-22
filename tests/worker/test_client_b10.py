import httpx
import pytest

from nexa.worker.client import WorkerApiClient, WorkerApiError, WorkerTransportError


def test_client_uses_exact_worker_bearer_callback_and_no_implicit_retry() -> None:
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/v1/attempts/018f05c4-a922-7d0d-9f55-f9084a72d999/renew"
        assert request.headers["authorization"] == "Bearer secret"
        assert request.headers["x-callback-id"] == "018f05c4-a922-7d0d-9f55-f9084a72d998"
        return httpx.Response(409, json={"code": "state_conflict"})

    client = WorkerApiClient("http://api.local", "secret", transport=httpx.MockTransport(respond))
    with pytest.raises(WorkerApiError, match="409 state_conflict"):
        client.renew(
            "018f05c4-a922-7d0d-9f55-f9084a72d999",
            "018f05c4-a922-7d0d-9f55-f9084a72d998",
            {"authority": {}},
        )
    client.close()
    assert len(seen) == 1


def test_network_error_does_not_retry_uncertain_bootstrap() -> None:
    sent = []

    def timeout(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        raise httpx.ReadTimeout("response lost")

    client = WorkerApiClient("http://api.local", transport=httpx.MockTransport(timeout))
    with pytest.raises(WorkerTransportError, match="transport unavailable"):
        client.bootstrap(
            installation_id="i",
            fingerprint="sha256:" + "a" * 64,
            secret="bootstrap",
            idempotency_key="same-once",
        )
    client.close()
    assert len(sent) == 1
