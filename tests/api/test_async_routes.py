import time
from types import SimpleNamespace

import anyio
import httpx
from fastapi import FastAPI

from nexa.api.dependencies import ApiServices
from nexa.api.routes_auth import router as auth_router
from nexa.application.identity_service import LoginResult


def test_login_does_not_run_password_work_on_the_event_loop() -> None:
    class SlowIdentity:
        def login(self, **_kwargs) -> LoginResult:
            time.sleep(0.2)
            return LoginResult(cookie="opaque-session", session={"csrf_token": "csrf"})

    settings = SimpleNamespace(
        public_origin="https://nexa.test",
        api_json_max_bytes=65_536,
        trusted_proxy_networks=(),
    )
    app = FastAPI()
    app.state.services = ApiServices(
        settings=settings,
        identity=SlowIdentity(),
        admin=None,
        policy=None,
    )
    app.include_router(auth_router)

    async def exercise() -> tuple[float, float]:
        clock: dict[str, float] = {}

        async def login() -> None:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://nexa.test",
            ) as client:
                response = await client.post(
                    "/v1/auth/login",
                    headers={"Origin": "https://nexa.test"},
                    json={
                        "username": "admin@example.test",
                        "password": "correct-horse-battery-staple",
                    },
                )
                assert response.status_code == 200
                clock["login_done"] = time.perf_counter()

        async def heartbeat() -> None:
            await anyio.sleep(0.02)
            clock["heartbeat"] = time.perf_counter()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(login)
            tasks.start_soon(heartbeat)
        return clock["heartbeat"], clock["login_done"]

    heartbeat_at, login_done_at = anyio.run(exercise)

    assert heartbeat_at < login_done_at
