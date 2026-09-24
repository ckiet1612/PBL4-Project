from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import httpx

from .errors import ApiError, CliError


@dataclass(frozen=True)
class ApiResponse:
    status_code: int
    body: object
    headers: httpx.Headers

    @property
    def body_json(self) -> object:
        return self.body

    @property
    def status(self) -> int:
        return self.status_code

    @property
    def json(self) -> object:
        return self.body


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size: int
    sha256: str
    headers: httpx.Headers


class NexaClient:
    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        tenant_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        max_retries: int = 2,
        retry_backoff: float = 0.2,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.tenant_id = tenant_id
        self.max_retries = max(0, max_retries)
        self.retry_backoff = max(0.0, retry_backoff)
        self.http = httpx.Client(
            base_url=self.endpoint,
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            follow_redirects=False,
        )

    def close(self) -> None:
        self.http.close()

    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, object] | None = None,
        json_body: object | None = None,
        headers: Mapping[str, str] | None = None,
        mutation: bool = False,
        idempotency_key: str | None = None,
        replayable: bool = True,
    ) -> ApiResponse:
        request_headers = self._headers(headers)
        if mutation and idempotency_key:
            request_headers["Idempotency-Key"] = idempotency_key
        retryable = not mutation or (bool(idempotency_key) and replayable)
        for attempt in range(self.max_retries + 1):
            try:
                response = self.http.request(
                    method, path, params=query, json=json_body, headers=request_headers
                )
            except httpx.TransportError as exc:
                if retryable and attempt < self.max_retries:
                    self._sleep(attempt)
                    continue
                raise CliError("network request failed", exit_code=8) from exc
            body = self._decode(response)
            if response.status_code in {429, 503} and retryable and attempt < self.max_retries:
                self._sleep(attempt, response.headers.get("retry-after"))
                continue
            self._require_success(response, body)
            return ApiResponse(response.status_code, body, response.headers)
        raise CliError("network request failed", exit_code=8)

    def stream_upload(
        self, path: str, file: BinaryIO, *, headers: Mapping[str, str], idempotency_key: str
    ) -> ApiResponse:
        merged = self._headers(headers)
        merged["Idempotency-Key"] = idempotency_key
        merged.setdefault("Content-Type", "application/octet-stream")
        replayable = file.seekable()
        start = file.tell() if replayable else None
        for attempt in range(self.max_retries + 1 if replayable else 1):
            if attempt and start is not None:
                file.seek(start)

            def chunks():
                while chunk := file.read(64 * 1024):
                    if not isinstance(chunk, bytes):
                        raise CliError("upload stream must return bytes", exit_code=2)
                    yield chunk

            try:
                response = self.http.post(path, content=chunks(), headers=merged)
            except httpx.TransportError as exc:
                if replayable and attempt < self.max_retries:
                    self._sleep(attempt)
                    continue
                raise CliError("network request failed", exit_code=8) from exc
            body = self._decode(response)
            if response.status_code in {429, 503} and replayable and attempt < self.max_retries:
                self._sleep(attempt, response.headers.get("retry-after"))
                continue
            self._require_success(response, body)
            return ApiResponse(response.status_code, body, response.headers)
        raise CliError("network request failed", exit_code=8)

    def stream_upload_file(
        self,
        path: Path,
        *,
        headers: Mapping[str, str],
        idempotency_key: str,
    ) -> ApiResponse:
        """Upload a replayable local file, reopening it for every attempt."""
        merged = self._headers(headers)
        merged["Idempotency-Key"] = idempotency_key
        merged.setdefault("Content-Type", "application/octet-stream")
        for attempt in range(self.max_retries + 1):
            try:
                with path.open("rb") as file:
                    response = self.http.post(
                        "/v1/artifacts", content=self._file_chunks(file), headers=merged
                    )
            except OSError as exc:
                raise CliError("unable to read artifact file", exit_code=2) from exc
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    self._sleep(attempt)
                    continue
                raise CliError("network request failed", exit_code=8) from exc
            body = self._decode(response)
            if response.status_code in {429, 503} and attempt < self.max_retries:
                self._sleep(attempt, response.headers.get("retry-after"))
                continue
            self._require_success(response, body)
            return ApiResponse(response.status_code, body, response.headers)
        raise CliError("network request failed", exit_code=8)

    @staticmethod
    def _require_success(response: httpx.Response, body: object) -> None:
        if not 200 <= response.status_code < 300:
            raise ApiError.from_response(response.status_code, body, response.headers)

    @staticmethod
    def _file_chunks(file: BinaryIO):
        while chunk := file.read(64 * 1024):
            if not isinstance(chunk, bytes):
                raise CliError("upload stream must return bytes", exit_code=2)
            yield chunk

    def stream_download(
        self,
        path: str,
        destination: Path,
        *,
        headers: Mapping[str, str],
        force: bool = False,
        expected_checksum: str | None = None,
    ) -> DownloadResult:
        if destination.exists() and not force:
            raise CliError("destination already exists; use force to replace it", exit_code=2)
        destination.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(self.max_retries + 1):
            temporary: Path | None = None
            try:
                with self.http.stream("GET", path, headers=self._headers(headers)) as response:
                    if response.status_code not in {200, 206}:
                        response.read()
                        if response.status_code in {429, 503} and attempt < self.max_retries:
                            self._sleep(attempt, response.headers.get("retry-after"))
                            continue
                        raise ApiError.from_response(
                            response.status_code, self._decode(response), response.headers
                        )
                    digest = hashlib.sha256()
                    size = 0
                    fd, temporary_name = tempfile.mkstemp(
                        prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
                    )
                    temporary = Path(temporary_name)
                    with os.fdopen(fd, "wb") as output:
                        for chunk in response.iter_bytes(64 * 1024):
                            digest.update(chunk)
                            size += len(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    expected_size = response.headers.get("content-length")
                    etag = response.headers.get("etag")
                    if etag is not None:
                        if not re.fullmatch(r'"sha256:[0-9a-f]{64}"', etag):
                            raise CliError("download checksum integrity check failed", exit_code=10)
                        expected_digest = etag[len('"sha256:') : -1]
                    else:
                        expected_digest = None
                    declared_checksum = response.headers.get("x-artifact-checksum")
                    if declared_checksum is not None and not re.fullmatch(
                        r"sha256:[0-9a-f]{64}", declared_checksum
                    ):
                        raise CliError("download checksum integrity check failed", exit_code=10)
                    if expected_checksum is not None:
                        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_checksum):
                            raise CliError("download checksum integrity check failed", exit_code=10)
                        if response.status_code == 200:
                            if digest.hexdigest() != expected_checksum[7:]:
                                raise CliError(
                                    "download checksum integrity check failed", exit_code=10
                                )
                        elif expected_digest is None or expected_digest != expected_checksum[7:]:
                            raise CliError("download checksum integrity check failed", exit_code=10)
                    if expected_size and size != int(expected_size):
                        raise CliError("download size integrity check failed", exit_code=10)
                    if (
                        response.status_code == 200
                        and expected_digest
                        and digest.hexdigest() != expected_digest
                    ):
                        raise CliError("download checksum integrity check failed", exit_code=10)
                    if (
                        response.status_code == 200
                        and declared_checksum
                        and digest.hexdigest() != declared_checksum[7:]
                    ):
                        raise CliError("download checksum integrity check failed", exit_code=10)
                    os.replace(temporary, destination)
                    temporary = None
                    directory_fd = os.open(destination.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                    return DownloadResult(destination, size, digest.hexdigest(), response.headers)
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    self._sleep(attempt)
                    continue
                raise CliError("network request failed", exit_code=8) from exc
            except (OSError, ValueError) as exc:
                raise CliError("download integrity check failed", exit_code=10) from exc
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        raise CliError("network request failed", exit_code=8)

    def _headers(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        merged = {"Accept": "application/json"}
        if headers:
            merged.update(
                (name, value)
                for name, value in headers.items()
                if name.lower() not in {"authorization", "x-nexa-tenant-id"}
            )
        if self.token:
            merged["Authorization"] = f"Bearer {self.token}"
        if self.tenant_id:
            merged["X-Nexa-Tenant-Id"] = self.tenant_id
        return merged

    @staticmethod
    def _decode(response: httpx.Response) -> object:
        if not response.content:
            return None
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            try:
                return response.json()
            except ValueError:
                return None
        return response.content

    def _sleep(self, attempt: int, retry_after: str | None = None) -> None:
        delay = self.retry_backoff * (2**attempt)
        if retry_after:
            with suppress(ValueError):
                delay = max(delay, min(float(retry_after), 30.0))
        if delay:
            time.sleep(delay)
