from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import UUID7, AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from nexa.api.dependencies import parse_json_request, resolve_principal, services
from nexa.api.http import strong_etag, wire_response
from nexa.api.schemas import (
    AdminReasonRequest,
    ErrorResponse,
    FairnessReport,
    GlobalPolicyUpdate,
    Job,
    JobPage,
    JobState,
    MembershipWriteRequest,
    TenantCreateRequest,
    TenantPolicyUpdate,
    TenantUpdateRequest,
    UserCreateRequest,
    UserUpdateRequest,
    UuidV7,
    WaitingReasonValue,
)
from nexa.application.errors import ApplicationError
from nexa.application.json_codec import jcs_request_hash
from nexa.application.preconditions import VersionPrecondition

router = APIRouter(prefix="/v1/admin")


def _updated_fields(model) -> dict:
    fields = model.model_dump(exclude_unset=True, exclude_none=True)
    if not fields:
        raise ApplicationError(
            code="validation_failed",
            status=422,
            message="At least one field must be provided",
        )
    return fields


@router.get("/tenants", operation_id="adminListTenants")
def admin_list_tenants(
    request: Request,
    cursor: str | None = Query(default=None, min_length=16, max_length=2048),
    page_size: int = Query(default=50, ge=1, le=100),
) -> dict:
    principal = resolve_principal(request, mutation=False)
    return services(request).admin.list_tenants(principal, page_size=page_size, cursor=cursor)


