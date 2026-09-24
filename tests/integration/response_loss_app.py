from __future__ import annotations

import os

from nexa.api.main import app as production_app


class CrashAfterCommitBeforeResponse:
    def __init__(self, wrapped_app, *, path: str = "/v1/jobs", success_status: int = 202) -> None:
        self.wrapped_app = wrapped_app
        self.path = path
        self.success_status = success_status

    async def __call__(self, scope, receive, send) -> None:
        async def crash_before_send(message) -> None:
            if (
                scope["type"] == "http"
                and scope["method"] == "POST"
                and scope["path"] == self.path
                and message["type"] == "http.response.start"
                and message["status"] == self.success_status
            ):
                os._exit(91)
            await send(message)

        await self.wrapped_app(scope, receive, crash_before_send)


app = CrashAfterCommitBeforeResponse(production_app)
