from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from nexa.api.dependencies import parse_json_request, resolve_principal, services
from nexa.api.http import strong_etag
from nexa.api.schemas import (
    CheckpointPage,
    ErrorResponse,
    EventPage,
    Job,
    JobPage,
    JobState,
    JobSubmitRequest,
    LogicalSession,
    ResultRecord,
    UuidV7,
)
from nexa.application.errors import ApplicationError
from nexa.application.job_service import JobOperationResult, JobService
from nexa.application.json_codec import jcs_request_hash

router = APIRouter(prefix="/v1")
_READ_SECURITY = [{"browserCookie": []}, {"cliBearer": []}]
_WRITE_SECURITY = [{"browserCookie": [], "csrfToken": []}, {"cliBearer": []}]


def _error_responses(*statuses: int) -> dict[int, dict[str, object]]:
    return {status: {"model": ErrorResponse} for status in statuses}


def _job_service(request: Request) -> JobService:
    service = services(request).jobs
    if not isinstance(service, JobService):
        raise ApplicationError(
            code="dependency_unavailable",
            status=503,
            message="Job service is unavailable",
            retry_after=1,
        )
    return service


def _result(result: JobOperationResult) -> JSONResponse:
    return JSONResponse(result.body, status_code=result.status, headers=result.headers)


@router.get(
    "/jobs",
    operation_id="listJobs",
    response_model=JobPage,
    responses=_error_responses(400, 401, 403, 500, 503),
    openapi_extra={"security": _READ_SECURITY},
)
async def list_jobs(
    request: Request,
    tenant_id: Annotated[UuidV7, Header(alias="X-Nexa-Tenant-Id")],
    cursor: Annotated[str | None, Query(min_length=16, max_length=2048)] = None,
    page_size: int = Query(default=50, ge=1, le=100),
    state: Annotated[JobState | None, Query()] = None,
    template_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    created_after: Annotated[datetime | None, Query()] = None,
) -> dict:
    principal = await run_in_threadpool(resolve_principal, request, mutation=False)
    return await run_in_threadpool(
        _job_service(request).list_jobs,
        principal,
        tenant_id=tenant_id,
        page_size=page_size,
        cursor=cursor,
        state=state,
        template_id=template_id,
        created_after=created_after,
    )


@router.post(
    "/jobs",
    operation_id="submitJob",
    status_code=202,
    response_model=Job,
    responses={
        **_error_responses(400, 401, 403, 409, 422, 429, 500, 503),
        202: {
            "description": "Job accepted and durable.",
            "headers": {
                "Location": {"schema": {"type": "string", "format": "uri-reference"}},
                "ETag": {"$ref": "#/components/headers/ETag"},
            },
        },
    },
    openapi_extra={"security": _WRITE_SECURITY},
)
async def submit_job(
    request: Request,
    tenant_id: Annotated[UuidV7, Header(alias="X-Nexa-Tenant-Id")],
    idempotency_key: str = Header(
        alias="Idempotency-Key",
        min_length=16,
        max_length=128,
        pattern=r"^[!-~]{16,128}$",
    ),
) -> Response:
    body, decoded = await parse_json_request(
        request, JobSubmitRequest, settings=services(request).settings
    )
    principal = await run_in_threadpool(resolve_principal, request, mutation=True)
    request_hash = jcs_request_hash(
        {
            "operation_id": "submitJob",
            "path": "/v1/jobs",
            "tenant_id": str(tenant_id),
            "body": decoded,
        }
    )
    result = await run_in_threadpool(
        _job_service(request).submit,
        principal,
        tenant_id=tenant_id,
        request=body,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    return _result(result)


@router.get(
    "/jobs/{job_id}",
    operation_id="getJob",
    response_model=Job,
    responses=_error_responses(400, 401, 403, 404, 500, 503),
    openapi_extra={"security": _READ_SECURITY},
)
async def get_job(
    request: Request,
    job_id: UuidV7,
    tenant_id: Annotated[UuidV7, Header(alias="X-Nexa-Tenant-Id")],
) -> Response:
    principal = await run_in_threadpool(resolve_principal, request, mutation=False)
    body = await run_in_threadpool(
        _job_service(request).get_job,
        principal,
        tenant_id=tenant_id,
        job_id=job_id,
    )
    return JSONResponse(body, headers={"ETag": strong_etag(int(body["version"]))})


@router.get(
    "/jobs/{job_id}/result",
    operation_id="getJobResult",
    response_model=ResultRecord,
    responses=_error_responses(400, 401, 403, 404, 500, 503),
    openapi_extra={"security": _READ_SECURITY},
)
async def get_job_result(
    request: Request,
    job_id: UuidV7,
    tenant_id: Annotated[UuidV7, Header(alias="X-Nexa-Tenant-Id")],
) -> dict:
    principal = await run_in_threadpool(resolve_principal, request, mutation=False)
    return await run_in_threadpool(
        _job_service(request).get_result,
        principal,
        tenant_id=tenant_id,
        job_id=job_id,
    )


@router.get(
    "/sessions/{session_id}",
    operation_id="getLogicalSession",
    response_model=LogicalSession,
    responses=_error_responses(400, 401, 403, 404, 500, 503),
    openapi_extra={"security": _READ_SECURITY},
)
async def get_logical_session(
    request: Request,
    session_id: UuidV7,
    tenant_id: Annotated[UuidV7, Header(alias="X-Nexa-Tenant-Id")],
) -> dict:
    principal = await run_in_threadpool(resolve_principal, request, mutation=False)
    return await run_in_threadpool(
        _job_service(request).get_session, principal, tenant_id=tenant_id, session_id=session_id
    )


@router.get(
    "/jobs/{job_id}/events",
    operation_id="listJobEvents",
    response_model=EventPage,
    responses=_error_responses(400, 401, 403, 404, 500, 503),
    openapi_extra={"security": _READ_SECURITY},
)
async def list_job_events(
    request: Request,
    job_id: UuidV7,
    tenant_id: Annotated[UuidV7, Header(alias="X-Nexa-Tenant-Id")],
    after_sequence: int = Query(default=0, ge=0),
    page_size: int = Query(default=50, ge=1, le=100),
) -> dict:
    principal = await run_in_threadpool(resolve_principal, request, mutation=False)
    return await run_in_threadpool(
        _job_service(request).list_events,
        principal,
        tenant_id=tenant_id,
        job_id=job_id,
        after_sequence=after_sequence,
        page_size=page_size,
    )


@router.get(
    "/jobs/{job_id}/checkpoints",
    operation_id="listJobCheckpoints",
    response_model=CheckpointPage,
    responses=_error_responses(400, 401, 403, 404, 500, 503),
    openapi_extra={"security": _READ_SECURITY},
)
async def list_job_checkpoints(
    request: Request,
    job_id: UuidV7,
    tenant_id: Annotated[UuidV7, Header(alias="X-Nexa-Tenant-Id")],
    cursor: Annotated[str | None, Query(min_length=16, max_length=2048)] = None,
    page_size: int = Query(default=50, ge=1, le=100),
) -> JSONResponse:
    principal = await run_in_threadpool(resolve_principal, request, mutation=False)
    body = await run_in_threadpool(
        _job_service(request).list_checkpoints,
        principal,
        tenant_id=tenant_id,
        job_id=job_id,
        cursor=cursor,
        page_size=page_size,
    )
    # Return the service wire body as-is: re-validating through the response model
    # would re-render created_at with microseconds instead of the contract's milliseconds.
    return JSONResponse(body)
