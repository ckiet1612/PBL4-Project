from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from nexa.api.dependencies import parse_json_request, resolve_principal, services
from nexa.api.http import wire_response
from nexa.api.schemas import ErrorResponse, Sweep, SweepSubmitRequest, UuidV7
from nexa.application.errors import ApplicationError
from nexa.application.json_codec import jcs_request_hash
from nexa.application.sweep_service import SweepService

router = APIRouter(prefix="/v1")
_READ_SECURITY = [{"browserCookie": []}, {"cliBearer": []}]
_WRITE_SECURITY = [{"browserCookie": [], "csrfToken": []}, {"cliBearer": []}]


def _error_responses(*statuses: int) -> dict[int, dict[str, object]]:
    return {status: {"model": ErrorResponse} for status in statuses}


def _sweep_service(request: Request) -> SweepService:
    service = services(request).sweeps
    if not isinstance(service, SweepService):
        raise ApplicationError(
            code="dependency_unavailable",
            status=503,
            message="Sweep service is unavailable",
            retry_after=1,
        )
    return service


@router.post(
    "/sweeps",
    operation_id="submitSweep",
    status_code=207,
    response_model=Sweep,
    responses={
        **_error_responses(400, 401, 403, 409, 422, 429, 500, 503),
        207: {
            "description": "Parent committed with per-child accepted/rejected outcomes.",
            "headers": {"Location": {"schema": {"type": "string", "format": "uri-reference"}}},
        },
    },
    openapi_extra={"security": _WRITE_SECURITY},
)
async def submit_sweep(
    request: Request,
    tenant_id: Annotated[UuidV7, Header(alias="X-Nexa-Tenant-Id")],
    idempotency_key: str = Header(
        alias="Idempotency-Key",
        min_length=16,
        max_length=128,
        pattern=r"^[!-~]{16,128}$",
    ),
) -> JSONResponse:
    body, decoded = await parse_json_request(
        request, SweepSubmitRequest, settings=services(request).settings
    )
    principal = await run_in_threadpool(resolve_principal, request, mutation=True)
    request_hash = jcs_request_hash(
        {
            "operation_id": "submitSweep",
            "path": "/v1/sweeps",
            "tenant_id": str(tenant_id),
            "body": decoded,
        }
    )
    result = await run_in_threadpool(
        _sweep_service(request).submit,
        principal,
        tenant_id=tenant_id,
        request=body,
        decoded=decoded,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        request_id=str(request.state.request_id),
    )
    return wire_response(Sweep, result.body, status_code=result.status, headers=result.headers)


@router.get(
    "/sweeps/{sweep_id}",
    operation_id="getSweep",
    response_model=Sweep,
    responses=_error_responses(400, 401, 403, 404, 500, 503),
    openapi_extra={"security": _READ_SECURITY},
)
async def get_sweep(
    request: Request,
    sweep_id: UuidV7,
    tenant_id: Annotated[UuidV7, Header(alias="X-Nexa-Tenant-Id")],
    cursor: Annotated[str | None, Query(min_length=16, max_length=2048)] = None,
    page_size: int = Query(default=50, ge=1, le=100),
) -> JSONResponse:
    principal = await run_in_threadpool(resolve_principal, request, mutation=False)
    result = await run_in_threadpool(
        _sweep_service(request).get,
        principal,
        tenant_id=tenant_id,
        sweep_id=sweep_id,
        page_size=page_size,
        cursor=cursor,
    )
    return wire_response(Sweep, result)
