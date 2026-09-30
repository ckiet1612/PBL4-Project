import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from nexa.api.dependencies import ApiServices
from nexa.api.routes_admin import router as admin_router
from nexa.api.routes_artifacts import router as artifacts_router
from nexa.api.routes_auth import router as auth_router
from nexa.api.routes_bootstrap import router as bootstrap_router
from nexa.api.routes_jobs import router as jobs_router
from nexa.api.routes_sweeps import router as sweeps_router
from nexa.api.routes_templates import router as templates_router
from nexa.api.routes_worker import router as worker_router
from nexa.application.admin_workers import AdminWorkerService
from nexa.application.artifact_service import ArtifactService
from nexa.application.errors import ApplicationError
from nexa.application.execution_service import ExecutionService
from nexa.application.identity_service import IdentityService
from nexa.application.job_service import JobService
from nexa.application.policy_service import PolicyService
from nexa.application.storage_identity import bind_artifact_store
from nexa.application.sweep_service import SweepService
from nexa.application.template_registry import TemplateCatalog
from nexa.config import Settings
from nexa.infrastructure.artifacts.store import FilesystemArtifactStore
from nexa.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema_guard import require_current_schema
from nexa.infrastructure.persistence.transactions import TransactionRetryExhausted

_LOG = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return str(value if value is not None else new_uuid7())


def _log_rejected_callback(request: Request, exc: ApplicationError) -> None:
    """Log a stale worker callback rejection as one bounded JSON line (B15-R33).

    The rejection commits nothing, so it is not a recovery event. The line carries the
    route template, the fixed message and the path identities only: never the credential,
    the callback body or the authority it presented.
    """
    route = request.scope.get("route")
    record: dict[str, Any] = {
        "event": "worker_callback_rejected",
        "method": request.method,
        "route": getattr(route, "path", None),
        "status": exc.status,
        "code": exc.code,
        "message": exc.message,
        "request_id": _request_id(request),
    }
    for name in ("worker_id", "attempt_id"):
        with suppress(TypeError, ValueError):
            record[name] = str(UUID(request.path_params.get(name)))
    _LOG.warning(json.dumps(record, separators=(",", ":")))


def _error_response(
    request: Request,
    *,
    status: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    reason: str | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    response_headers = {"X-Request-Id": request_id, **(headers or {})}
    body = {"code": code, "message": message, "request_id": request_id}
    if reason is not None:
        body["reason"] = reason
    return JSONResponse(
        body,
        status_code=status,
        headers=response_headers,
    )


def create_app(settings: Settings, *, engine: Engine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_engine = engine or create_database_engine(settings.database_url)
        owns_engine = engine is None
        try:
            require_current_schema(active_engine)
            session_factory = create_session_factory(active_engine)
            identity = IdentityService(session_factory, settings)
            identity.initialize()
            artifact_store = FilesystemArtifactStore(
                settings.artifact_root,
                max_file_bytes=settings.artifact_max_file_bytes,
            )
            bind_artifact_store(session_factory, artifact_store)
            job_service = JobService(session_factory, settings, identity, artifact_store)
            app.state.services = ApiServices(
                settings=settings,
                identity=identity,
                admin=AdminWorkerService(session_factory, settings, identity),
                policy=PolicyService(session_factory, settings, identity),
                artifact=ArtifactService(session_factory, settings, identity, artifact_store),
                jobs=job_service,
                templates=TemplateCatalog(session_factory, identity),
                sweeps=SweepService(job_service),
                worker=ExecutionService(
                    session_factory,
                    settings,
                    identity,
                    artifact_store=artifact_store,
                    storage_readiness=lambda: artifact_store.check_readiness(
                        critical_watermark_percent=settings.storage_critical_watermark_percent
                    ),
                ),
            )

            async def health_monitor() -> None:
                while True:
                    await asyncio.sleep(5)
                    try:
                        await asyncio.to_thread(app.state.services.worker.sweep_health)
                    except SQLAlchemyError:
                        _LOG.warning("worker health sweep unavailable")

            monitor = asyncio.create_task(health_monitor())
            try:
                yield
            finally:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
        finally:
            if owns_engine:
                active_engine.dispose()

    app = FastAPI(title="Nexa API", version="1.0.0", lifespan=lifespan)
    generated_openapi = app.openapi

    def openapi_with_contract_security() -> dict[str, Any]:
        schema = generated_openapi()
        schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
        schemes.update(
            {
                "browserCookie": {"type": "apiKey", "in": "cookie", "name": "nexa_session"},
                "csrfToken": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-CSRF-Token",
                },
                "cliBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "opaque-cli-token",
                    "description": (
                        "Exact non-hierarchical scopes. `jobs:read` authorizes "
                        "template/job/session/event/log/result reads; `jobs:write` authorizes "
                        "submit, sweep and job control; `artifacts:read` authorizes tenant "
                        "artifact list/download; `artifacts:write` authorizes tenant upload; "
                        "`tokens:write` authorizes only the caller's token list/create/revoke; "
                        "`admin:read` and `admin:write` authorize SYSTEM_ADMIN reads and "
                        "mutations. Current-principal introspection accepts any otherwise-valid "
                        "token. No scope bypasses membership, ownership, global grant, CSRF, "
                        "tenant context or object-state checks."
                    ),
                },
                "workerBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "opaque-worker-token",
                },
            }
        )
        return schema

    app.openapi = openapi_with_contract_security

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any):
        request.state.request_id = new_uuid7()
        response = await call_next(request)
        response.headers["X-Request-Id"] = str(request.state.request_id)
        return response

    @app.exception_handler(ApplicationError)
    async def application_error_handler(request: Request, exc: ApplicationError) -> JSONResponse:
        if exc.code == "stale_authority":
            _log_rejected_callback(request, exc)
        headers: dict[str, str] = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
        if exc.location is not None:
            headers["Location"] = exc.location
        return _error_response(
            request,
            status=exc.status,
            code=exc.code,
            message=exc.message,
            headers=headers,
            reason=exc.reason,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # A path or query parameter of the wrong type, pattern or format is malformed wire
        # input: 400, as the contract status table and every OpenAPI operation declare
        # (B16-R08). JSON bodies are parsed by `parse_json_request`, which applies the same
        # split; header errors keep 422 like the application's own header checks.
        wire = any(error["loc"][:1] in (("path",), ("query",)) for error in exc.errors())
        return _error_response(
            request,
            status=400 if wire else 422,
            code="validation_failed",
            message="Request parameters do not match the approved schema",
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        code = "resource_not_found" if exc.status_code == 404 else "validation_failed"
        return _error_response(
            request,
            status=exc.status_code,
            code=code,
            message="Resource was not found" if exc.status_code == 404 else "Invalid request",
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        return _error_response(
            request,
            status=503,
            code="dependency_unavailable",
            message="The database dependency is unavailable",
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(TransactionRetryExhausted)
    async def transaction_retry_handler(
        request: Request, exc: TransactionRetryExhausted
    ) -> JSONResponse:
        return _error_response(
            request,
            status=503,
            code="dependency_unavailable",
            message="The database transaction could not be completed",
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return _error_response(
            request,
            status=500,
            code="internal_error",
            message="The request could not be completed safely",
        )

    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(bootstrap_router)
    app.include_router(artifacts_router)
    app.include_router(jobs_router)
    app.include_router(templates_router)
    app.include_router(sweeps_router)
    app.include_router(worker_router)
    return app
