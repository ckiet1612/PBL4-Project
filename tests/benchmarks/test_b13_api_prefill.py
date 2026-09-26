"""B13 API prefill retries only transient dependency failures."""

import httpx

from benchmarks.b13.api_prefill import _submit


class RecordingAPI:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.keys = []

    def post(self, path, *, headers, json):
        self.keys.append(headers["Idempotency-Key"])
        return next(self.responses)


def _response(status, code):
    return httpx.Response(
        status,
        json={"code": code},
        request=httpx.Request("POST", "https://nexa.test/v1/jobs"),
    )


def test_transient_dependency_failure_retries_same_idempotency_key(monkeypatch):
    sleeps = []
    monkeypatch.setattr("benchmarks.b13.api_prefill.time.sleep", sleeps.append)
    api = RecordingAPI([_response(503, "dependency_unavailable"), _response(202, "accepted")])

    result = _submit(api, "tenant-1", "same-key", {}, "token")
    assert result[1].status_code == 202
    key, response, elapsed, retries = result

    assert key == "same-key"
    assert response.status_code == 202
    assert retries == 1
    assert elapsed >= 0
    assert api.keys == ["same-key", "same-key"]
    assert sleeps == [0.1]


def test_queue_full_is_not_retried(monkeypatch):
    sleeps = []
    monkeypatch.setattr("benchmarks.b13.api_prefill.time.sleep", sleeps.append)
    api = RecordingAPI([_response(503, "queue_full")])

    _, response, retries_elapsed, retries = _submit(api, "tenant-1", "same-key", {}, "token")

    assert response.status_code == 503
    assert retries_elapsed >= 0
    assert retries == 0
    assert api.keys == ["same-key"]
    assert sleeps == []
