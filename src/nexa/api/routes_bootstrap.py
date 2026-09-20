from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from nexa.api.dependencies import (
    maintenance_source_allowed,
    parse_json_request,
    services,
)
from nexa.api.http import strong_etag
from nexa.api.schemas import AdminBootstrapRequest, WorkerBootstrapRequest
from nexa.application.json_codec import jcs_request_hash
from nexa.infrastructure.security import header_secret_bytes

router = APIRouter(prefix="/v1/internal")


@router.post("/admin-bootstrap", operation_id="bootstrapInitialAdmin", status_code=201)
async def bootstrap_initial_admin(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    bootstrap_secret: str = Header(default="", alias="X-Nexa-Bootstrap-Secret"),
) -> JSONResponse:
    api = services(request)
    body, decoded = await parse_json_request(request, AdminBootstrapRequest, settings=api.settings)
    try:
        presented_secret = header_secret_bytes(bootstrap_secret)
    except ValueError:
        presented_secret = b""
    created = await run_in_threadpool(
        api.identity.bootstrap_admin,
        bootstrap_secret=presented_secret,
        source_allowed=maintenance_source_allowed(request, api.settings),
        username=body.username,
        display_name=body.display_name,
        password=body.password,
        idempotency_key=idempotency_key,
        request_hash=jcs_request_hash(decoded),
    )
    return JSONResponse(
        created,
        status_code=201,
        headers={
            "Location": f"/v1/admin/users/{created['user_id']}",
            "ETag": strong_etag(created["version"]),
        },
    )


@router.post("/worker-bootstrap", operation_id="bootstrapLocalWorker", status_code=201)
async def bootstrap_local_worker(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    bootstrap_secret: str = Header(default="", alias="X-Nexa-Bootstrap-Secret"),
) -> JSONResponse:
    api = services(request)
    body, decoded = await parse_json_request(request, WorkerBootstrapRequest, settings=api.settings)
    try:
        presented_secret = header_secret_bytes(bootstrap_secret)
    except ValueError:
        presented_secret = b""
    created = await run_in_threadpool(
        api.identity.bootstrap_worker,
        bootstrap_secret=presented_secret,
        source_allowed=maintenance_source_allowed(request, api.settings),
        installation_id=body.installation_id,
        credential_public_fingerprint=body.credential_public_fingerprint,
        idempotency_key=idempotency_key,
        request_hash=jcs_request_hash(decoded),
    )
    return JSONResponse(
        created,
        status_code=201,
        headers={"Location": f"/v1/admin/workers/{created['worker_id']}"},
    )
