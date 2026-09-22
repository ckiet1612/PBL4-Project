"""Bounded worker-only REST client; all mutations have explicit callback identity."""

from uuid import UUID

import httpx


class WorkerApiError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(f"worker API returned {status} {code}")


class WorkerTransportError(RuntimeError):
    pass


class WorkerApiClient:
    def __init__(self, base_url: str, credential: str | None = None, *, transport=None) -> None:
        self.credential = credential
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(8),
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self.http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        request_headers = dict(headers or {})
        if self.credential is not None:
            request_headers["Authorization"] = f"Bearer {self.credential}"
        try:
            response = self.http.request(method, path, json=body, headers=request_headers)
        except httpx.HTTPError:
            raise WorkerTransportError("worker API transport unavailable") from None
        if not 200 <= response.status_code < 300:
            try:
                code = response.json().get("code", "unknown_error")
            except (ValueError, AttributeError):
                code = "unknown_error"
            raise WorkerApiError(response.status_code, code)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("worker API returned a non-object response")
        return result

    def bootstrap(
        self, *, installation_id: str, fingerprint: str, secret: str, idempotency_key: str
    ) -> dict:
        return self.request(
            "POST",
            "/v1/internal/worker-bootstrap",
            body={
                "installation_id": installation_id,
                "credential_public_fingerprint": fingerprint,
            },
            headers={"X-Nexa-Bootstrap-Secret": secret, "Idempotency-Key": idempotency_key},
        )

    def create_incarnation(self, worker_id: str, *, nonce: str, key: str) -> dict:
        return self.request(
            "POST",
            f"/v1/workers/{worker_id}/incarnations",
            body={"process_start_nonce": nonce},
            headers={"Idempotency-Key": key},
        )

    def reconciliation(
        self, worker_id: str, incarnation_id: str, *, cursor: str | None = None
    ) -> dict:
        query = "?page_size=100" + (f"&cursor={cursor}" if cursor else "")
        return self.request(
            "GET",
            f"/v1/workers/{worker_id}/reconciliation{query}",
            headers={"X-Worker-Incarnation-Id": incarnation_id},
        )

    def heartbeat(self, worker_id: str, callback_id: str, body: dict) -> dict:
        return self.request(
            "POST",
            f"/v1/workers/{worker_id}/heartbeat",
            body=body,
            headers={"X-Callback-Id": callback_id},
        )

    def poll(self, worker_id: str, incarnation_id: str) -> dict:
        return self.request(
            "POST",
            f"/v1/workers/{worker_id}/poll",
            body={"worker_incarnation_id": incarnation_id, "long_poll_seconds": 0},
        )

    def adopt(self, attempt_id: str, callback_id: str, body: dict) -> dict:
        UUID(attempt_id)
        return self.request(
            "POST",
            f"/v1/attempts/{attempt_id}/adopt",
            body=body,
            headers={"X-Callback-Id": callback_id},
        )

    def renew(self, attempt_id: str, callback_id: str, body: dict) -> dict:
        UUID(attempt_id)
        return self.request(
            "POST",
            f"/v1/attempts/{attempt_id}/renew",
            body=body,
            headers={"X-Callback-Id": callback_id},
        )

    def fail(self, attempt_id: str, callback_id: str, body: dict) -> dict:
        UUID(attempt_id)
        return self.request(
            "POST",
            f"/v1/attempts/{attempt_id}/fail",
            body=body,
            headers={"X-Callback-Id": callback_id},
        )

    def cleanup(self, attempt_id: str, callback_id: str, body: dict) -> dict:
        UUID(attempt_id)
        return self.request(
            "POST",
            f"/v1/attempts/{attempt_id}/cleanup",
            body=body,
            headers={"X-Callback-Id": callback_id},
        )
