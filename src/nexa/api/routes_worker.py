from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from nexa.api.dependencies import parse_json_request, services
from nexa.api.execution_cleanup_schemas import (
    CleanupRequest,
    CleanupResponse,
    FailureRequest,
    WorkerAck,
)
from nexa.api.schemas import (
    AdoptRequest,
    AdoptResponse,
    AuthorityRequest,
    CheckpointPublishRequest,
    CheckpointRecord,
    CheckpointReservationResponse,
    CompleteRequest,
    ErrorResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    PollRequest,
    PollResponse,
    ReconciliationPage,
    RenewBody,
    RenewResponse,
    ResultReservationResponse,
    StartRequest,
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


@router.post(
    "/attempts/{attempt_id}/result-reservations",
    operation_id="workerReserveResult",
    status_code=201,
    response_model=ResultReservationResponse,
    responses=_ERRORS,
    openapi_extra={"security": _SECURITY},
)
async def reserve_result(
    request: Request,
    attempt_id: UuidV7,
    callback_id: Annotated[UuidV7, Header(alias="X-Callback-Id")],
) -> JSONResponse:
    body, decoded = await parse_json_request(
        request, AuthorityRequest, settings=services(request).settings
    )
    result = await run_in_threadpool(
        _service(request).reserve_result,
        credential=_credential(request),
        attempt_id=attempt_id,
        callback_id=callback_id,
        payload_hash=jcs_request_hash(decoded),
        authority=body.authority,
    )
    return JSONResponse(result, status_code=201)


@router.post(
    "/attempts/{attempt_id}/checkpoint-reservations",
    operation_id="workerReserveCheckpoint",
    status_code=201,
    response_model=CheckpointReservationResponse,
    responses=_ERRORS,
    openapi_extra={"security": _SECURITY},
)
async def reserve_checkpoint(
    request: Request,
    attempt_id: UuidV7,
    callback_id: Annotated[UuidV7, Header(alias="X-Callback-Id")],
) -> JSONResponse:
    body, decoded = await parse_json_request(
        request, AuthorityRequest, settings=services(request).settings
    )
    result = await run_in_threadpool(
        _service(request).reserve_checkpoint,
        credential=_credential(request),
        attempt_id=attempt_id,
        callback_id=callback_id,
        payload_hash=jcs_request_hash(decoded),
        authority=body.authority,
    )
    return JSONResponse(result, status_code=201)


@router.post(
    "/attempts/{attempt_id}/checkpoints",
    operation_id="workerPublishCheckpoint",
    status_code=201,
    response_model=CheckpointRecord,
    responses=_ERRORS,
    openapi_extra={"security": _SECURITY},
)
async def publish_checkpoint(
    request: Request,
    attempt_id: UuidV7,
    callback_id: Annotated[UuidV7, Header(alias="X-Callback-Id")],
) -> JSONResponse:
    body, decoded = await parse_json_request(
        request, CheckpointPublishRequest, settings=services(request).settings
    )
    result = await run_in_threadpool(
        _service(request).publish_checkpoint,
        credential=_credential(request),
        attempt_id=attempt_id,
        callback_id=callback_id,
        payload_hash=jcs_request_hash(decoded),
        request=body,
    )
    return JSONResponse(result, status_code=201)


@router.post(
    "/attempts/{attempt_id}/claim",
    operation_id="workerClaimAttempt",
    responses=_ERRORS,
    openapi_extra={"security": _SECURITY},
)
async def claim_attempt(
    request: Request,
    attempt_id: UuidV7,
    callback_id: Annotated[UuidV7, Header(alias="X-Callback-Id")],
) -> dict:
    body, decoded = await parse_json_request(
        request, AuthorityRequest, settings=services(request).settings
    )
    return await run_in_threadpool(
        _service(request).claim_attempt,
        credential=_credential(request),
        attempt_id=attempt_id,
        callback_id=callback_id,
        payload_hash=jcs_request_hash(decoded),
        authority=body.authority,
    )


@router.post(
    "/attempts/{attempt_id}/start",
    operation_id="workerStartAttempt",
    responses=_ERRORS,
    openapi_extra={"security": _SECURITY},
)
async def start_attempt(
    request: Request,
    attempt_id: UuidV7,
    callback_id: Annotated[UuidV7, Header(alias="X-Callback-Id")],
) -> dict:
    body, decoded = await parse_json_request(
        request, StartRequest, settings=services(request).settings
    )
    return await run_in_threadpool(
        _service(request).start_attempt,
        credential=_credential(request),
        attempt_id=attempt_id,
        callback_id=callback_id,
        payload_hash=jcs_request_hash(decoded),
        request=body,
    )


def _header_authority(request, attempt_id):
    from pydantic import ValidationError

    from nexa.api.schemas import Authority

    try:
        return Authority.model_validate(
            {
                "attempt_id": attempt_id,
                "worker_id": request.headers.get("X-Worker-Id"),
                "worker_incarnation_id": request.headers.get("X-Worker-Incarnation-Id"),
                "allocation_id": request.headers.get("X-Allocation-Id"),
                "lease_id": request.headers.get("X-Lease-Id"),
                "job_fence": int(request.headers.get("X-Job-Fence", "0")),
            }
        )
    except (ValidationError, ValueError):
        raise ApplicationError(
            code="validation_failed", status=422, message="Invalid attempt authority headers"
        ) from None


@router.post(
    "/attempts/{attempt_id}/artifacts",
    operation_id="workerUploadAttemptArtifact",
    status_code=201,
    responses=_ERRORS,
    openapi_extra={"security": _SECURITY},
)
async def upload_attempt_artifact(
    request: Request,
    attempt_id: UuidV7,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    kind: Annotated[str, Header(alias="X-Artifact-Kind")],
    media_type: Annotated[str, Header(alias="X-Artifact-Media-Type")],
    size: Annotated[int, Header(alias="X-Artifact-Size", ge=0)],
    checksum: Annotated[str, Header(alias="X-Artifact-Checksum")],
):
    from nexa.application.execution_artifacts import AttemptArtifactService, WorkerUploadPrincipal

    if request.headers.get("content-type") != "application/octet-stream":
        raise ApplicationError(
            code="validation_failed", status=400, message="Expected binary artifact transport"
        )
    authority = _header_authority(request, attempt_id)
    service = AttemptArtifactService(
        services(request).artifact, _service(request), _credential(request), authority, kind
    )
    tenant = await run_in_threadpool(service.tenant_id)
    content_length = request.headers.get("content-length")
    result = await service.upload(
        WorkerUploadPrincipal(authority.worker_id),
        tenant_id=tenant,
        idempotency_key=idempotency_key,
        kind=kind,
        media_type=media_type,
        expected_size=size,
        expected_checksum=checksum,
        content_length=int(content_length) if content_length else None,
        chunks=request.stream(),
    )
    return JSONResponse(result.body, status_code=result.status, headers=result.headers)


@router.get(
    "/attempts/{attempt_id}/execution-artifacts/{artifact_id}/content",
    operation_id="workerDownloadExecutionArtifact",
    response_class=StreamingResponse,
    responses=_ERRORS,
    openapi_extra={"security": _SECURITY},
)
async def download_execution_artifact(
    request: Request,
    attempt_id: UuidV7,
    artifact_id: UuidV7,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
):
    from nexa.application.execution_artifacts import AttemptArtifactService

    authority = _header_authority(request, attempt_id)
    service = AttemptArtifactService(
        services(request).artifact, _service(request), _credential(request), authority
    )
    result = await run_in_threadpool(service.download_execution_artifact, artifact_id, range_header)

    def body_iterator():
        try:
            while True:
                chunk = result.reader.read(1024 * 1024)
                if not chunk:
                    return
                yield chunk
        finally:
            result.reader.close()

    return StreamingResponse(body_iterator(), status_code=result.status, headers=result.headers)


@router.post(
    "/attempts/{attempt_id}/fail",
    operation_id="workerFailAttempt",
    responses=_ERRORS,
    response_model=WorkerAck,
    openapi_extra={"security": _SECURITY},
)
async def fail_attempt(
    request: Request,
    attempt_id: UuidV7,
    callback_id: Annotated[UuidV7, Header(alias="X-Callback-Id")],
):
    body, decoded = await parse_json_request(
        request, FailureRequest, settings=services(request).settings
    )
    result = await run_in_threadpool(
        _service(request).fail_attempt,
        worker_id=body.authority.worker_id,
        credential=_credential(request),
        attempt_id=attempt_id,
        callback_id=callback_id,
        payload_hash=jcs_request_hash(decoded),
        request=body,
    )
    return result


@router.post(
    "/attempts/{attempt_id}/cleanup",
    operation_id="workerReportCleanup",
    responses=_ERRORS,
    response_model=CleanupResponse,
    openapi_extra={"security": _SECURITY},
)
async def report_cleanup(
    request: Request,
    attempt_id: UuidV7,
    callback_id: Annotated[UuidV7, Header(alias="X-Callback-Id")],
):
    body, decoded = await parse_json_request(
        request, CleanupRequest, settings=services(request).settings
    )
    result = await run_in_threadpool(
        _service(request).report_cleanup,
        worker_id=body.worker_id,
        credential=_credential(request),
        attempt_id=attempt_id,
        callback_id=callback_id,
        payload_hash=jcs_request_hash(decoded),
        request=body,
    )
    return JSONResponse(result, status_code=200 if result.get("verified") else 202)


@router.post(
    "/attempts/{attempt_id}/complete", operation_id="workerCompleteAttempt", responses=_ERRORS
)
async def complete_attempt(
    request: Request,
    attempt_id: UuidV7,
    callback_id: Annotated[UuidV7, Header(alias="X-Callback-Id")],
):
    body, decoded = await parse_json_request(
        request, CompleteRequest, settings=services(request).settings
    )
    return await run_in_threadpool(
        _service(request).complete_attempt,
        credential=_credential(request),
        attempt_id=attempt_id,
        callback_id=callback_id,
        payload_hash=jcs_request_hash(decoded),
        request=body,
    )
