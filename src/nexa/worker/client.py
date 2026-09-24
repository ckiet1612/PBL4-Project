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

    def claim(self, attempt_id: str, callback_id: str, body: dict) -> dict:
        return self._attempt_callback(attempt_id, callback_id, "claim", body)

    def start(self, attempt_id: str, callback_id: str, body: dict) -> dict:
        return self._attempt_callback(attempt_id, callback_id, "start", body)

    def reserve_result(self, attempt_id: str, callback_id: str, body: dict) -> dict:
        return self._attempt_callback(attempt_id, callback_id, "result-reservations", body)

    def complete(self, attempt_id: str, callback_id: str, body: dict) -> dict:
        return self._attempt_callback(attempt_id, callback_id, "complete", body)

    def _attempt_callback(self, attempt_id, callback_id, suffix, body):
        UUID(attempt_id)
        return self.request(
            "POST",
            f"/v1/attempts/{attempt_id}/{suffix}",
            body=body,
            headers={"X-Callback-Id": callback_id},
        )

    def _authority_headers(self, authority):
        headers = {
            "X-Worker-Id": authority.worker_id,
            "X-Worker-Incarnation-Id": authority.worker_incarnation_id,
            "X-Allocation-Id": authority.allocation_id,
            "X-Lease-Id": authority.lease_id,
            "X-Job-Fence": str(authority.job_fence),
        }
        if self.credential is not None:
            headers["Authorization"] = f"Bearer {self.credential}"
        return headers

    @staticmethod
    def _check_response(response):
        if not 200 <= response.status_code < 300:
            response.read()
            try:
                code = response.json().get("code", "unknown_error")
            except (ValueError, AttributeError):
                code = "unknown_error"
            raise WorkerApiError(response.status_code, code)

    def download_execution(self, authority, artifact, target):
        """Stream into a private exclusive file, checking the claimed descriptor."""
        import hashlib
        import os
        from pathlib import Path

        target = Path(target)
        UUID(artifact["artifact_id"])
        path = (
            f"/v1/attempts/{authority.attempt_id}/execution-artifacts/"
            f"{artifact['artifact_id']}/content"
        )
        created = False
        try:
            headers = self._authority_headers(authority)
            with self.http.stream("GET", path, headers=headers) as response:
                self._check_response(response)
                if (
                    response.status_code != 200
                    or response.headers.get("etag", "").strip('"') != artifact["checksum"]
                    or response.headers.get("x-artifact-media-type") != artifact["media_type"]
                ):
                    raise ValueError("execution artifact response metadata mismatch")
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                created = True
                with os.fdopen(fd, "wb") as output:
                    digest = hashlib.sha256()
                    count = 0
                    for chunk in response.iter_bytes(64 * 1024):
                        count += len(chunk)
                        if count > artifact["size_bytes"]:
                            raise ValueError("execution artifact size exceeded descriptor")
                        output.write(chunk)
                        digest.update(chunk)
                    if (
                        count != artifact["size_bytes"]
                        or "sha256:" + digest.hexdigest() != artifact["checksum"]
                    ):
                        raise ValueError("execution artifact bytes mismatch")
                    output.flush()
                    os.fsync(output.fileno())
        except BaseException:
            if created:
                target.unlink(missing_ok=True)
            raise

    def upload_artifact(self, authority, descriptor, key, content: bytes):
        import hashlib

        if (
            len(content) != descriptor["size_bytes"]
            or "sha256:" + hashlib.sha256(content).hexdigest() != descriptor["checksum"]
        ):
            raise ValueError("attempt upload does not match descriptor")
        headers = self._authority_headers(authority)
        headers.update(
            {
                "Content-Type": "application/octet-stream",
                "Idempotency-Key": key,
                "X-Artifact-Kind": descriptor["kind"],
                "X-Artifact-Media-Type": descriptor["media_type"],
                "X-Artifact-Size": str(descriptor["size_bytes"]),
                "X-Artifact-Checksum": descriptor["checksum"],
            }
        )
        try:
            response = self.http.post(
                f"/v1/attempts/{authority.attempt_id}/artifacts", content=content, headers=headers
            )
        except httpx.HTTPError:
            raise WorkerTransportError("worker API upload unavailable") from None
        self._check_response(response)
        artifact = response.json()
        for field in ("kind", "media_type", "size_bytes", "checksum"):
            if artifact[field] != descriptor[field]:
                raise ValueError("attempt upload response binding mismatch")
        UUID(artifact["artifact_id"])
        return artifact
