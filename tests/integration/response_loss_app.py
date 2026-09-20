from __future__ import annotations

import os

from nexa.api.main import app as production_app


class CrashAfterCommitBeforeResponse:
    def __init__(self, wrapped_app) -> None:
        self.wrapped_app = wrapped_app

    async def __call__(self, scope, receive, send) -> None:
        async def crash_before_send(message) -> None:
            if (
                scope["type"] == "http"
                and scope["method"] == "POST"
                and scope["path"] == "/v1/jobs"
                and message["type"] == "http.response.start"
                and message["status"] == 202
            ):
                os._exit(91)
            await send(message)

        await self.wrapped_app(scope, receive, crash_before_send)


app = CrashAfterCommitBeforeResponse(production_app)
