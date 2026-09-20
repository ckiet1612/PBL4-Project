from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from nexa.api.dependencies import resolve_principal, services
from nexa.api.schemas import (
    ArtifactKind,
    ArtifactPage,
    ArtifactResponse,
    ErrorResponse,
    UserArtifactKind,
    UuidV7,
)
from nexa.application.artifact_service import ArtifactService
from nexa.application.errors import ApplicationError

router = APIRouter(prefix="/v1")


def _error_responses(*statuses: int) -> dict[int, dict[str, object]]:
    return {status: {"model": ErrorResponse} for status in statuses}


def _artifact_service(request: Request) -> ArtifactService:
    service = services(request).artifact
    if not isinstance(service, ArtifactService):
        raise ApplicationError(
            code="dependency_unavailable",
            status=503,
            message="Artifact storage is unavailable",
            retry_after=1,
        )
    return service


_UUID7_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_MEDIA_TYPE_PATTERN = (
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+"
    r"(?:;[A-Za-z0-9!#$&^_.+;= -]+)?$"
)
_VERSION_ETAG_PATTERN = r'^"v[1-9][0-9]*"$'
_CHECKSUM_ETAG_PATTERN = r'^"sha256:[0-9a-f]{64}"$'
TenantContextHeader = Annotated[
    UuidV7,
    Header(
        alias="X-Nexa-Tenant-Id",
        json_schema_extra={"pattern": _UUID7_PATTERN},
    ),
]


@router.get(
    "/artifacts",
    operation_id="listArtifacts",
    response_model=ArtifactPage,
    responses=_error_responses(400, 401, 403, 500, 503),
)
def list_artifacts(
    request: Request,
    tenant_id: TenantContextHeader,
    page_size: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=16, max_length=2048),
    kind: Annotated[ArtifactKind | None, Query()] = None,
) -> dict:
    principal = resolve_principal(request, mutation=False)
    return _artifact_service(request).list_artifacts(
        principal, tenant_id=tenant_id, page_size=page_size, cursor=cursor, kind=kind
    )


@router.post(
    "/artifacts",
    operation_id="uploadArtifact",
    status_code=201,
    response_model=ArtifactResponse,
    responses={
        **_error_responses(400, 401, 403, 409, 413, 422, 429, 500, 503),
        201: {
            "description": "Durable committed artifact.",
            "headers": {
                "Location": {"schema": {"type": "string", "format": "uri-reference"}},
                "ETag": {"schema": {"type": "string", "pattern": _VERSION_ETAG_PATTERN}},
            },
        },
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def upload_artifact(
    request: Request,
    tenant_id: TenantContextHeader,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=16,
            max_length=128,
            pattern=r"^[!-~]{16,128}$",
        ),
    ],
    artifact_checksum: Annotated[
        str, Header(alias="X-Artifact-Checksum", pattern=r"^sha256:[0-9a-f]{64}$")
    ],
    artifact_size: Annotated[int, Header(alias="X-Artifact-Size", ge=0, le=1_099_511_627_776)],
    artifact_kind: Annotated[UserArtifactKind, Header(alias="X-Artifact-Kind")],
    artifact_media_type: Annotated[
        str,
        Header(
            alias="X-Artifact-Media-Type",
            min_length=1,
            max_length=127,
            pattern=_MEDIA_TYPE_PATTERN,
        ),
    ],
) -> JSONResponse:
    principal = await run_in_threadpool(resolve_principal, request, mutation=True)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/octet-stream":
        raise ApplicationError(
            code="validation_failed",
            status=400,
            message="Content-Type must be application/octet-stream",
        )
    parsed_content_length = None
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            parsed_content_length = int(content_length)
        except ValueError:
            raise ApplicationError(
                code="validation_failed", status=400, message="Content-Length is invalid"
            ) from None
        if parsed_content_length < 0:
            raise ApplicationError(
                code="validation_failed", status=400, message="Content-Length is invalid"
            )
    result = await _artifact_service(request).upload(
        principal,
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        kind=artifact_kind,
        media_type=artifact_media_type,
        expected_size=artifact_size,
        expected_checksum=artifact_checksum,
        chunks=request.stream(),
        content_length=parsed_content_length,
    )
    return JSONResponse(result.body, status_code=result.status, headers=result.headers)


@router.get(
    "/artifacts/{artifact_id}",
    operation_id="getArtifactMetadata",
    response_model=ArtifactResponse,
    responses={
        **_error_responses(400, 401, 403, 404, 500, 503),
        200: {
            "description": "Owned artifact metadata.",
            "headers": {"ETag": {"schema": {"type": "string", "pattern": _VERSION_ETAG_PATTERN}}},
        },
    },
)
def get_artifact_metadata(
    request: Request, tenant_id: TenantContextHeader, artifact_id: UuidV7
) -> JSONResponse:
    principal = resolve_principal(request, mutation=False)
    body = _artifact_service(request).get_artifact(
        principal, tenant_id=tenant_id, artifact_id=artifact_id
    )
    return JSONResponse(body, headers={"ETag": f'"v{body["version"]}"'})


@router.get(
    "/artifacts/{artifact_id}/content",
    operation_id="downloadArtifact",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Complete bytes",
            "headers": {
                "ETag": {"schema": {"type": "string", "pattern": _CHECKSUM_ETAG_PATTERN}},
                "X-Artifact-Media-Type": {
                    "schema": {"type": "string", "minLength": 1, "maxLength": 127}
                },
                "Content-Length": {"schema": {"type": "integer", "minimum": 0}},
            },
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        },
        206: {
            "description": "Single byte range",
            "headers": {
                "Content-Range": {"schema": {"type": "string"}},
                "ETag": {"schema": {"type": "string", "pattern": _CHECKSUM_ETAG_PATTERN}},
                "X-Artifact-Media-Type": {
                    "schema": {"type": "string", "minLength": 1, "maxLength": 127}
                },
            },
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        },
        **_error_responses(400, 401, 403, 404, 503),
        500: {"model": ErrorResponse},
    },
)
def download_artifact(
    request: Request,
    tenant_id: TenantContextHeader,
    artifact_id: UuidV7,
    range_header: str | None = Header(
        default=None, alias="Range", pattern=r"^bytes=[0-9]+-[0-9]*$"
    ),
) -> StreamingResponse:
    principal = resolve_principal(request, mutation=False)
    result = _artifact_service(request).download_artifact(
        principal,
        tenant_id=tenant_id,
        artifact_id=artifact_id,
        range_header=range_header,
    )

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
