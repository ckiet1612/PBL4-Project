from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from nexa.api.dependencies import resolve_principal, services
from nexa.api.http import wire_response
from nexa.api.schemas import ErrorResponse, Template, TemplateList, UuidV7
from nexa.application.errors import ApplicationError
from nexa.application.template_registry import TemplateCatalog

router = APIRouter(prefix="/v1")
_READ_SECURITY = [{"browserCookie": []}, {"cliBearer": []}]


def _error_responses(*statuses: int) -> dict[int, dict[str, object]]:
    return {status: {"model": ErrorResponse} for status in statuses}


def _catalog(request: Request) -> TemplateCatalog:
    catalog = services(request).templates
    if not isinstance(catalog, TemplateCatalog):
        raise ApplicationError(
            code="dependency_unavailable",
            status=503,
            message="Template catalog is unavailable",
            retry_after=1,
        )
    return catalog


@router.get(
    "/templates",
    operation_id="listTemplates",
    response_model=TemplateList,
    responses=_error_responses(400, 401, 403, 500, 503),
    openapi_extra={"security": _READ_SECURITY},
)
async def list_templates(
    request: Request,
    tenant_id: Annotated[UuidV7, Header(alias="X-Nexa-Tenant-Id")],
    enabled: Annotated[bool, Query()] = True,
) -> JSONResponse:
    principal = await run_in_threadpool(resolve_principal, request, mutation=False)
    result = await run_in_threadpool(
        _catalog(request).list_templates, principal, tenant_id=tenant_id, enabled=enabled
    )
    return wire_response(TemplateList, result)


@router.get(
    "/templates/{template_id}",
    operation_id="getTemplate",
    response_model=Template,
    responses=_error_responses(400, 401, 403, 404, 500, 503),
    openapi_extra={"security": _READ_SECURITY},
)
async def get_template(
    request: Request,
    tenant_id: Annotated[UuidV7, Header(alias="X-Nexa-Tenant-Id")],
    template_id: Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{1,63}$")],
) -> JSONResponse:
    principal = await run_in_threadpool(resolve_principal, request, mutation=False)
    result = await run_in_threadpool(
        _catalog(request).get_template, principal, tenant_id=tenant_id, template_id=template_id
    )
    return wire_response(Template, result)