@router.post("/tenants", operation_id="adminCreateTenant", status_code=201)
async def admin_create_tenant(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    api = services(request)
    principal = await run_in_threadpool(resolve_principal, request, mutation=True)
    body, decoded = await parse_json_request(request, TenantCreateRequest, settings=api.settings)
    return await run_in_threadpool(
        api.admin.create_tenant,
        principal,
        slug=body.slug,
        display_name=body.display_name,
        idempotency_key=idempotency_key,
        request_hash=jcs_request_hash(decoded),
    )


@router.get("/tenants/{tenant_id}", operation_id="adminGetTenant")
def admin_get_tenant(request: Request, tenant_id: UUID) -> Response:
    principal = resolve_principal(request, mutation=False)
    body = services(request).admin.get_tenant(principal, tenant_id)
    return JSONResponse(body, headers={"ETag": strong_etag(body["version"])})


@router.patch("/tenants/{tenant_id}", operation_id="adminUpdateTenant")
async def admin_update_tenant(
    request: Request,
    tenant_id: UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    api = services(request)
    principal = await run_in_threadpool(resolve_principal, request, mutation=True)
    body, decoded = await parse_json_request(request, TenantUpdateRequest, settings=api.settings)
    fields = _updated_fields(body)
    updated = await run_in_threadpool(
        api.admin.update_tenant,
        principal,
        tenant_id=tenant_id,
        expected_version=VersionPrecondition(if_match),
        idempotency_key=idempotency_key,
        request_hash=jcs_request_hash({**decoded, "tenant_id": str(tenant_id)}),
        **fields,
    )
    return JSONResponse(updated, headers={"ETag": strong_etag(updated["version"])})


@router.get("/users", operation_id="adminListUsers")
def admin_list_users(
    request: Request,
    cursor: str | None = Query(default=None, min_length=16, max_length=2048),
    page_size: int = Query(default=50, ge=1, le=100),
) -> dict:
    principal = resolve_principal(request, mutation=False)
    return services(request).admin.list_users(principal, page_size=page_size, cursor=cursor)


@router.post("/users", operation_id="adminCreateUser", status_code=201)
async def admin_create_user(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    api = services(request)
    principal = await run_in_threadpool(resolve_principal, request, mutation=True)
    body, decoded = await parse_json_request(request, UserCreateRequest, settings=api.settings)
    return await run_in_threadpool(
        api.admin.create_user,
        principal,
        username=body.username,
        display_name=body.display_name,
        password=body.password,
        system_roles=set(body.system_roles),
        idempotency_key=idempotency_key,
        request_hash=jcs_request_hash(decoded),
    )


@router.get("/users/{user_id}", operation_id="adminGetUser")
def admin_get_user(request: Request, user_id: UUID) -> Response:
    principal = resolve_principal(request, mutation=False)
    body = services(request).admin.get_user(principal, user_id)
    return JSONResponse(body, headers={"ETag": strong_etag(body["version"])})


@router.patch("/users/{user_id}", operation_id="adminUpdateUser")
async def admin_update_user(
    request: Request,
    user_id: UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    api = services(request)
    principal = await run_in_threadpool(resolve_principal, request, mutation=True)
    body, decoded = await parse_json_request(request, UserUpdateRequest, settings=api.settings)
    fields = _updated_fields(body)
    if "system_roles" in fields:
        fields["system_roles"] = set(fields["system_roles"])
    updated = await run_in_threadpool(
        api.admin.update_user,
        principal,
        user_id=user_id,
        expected_version=VersionPrecondition(if_match),
        idempotency_key=idempotency_key,
        request_hash=jcs_request_hash({**decoded, "user_id": str(user_id)}),
        **fields,
    )
    return JSONResponse(updated, headers={"ETag": strong_etag(updated["version"])})


@router.get(
    "/tenants/{tenant_id}/memberships",
    operation_id="adminListMemberships",
)
def admin_list_memberships(
    request: Request,
    tenant_id: UUID,
    cursor: str | None = Query(default=None, min_length=16, max_length=2048),
    page_size: int = Query(default=50, ge=1, le=100),
) -> Response:
    principal = resolve_principal(request, mutation=False)
    body = services(request).admin.list_memberships(
        principal, tenant_id, page_size=page_size, cursor=cursor
    )
    return JSONResponse(
        body,
        headers={"ETag": strong_etag(body["membership_set_version"])},
    )


@router.post(
    "/tenants/{tenant_id}/memberships",
    operation_id="adminUpsertMembership",
)
async def admin_upsert_membership(
    request: Request,
    tenant_id: UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    api = services(request)
    principal = await run_in_threadpool(resolve_principal, request, mutation=True)
    body, decoded = await parse_json_request(request, MembershipWriteRequest, settings=api.settings)
    result = await run_in_threadpool(
        api.admin.upsert_membership,
        principal,
        tenant_id=tenant_id,
        user_id=body.user_id,
        role=body.role,
        expected_version=VersionPrecondition(if_match),
        idempotency_key=idempotency_key,
        request_hash=jcs_request_hash({**decoded, "tenant_id": str(tenant_id)}),
    )
    return JSONResponse(result.body, headers={"ETag": strong_etag(result.version)})


@router.delete(
    "/tenants/{tenant_id}/memberships/{user_id}",
    operation_id="adminDeleteMembership",
    status_code=204,
)
def admin_delete_membership(
    request: Request,
    tenant_id: UUID,
    user_id: UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    principal = resolve_principal(request, mutation=True)
    version = services(request).admin.delete_membership(
        principal,
        tenant_id=tenant_id,
        user_id=user_id,
        expected_version=VersionPrecondition(if_match),
        idempotency_key=idempotency_key,
        request_hash=jcs_request_hash({"tenant_id": str(tenant_id), "user_id": str(user_id)}),
    )
    return Response(status_code=204, headers={"ETag": strong_etag(version)})


@router.get("/policy", operation_id="adminGetGlobalPolicy")
def admin_get_global_policy(request: Request) -> Response:
    principal = resolve_principal(request, mutation=False)
    body = services(request).policy.get_global_policy(principal)
    return JSONResponse(body, headers={"ETag": strong_etag(body["version"])})


@router.patch("/policy", operation_id="adminUpdateGlobalPolicy")
async def admin_update_global_policy(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    api = services(request)
    principal = await run_in_threadpool(resolve_principal, request, mutation=True)
    body, decoded = await parse_json_request(request, GlobalPolicyUpdate, settings=api.settings)
    fields = _updated_fields(body)
    updated = await run_in_threadpool(
        api.policy.update_global_policy,
        principal,
        expected_version=VersionPrecondition(if_match),
        global_outstanding_limit=fields.get("global_outstanding_limit"),
        operational_mode=fields.get("operational_mode"),
        idempotency_key=idempotency_key,
        request_hash=jcs_request_hash(decoded),
    )
    return JSONResponse(updated, headers={"ETag": strong_etag(updated["version"])})


@router.get("/tenants/{tenant_id}/policy", operation_id="adminGetTenantPolicy")
def admin_get_tenant_policy(request: Request, tenant_id: UUID) -> Response:
    principal = resolve_principal(request, mutation=False)
    body = services(request).policy.get_tenant_policy(principal, tenant_id)
    return JSONResponse(body, headers={"ETag": strong_etag(body["version"])})


@router.patch("/tenants/{tenant_id}/policy", operation_id="adminUpdateTenantPolicy")
async def admin_update_tenant_policy(
    request: Request,
    tenant_id: UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    api = services(request)
    principal = await run_in_threadpool(resolve_principal, request, mutation=True)
    body, decoded = await parse_json_request(request, TenantPolicyUpdate, settings=api.settings)
    changes = _updated_fields(body)
    updated = await run_in_threadpool(
        api.policy.update_tenant_policy,
        principal,
        tenant_id=tenant_id,
        expected_version=VersionPrecondition(if_match),
        changes=changes,
        idempotency_key=idempotency_key,
        request_hash=jcs_request_hash({**decoded, "tenant_id": str(tenant_id)}),
    )
    return JSONResponse(updated, headers={"ETag": strong_etag(updated["version"])})


@router.get("/audit", operation_id="adminListAuditRecords")
def admin_list_audit_records(
    request: Request,
    from_at: Annotated[datetime, Query(alias="from")],
    to_at: Annotated[datetime, Query(alias="to")],
    action: str | None = Query(default=None, min_length=1, max_length=64),
    cursor: str | None = Query(default=None, min_length=16, max_length=2048),
    page_size: int = Query(default=50, ge=1, le=100),
) -> dict:
    principal = resolve_principal(request, mutation=False)
    return services(request).admin.list_audit_records(
        principal,
        page_size=page_size,
        cursor=cursor,
        action=action,
        from_at=from_at,
        to_at=to_at,
    )


@router.get("/workers", operation_id="adminListWorkers")
def admin_list_workers(
    request: Request,
    cursor: str | None = Query(default=None, min_length=16, max_length=2048),
    page_size: int = Query(default=50, ge=1, le=100),
) -> Response:
    principal = resolve_principal(request, mutation=False)
    return JSONResponse(
        services(request).admin.list_workers(principal, page_size=page_size, cursor=cursor)
    )


@router.get("/workers/{worker_id}", operation_id="adminGetWorker")
def admin_get_worker(request: Request, worker_id: UUID) -> Response:
    principal = resolve_principal(request, mutation=False)
    body = services(request).admin.get_worker(principal, worker_id)
    return JSONResponse(body, headers={"ETag": strong_etag(body["version"])})


async def _worker_mutation(
    request: Request,
    worker_id: UUID,
    idempotency_key: str,
    if_match: str | None,
    *,
    method: str,
    status_code: int,
) -> Response:
    api = services(request)
    principal = await run_in_threadpool(resolve_principal, request, mutation=True)
    body, decoded = await parse_json_request(request, AdminReasonRequest, settings=api.settings)
    updated = await run_in_threadpool(
        getattr(api.admin, method),
        principal,
        worker_id=worker_id,
        reason=body.reason,
        expected_version=VersionPrecondition(if_match),
        idempotency_key=idempotency_key,
        request_hash=jcs_request_hash({**decoded, "worker_id": str(worker_id)}),
    )
    return JSONResponse(
        updated, status_code=status_code, headers={"ETag": strong_etag(updated["version"])}
    )


@router.post("/workers/{worker_id}/drain", operation_id="adminDrainWorker", status_code=202)
async def admin_drain_worker(
    request: Request,
    worker_id: UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    return await _worker_mutation(
        request, worker_id, idempotency_key, if_match, method="drain_worker", status_code=202
    )


@router.post("/workers/{worker_id}/disable", operation_id="adminDisableWorker", status_code=202)
async def admin_disable_worker(
    request: Request,
    worker_id: UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    return await _worker_mutation(
        request, worker_id, idempotency_key, if_match, method="disable_worker", status_code=202
    )


@router.post("/workers/{worker_id}/enable", operation_id="adminEnableWorker")
async def admin_enable_worker(
    request: Request,
    worker_id: UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    return await _worker_mutation(
        request, worker_id, idempotency_key, if_match, method="enable_worker", status_code=200
    )


@router.get("/allocations", operation_id="adminListAllocations")
def admin_list_allocations(
    request: Request,
    cursor: str | None = Query(default=None, min_length=16, max_length=2048),
    page_size: int = Query(default=50, ge=1, le=100),
    state: Literal["HELD", "QUARANTINED", "RELEASED"] | None = Query(default=None),
) -> Response:
    principal = resolve_principal(request, mutation=False)
    return JSONResponse(
        services(request).admin.list_allocations(
            principal, page_size=page_size, cursor=cursor, state=state
        )
    )


@router.get("/recovery-events", operation_id="adminListRecoveryEvents")
def admin_list_recovery_events(
    request: Request,
    from_at: Annotated[datetime, Query(alias="from")],
    to_at: Annotated[datetime, Query(alias="to")],
    cursor: str | None = Query(default=None, min_length=16, max_length=2048),
    page_size: int = Query(default=50, ge=1, le=100),
) -> Response:
    principal = resolve_principal(request, mutation=False)
    return JSONResponse(
        services(request).admin.list_recovery_events(
            principal, page_size=page_size, cursor=cursor, from_at=from_at, to_at=to_at
        )
    )


_READ_SECURITY = [{"browserCookie": []}, {"cliBearer": []}]


def _error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {status: {"model": ErrorResponse} for status in statuses}


def _aware(value: datetime | None, name: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ApplicationError(
            code="validation_failed",
            status=400,
            message=f"The {name} timestamp must include a timezone",
        )
    return value


@router.get(
    "/jobs",
    operation_id="adminListJobs",
    response_model=JobPage,
    responses=_error_responses(400, 401, 403, 500, 503),
    openapi_extra={"security": _READ_SECURITY},
)
def admin_list_jobs(
    request: Request,
    cursor: Annotated[str | None, Query(min_length=16, max_length=2048)] = None,
    page_size: int = Query(default=50, ge=1, le=100),
    tenant_id: Annotated[UuidV7 | None, Query()] = None,
    user_id: Annotated[UuidV7 | None, Query()] = None,
    state: Annotated[JobState | None, Query()] = None,
    waiting_reason: Annotated[WaitingReasonValue | None, Query()] = None,
    created_after: Annotated[datetime | None, Query()] = None,
) -> Response:
    principal = resolve_principal(request, mutation=False)
    result = services(request).admin.list_jobs(
        principal,
        page_size=page_size,
        cursor=cursor,
        tenant_id=tenant_id,
        user_id=user_id,
        state=state,
        waiting_reason=waiting_reason,
        created_after=_aware(created_after, "created_after"),
    )
    return wire_response(JobPage, result)


@router.get(
    "/jobs/{job_id}",
    operation_id="adminGetJob",
    response_model=Job,
    responses=_error_responses(400, 401, 403, 404, 500, 503),
    openapi_extra={"security": _READ_SECURITY},
)
def admin_get_job(request: Request, job_id: UuidV7) -> Response:
    principal = resolve_principal(request, mutation=False)
    body = services(request).admin.get_job(principal, job_id=job_id)
    return wire_response(Job, body, headers={"ETag": strong_etag(int(body["version"]))})


class _FairnessQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    from_at: AwareDatetime = Field(alias="from")
    to_at: AwareDatetime = Field(alias="to")
    bucket_seconds: int = Field(ge=1, le=86_400)
    tenant_id: UUID7 | None = None


_TIMESTAMP_SCHEMA = {"$ref": "#/components/schemas/Timestamp"}
_FAIRNESS_PARAMETERS = [
    {"name": "from", "in": "query", "required": True, "schema": _TIMESTAMP_SCHEMA},
    {"name": "to", "in": "query", "required": True, "schema": _TIMESTAMP_SCHEMA},
    {
        "name": "bucket_seconds",
        "in": "query",
        "required": True,
        "schema": {"type": "integer", "minimum": 1, "maximum": 86400},
    },
    {
        "name": "tenant_id",
        "in": "query",
        "required": False,
        "schema": {"$ref": "#/components/schemas/UuidV7"},
    },
]


# Parameters are parsed here rather than declared, so every violation is a 400 and the
# operation documents no 422 (contract adminQueryFairness, B18-R07).
@router.get(
    "/fairness",
    operation_id="adminQueryFairness",
    response_model=FairnessReport,
    responses=_error_responses(400, 401, 403, 500, 503),
    openapi_extra={"parameters": _FAIRNESS_PARAMETERS, "security": _READ_SECURITY},
)
def admin_query_fairness(request: Request) -> Response:
    try:
        query = _FairnessQuery.model_validate(dict(request.query_params))
    except ValidationError as error:
        names = sorted({str(item["loc"][0]) for item in error.errors() if item["loc"]})
        raise ApplicationError(
            code="validation_failed",
            status=400,
            message=f"Invalid fairness query parameter: {', '.join(names) or 'query'}",
        ) from None
    principal = resolve_principal(request, mutation=False)
    report = services(request).admin.query_fairness(
        principal,
        from_at=query.from_at,
        to_at=query.to_at,
        bucket_seconds=query.bucket_seconds,
        tenant_id=query.tenant_id,
    )
    return wire_response(FairnessReport, report)
