from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class OutputMode(StrEnum):
    HUMAN = "human"
    JSON = "json"


class CliError(Exception):
    def __init__(
        self,
        message: str,
        *,
        exit_code: int = 9,
        code: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
        retry_after: str | None = None,
        location: str | None = None,
        etag: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.retry_after = retry_after
        self.location = location
        self.etag = etag

    @property
    def contract_code(self) -> str | None:
        return self.code

    @property
    def http_status(self) -> int | None:
        return self.status_code

    @property
    def retry_guidance(self) -> str | None:
        return self.retry_after


class ApiError(CliError):
    @classmethod
    def from_response(cls, status_code: int, body: object, headers: Mapping[str, str]) -> ApiError:
        payload = body if isinstance(body, dict) else {}
        code = payload.get("code") if isinstance(payload.get("code"), str) else None
        message = "API request failed"
        exit_code = {
            401: 3,
            403: 4,
            404: 5,
            409: 6,
            412: 6,
            428: 6,
            429: 7,
        }.get(status_code, 8 if status_code >= 500 else 9)
        return cls(
            message,
            exit_code=exit_code,
            code=code,
            status_code=status_code,
            request_id=headers.get("x-request-id") or headers.get("X-Request-Id"),
            retry_after=headers.get("retry-after") or headers.get("Retry-After"),
            location=headers.get("location") or headers.get("Location"),
            etag=headers.get("etag") or headers.get("ETag"),
        )


def render_error(exc: CliError, output_mode: OutputMode) -> str:
    data: dict[str, Any] = {"message": exc.message, "exit_code": exc.exit_code}
    if exc.code:
        data["code"] = exc.code
    if exc.status_code is not None:
        data["status"] = exc.status_code
    if exc.request_id:
        data["request_id"] = exc.request_id
    if exc.retry_after:
        data["retry_after"] = exc.retry_after
    if exc.location:
        data["location"] = exc.location
    if exc.etag:
        data["etag"] = exc.etag
    if output_mode is OutputMode.JSON:
        rendered = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    else:
        rendered = f"error: {exc.message}"
        if exc.code:
            rendered += f" ({exc.code})"
        if exc.status_code is not None:
            rendered += f" [status {exc.status_code}]"
        if exc.request_id:
            rendered += f" [request {exc.request_id}]"
        if exc.retry_after:
            rendered += f" [retry-after {exc.retry_after}]"
        if exc.location:
            rendered += f" [location {exc.location}]"
        if exc.etag:
            rendered += f" [etag {exc.etag}]"
    import sys

    print(rendered, file=sys.stderr)
    return rendered
