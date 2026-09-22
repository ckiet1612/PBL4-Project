from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from nexa.api.dependencies import parse_json_request, services
from nexa.api.schemas import (
    AdoptRequest,
    AdoptResponse,
    ErrorResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    PollRequest,
    PollResponse,
    ReconciliationPage,
    RenewBody,
    RenewResponse,
    UuidV7,
    WorkerIncarnation,
    WorkerIncarnationCreateRequest,
)
from nexa.application.errors import ApplicationError
from nexa.application.json_codec import jcs_request_hash
from nexa.application.worker_service import WorkerService

router = APIRouter(prefix="/v1", tags=["Worker"])
_SECURITY = [{"workerBearer": []}]
_ERRORS = {status: {"model": ErrorResponse} for status in (400, 401, 403, 409, 422, 500, 503)}


def _credential(request: Request) -> str:
    if request.cookies.get("nexa_session") is not None:
        raise ApplicationError(
            code="authentication_required", status=401, message="Invalid credentials"
        )
    authorization = request.headers.get("authorization", "")
    scheme, separator, value = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not value or " " in value:
        raise ApplicationError(
            code="authentication_required", status=401, message="Invalid credentials"
        )
    return value


def _service(request: Request) -> WorkerService:
    service = services(request).worker
    if service is None:
        raise ApplicationError(
            code="dependency_unavailable",
            status=503,
            message="Worker service is unavailable",
            retry_after=1,
        )
    return service


@router.post(
    "/workers/{worker_id}/incarnations",
    operation_id="workerCreateIncarnation",
    status_code=201,
    response_model=WorkerIncarnation,
    responses=_ERRORS,
    openapi_extra={"security": _SECURITY},
)
async def create_incarnation(
    request: Request,
    worker_id: UuidV7,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=16, max_length=128, pattern=r"^[!-~]{16,128}$"),
    ],
) -> JSONResponse:
    body, decoded = await parse_json_request(
        request, WorkerIncarnationCreateRequest, settings=services(request).settings
    )
    result = await run_in_threadpool(
        _service(request).create_incarnation,
        worker_id=worker_id,
        credential=_credential(request),
        process_start_nonce=body.process_start_nonce,
        idempotency_key=idempotency_key,
        request_hash=jcs_request_hash(
            {
                "operation_id": "workerCreateIncarnation",
                "worker_id": str(worker_id),
                "body": decoded,
            }
        ),
    )
    return JSONResponse(result, status_code=201)


@router.get(
    "/workers/{worker_id}/reconciliation",
    operation_id="workerGetReconciliation",
    response_model=ReconciliationPage,
    responses=_ERRORS,
    openapi_extra={"security": _SECURITY},
)
async def get_reconciliation(
    request: Request,
    worker_id: UuidV7,
    incarnation_id: Annotated[UuidV7, Header(alias="X-Worker-Incarnation-Id")],
    cursor: Annotated[str | None, Query(min_length=16, max_length=2048)] = None,
    page_size: int = Query(default=50, ge=1, le=100),
) -> dict:
    return await run_in_threadpool(
        _service(request).get_reconciliation,
        worker_id=worker_id,
        credential=_credential(request),
        incarnation_id=incarnation_id,
        page_size=page_size,
        cursor=cursor,
    )


@router.post(
    "/workers/{worker_id}/heartbeat",
    operation_id="workerHeartbeat",
    response_model=HeartbeatResponse,
    responses=_ERRORS,
    openapi_extra={"security": _SECURITY},
)
async def heartbeat(
    request: Request,
    worker_id: UuidV7,
    callback_id: Annotated[UuidV7, Header(alias="X-Callback-Id")],
) -> dict:
    body, decoded = await parse_json_request(
        request, HeartbeatRequest, settings=services(request).settings
    )
    return await run_in_threadpool(
        _service(request).heartbeat,
        worker_id=worker_id,
        credential=_credential(request),
        callback_id=callback_id,
        payload_hash=jcs_request_hash(decoded),
        request=body,
    )


@router.post(
    "/workers/{worker_id}/poll",
    operation_id="workerPollDispatch",
    response_model=PollResponse,
    responses=_ERRORS,
    openapi_extra={"security": _SECURITY},
)
async def poll(request: Request, worker_id: UuidV7) -> dict:
    body, _ = await parse_json_request(request, PollRequest, settings=services(request).settings)
    return await run_in_threadpool(
        _service(request).poll,
        worker_id=worker_id,
        credential=_credential(request),
        incarnation_id=body.worker_incarnation_id,
        long_poll_seconds=body.long_poll_seconds,
    )


@router.post(
    "/attempts/{attempt_id}/adopt",
    operation_id="workerAdoptAttempt",
    response_model=AdoptResponse,
    responses=_ERRORS,
    openapi_extra={"security": _SECURITY},
)
async def adopt_attempt(
    request: Request,
    attempt_id: UuidV7,
    callback_id: Annotated[UuidV7, Header(alias="X-Callback-Id")],
) -> dict:
    body, decoded = await parse_json_request(
        request, AdoptRequest, settings=services(request).settings
    )
    return await run_in_threadpool(
        _service(request).adopt_attempt,
        worker_id=body.prior_authority.worker_id,
        credential=_credential(request),
        attempt_id=attempt_id,
        callback_id=callback_id,
        payload_hash=jcs_request_hash(decoded),
        request=body,
    )


@router.post(
    "/attempts/{attempt_id}/renew",
    operation_id="workerRenewAttempt",
    response_model=RenewResponse,
    responses=_ERRORS,
    openapi_extra={"security": _SECURITY},
)
async def renew_attempt(
    request: Request,
    attempt_id: UuidV7,
    callback_id: Annotated[UuidV7, Header(alias="X-Callback-Id")],
) -> dict:
    body, decoded = await parse_json_request(
        request, RenewBody, settings=services(request).settings
    )
    return await run_in_threadpool(
        _service(request).renew_attempt,
        worker_id=body.root.authority.worker_id,
        credential=_credential(request),
        attempt_id=attempt_id,
        callback_id=callback_id,
        payload_hash=jcs_request_hash(decoded),
        request=body.root,
    )
