from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

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
from nexa.application.admin_service import AdminService
from nexa.application.artifact_service import ArtifactService
from nexa.application.errors import ApplicationError
from nexa.application.identity_service import IdentityService
from nexa.application.policy_service import PolicyService
from nexa.config import Settings
from nexa.infrastructure.artifacts.store import FilesystemArtifactStore
from nexa.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema_guard import require_current_schema
from nexa.infrastructure.persistence.transactions import TransactionRetryExhausted


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return str(value if value is not None else new_uuid7())


def _error_response(
    request: Request,
    *,
    status: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    response_headers = {"X-Request-Id": request_id, **(headers or {})}
    return JSONResponse(
        {"code": code, "message": message, "request_id": request_id},
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
            app.state.services = ApiServices(
                settings=settings,
                identity=identity,
                admin=AdminService(session_factory, settings, identity),
                policy=PolicyService(session_factory, settings, identity),
                artifact=ArtifactService(session_factory, settings, identity, artifact_store),
            )
            yield
        finally:
            if owns_engine:
                active_engine.dispose()

    app = FastAPI(title="Nexa API", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any):
        request.state.request_id = new_uuid7()
        response = await call_next(request)
        response.headers["X-Request-Id"] = str(request.state.request_id)
        return response

    @app.exception_handler(ApplicationError)
    async def application_error_handler(request: Request, exc: ApplicationError) -> JSONResponse:
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
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            request,
            status=422,
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
    return app
