import hmac
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import Request
from pydantic import BaseModel, ValidationError

from nexa.application.admin_service import AdminService
from nexa.application.errors import ApplicationError
from nexa.application.identity_service import IdentityService
from nexa.application.job_service import JobService
from nexa.application.json_codec import JsonRequestError, decode_json_object
from nexa.application.policy_service import PolicyService
from nexa.config import Settings
from nexa.domain.identity import Principal


@dataclass(frozen=True, slots=True)
class ApiServices:
    settings: Settings
    identity: IdentityService
    admin: AdminService
    policy: PolicyService
    artifact: object | None = None
    jobs: JobService | None = None


def services(request: Request) -> ApiServices:
    return request.app.state.services


async def parse_json_request[ModelT: BaseModel](
    request: Request, model: type[ModelT], *, settings: Settings
) -> tuple[ModelT, dict]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ApplicationError(
            code="validation_failed",
            status=400,
            message="Content-Type must be application/json",
        )
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > settings.api_json_max_bytes:
            raise ApplicationError(
                code="payload_too_large",
                status=413,
                message="JSON request body is too large",
            )
        chunks.append(chunk)
    try:
        decoded = decode_json_object(b"".join(chunks), max_bytes=settings.api_json_max_bytes)
    except JsonRequestError as exc:
        raise ApplicationError(code="validation_failed", status=400, message=str(exc)) from exc
    try:
        return model.model_validate(decoded), decoded
    except ValidationError as exc:
        status = 400 if any(error["type"] == "extra_forbidden" for error in exc.errors()) else 422
        raise ApplicationError(
            code="validation_failed",
            status=status,
            message="Request body does not match the approved schema",
        ) from None


def source_address(request: Request, settings: Settings) -> str:
    direct = request.client.host if request.client is not None else ""
    try:
        direct_ip = ip_address(direct)
    except ValueError:
        return direct
    if any(direct_ip in network for network in settings.trusted_proxy_networks):
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            try:
                return str(ip_address(forwarded))
            except ValueError:
                pass
    return str(direct_ip)


def maintenance_source_allowed(request: Request, settings: Settings) -> bool:
    try:
        source = ip_address(source_address(request, settings))
    except ValueError:
        return False
    return any(source in network for network in settings.maintenance_networks)


def require_browser_origin(request: Request, settings: Settings) -> None:
    expected = urlsplit(settings.public_origin)
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if origin != settings.public_origin or host != expected.netloc:
        raise ApplicationError(
            code="invalid_csrf",
            status=403,
            message="Origin or Host does not match the configured public origin",
        )


def _credentials(request: Request) -> tuple[str | None, str | None]:
    cookie = request.cookies.get("nexa_session")
    authorization = request.headers.get("authorization")
    bearer = None
    if authorization is not None:
        scheme, separator, value = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not value:
            raise ApplicationError(
                code="authentication_required",
                status=401,
                message="Invalid credentials",
            )
        bearer = value
    if cookie is not None and bearer is not None:
        raise ApplicationError(
            code="validation_failed",
            status=400,
            message="A request must use exactly one credential type",
        )
    return cookie, bearer


def resolve_principal(request: Request, *, mutation: bool) -> Principal:
    api = services(request)
    cookie, bearer = _credentials(request)
    if cookie is None and bearer is None:
        raise ApplicationError(
            code="authentication_required", status=401, message="Invalid credentials"
        )
    if bearer is not None:
        return api.identity.resolve_cli_token(bearer)
    if mutation:
        require_browser_origin(request, api.settings)
        csrf = request.headers.get("x-csrf-token")
        if csrf is None:
            raise ApplicationError(
                code="invalid_csrf",
                status=403,
                message="The CSRF token is missing or invalid",
            )
        authenticated = api.identity.resolve_browser_session(cookie)
        if not hmac.compare_digest(authenticated.csrf_token, csrf):
            raise ApplicationError(
                code="invalid_csrf",
                status=403,
                message="The CSRF token is missing or invalid",
            )
        return authenticated.principal
    return api.identity.resolve_browser_session(cookie).principal


def require_cookie_only(request: Request, *, mutation: bool) -> tuple[str, str | None]:
    api = services(request)
    cookie, bearer = _credentials(request)
    if bearer is not None or cookie is None:
        raise ApplicationError(
            code="authentication_required", status=401, message="Invalid credentials"
        )
    csrf = None
    if mutation:
        require_browser_origin(request, api.settings)
        csrf = request.headers.get("x-csrf-token")
        if csrf is None:
            raise ApplicationError(
                code="invalid_csrf",
                status=403,
                message="The CSRF token is missing or invalid",
            )
    return cookie, csrf
