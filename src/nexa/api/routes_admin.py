from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from nexa.api.dependencies import parse_json_request, resolve_principal, services
from nexa.api.http import strong_etag
from nexa.api.schemas import (
    GlobalPolicyUpdate,
    MembershipWriteRequest,
    TenantCreateRequest,
    TenantPolicyUpdate,
    TenantUpdateRequest,
    UserCreateRequest,
    UserUpdateRequest,
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
