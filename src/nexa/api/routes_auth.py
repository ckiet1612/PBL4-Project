from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from nexa.api.dependencies import (
    parse_json_request,
    require_browser_origin,
    require_cookie_only,
    resolve_principal,
    services,
    source_address,
)
from nexa.api.schemas import LoginRequest, TokenCreateRequest
from nexa.application.json_codec import jcs_request_hash, json_wire_value

router = APIRouter(prefix="/v1")


@router.post("/auth/login", operation_id="loginBrowserSession")
async def login_browser_session(request: Request) -> Response:
    api = services(request)
    require_browser_origin(request, api.settings)
    body, _ = await parse_json_request(request, LoginRequest, settings=api.settings)
    result = await run_in_threadpool(
        api.identity.login,
        username=body.username,
        password=body.password,
        source=source_address(request, api.settings),
    )
    response = JSONResponse(json_wire_value(result.session), status_code=200)
    response.set_cookie(
        "nexa_session",
        result.cookie,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/auth/session", operation_id="getBrowserSession")
def get_browser_session(request: Request) -> dict:
    cookie, _ = require_cookie_only(request, mutation=False)
    return services(request).identity.get_browser_session(cookie)


@router.post("/auth/logout", operation_id="logoutBrowserSession", status_code=204)
def logout_browser_session(request: Request) -> Response:
    cookie, csrf = require_cookie_only(request, mutation=True)
    services(request).identity.logout(cookie, csrf or "")
    response = Response(status_code=204)
    response.delete_cookie(
        "nexa_session",
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/tokens", operation_id="listCliTokens")
def list_cli_tokens(
    request: Request,
    cursor: str | None = Query(default=None, min_length=16, max_length=2048),
    page_size: int = Query(default=50, ge=1, le=100),
) -> dict:
    principal = resolve_principal(request, mutation=False)
    return services(request).identity.list_cli_tokens(principal, page_size=page_size, cursor=cursor)


@router.post("/tokens", operation_id="createCliToken", status_code=201)
async def create_cli_token(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> Response:
    api = services(request)
    principal = await run_in_threadpool(resolve_principal, request, mutation=True)
    body, decoded = await parse_json_request(request, TokenCreateRequest, settings=api.settings)
    created = await run_in_threadpool(
        api.identity.create_cli_token,
        principal,
        name=body.name,
        scopes=set(body.scopes),
        expires_in_seconds=body.expires_in_seconds,
        idempotency_key=idempotency_key,
        request_hash=jcs_request_hash(decoded),
    )
    return JSONResponse(
        created,
        status_code=201,
        headers={"Location": f"/v1/tokens/{created['token_id']}"},
    )


@router.delete("/tokens/{token_id}", operation_id="revokeCliToken", status_code=204)
def revoke_cli_token(
    request: Request,
    token_id: UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> Response:
    api = services(request)
    principal = resolve_principal(request, mutation=True)
    api.identity.revoke_cli_token(
        principal,
        token_id,
        idempotency_key=idempotency_key,
        request_hash=jcs_request_hash({"token_id": str(token_id)}),
    )
    return Response(status_code=204)
