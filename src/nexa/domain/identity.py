from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class AuthorizationError(PermissionError):
    pass


class CredentialKind(StrEnum):
    BROWSER = "BROWSER"
    CLI = "CLI"
    WORKER = "WORKER"
    BOOTSTRAP = "BOOTSTRAP"


class TokenScope(StrEnum):
    JOBS_READ = "jobs:read"
    JOBS_WRITE = "jobs:write"
    ARTIFACTS_READ = "artifacts:read"
    ARTIFACTS_WRITE = "artifacts:write"
    TOKENS_WRITE = "tokens:write"
    ADMIN_READ = "admin:read"
    ADMIN_WRITE = "admin:write"


@dataclass(frozen=True, slots=True)
class TenantMembership:
    tenant_id: UUID
    role: str


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID | None
    credential_kind: CredentialKind
    credential_id: str
    scopes: frozenset[TokenScope]
    memberships: tuple[TenantMembership, ...]
    system_admin: bool

    def has_scope(self, scope: TokenScope) -> bool:
        return scope in self.scopes


def require_admin(principal: Principal, *, write: bool) -> None:
    if principal.user_id is None or not principal.system_admin:
        raise AuthorizationError("An active SYSTEM_ADMIN grant is required")
    if principal.credential_kind is CredentialKind.BROWSER:
        return
    if principal.credential_kind is not CredentialKind.CLI:
        raise AuthorizationError("This credential type cannot call admin operations")
    required = TokenScope.ADMIN_WRITE if write else TokenScope.ADMIN_READ
    if not principal.has_scope(required):
        raise AuthorizationError(f"The exact {required.value} scope is required")


def require_tenant_membership(principal: Principal, tenant_id: UUID) -> TenantMembership:
    if principal.credential_kind not in {CredentialKind.BROWSER, CredentialKind.CLI}:
        raise AuthorizationError("This credential type has no tenant membership authority")
    for membership in principal.memberships:
        if membership.tenant_id == tenant_id:
            return membership
    raise AuthorizationError("An active target-tenant membership is required")


def validate_requested_token_scopes(principal: Principal, requested: set[TokenScope]) -> None:
    if not requested:
        raise AuthorizationError("At least one token scope is required")
    if principal.credential_kind is CredentialKind.CLI:
        if TokenScope.TOKENS_WRITE not in principal.scopes:
            raise AuthorizationError("The exact tokens:write scope is required")
        allowed = principal.scopes
    elif principal.credential_kind is CredentialKind.BROWSER:
        allowed = {TokenScope.TOKENS_WRITE}
        if principal.memberships:
            allowed.update(
                {
                    TokenScope.JOBS_READ,
                    TokenScope.JOBS_WRITE,
                    TokenScope.ARTIFACTS_READ,
                    TokenScope.ARTIFACTS_WRITE,
                }
            )
        if principal.system_admin:
            allowed.update({TokenScope.ADMIN_READ, TokenScope.ADMIN_WRITE})
    else:
        raise AuthorizationError("This credential type cannot create CLI tokens")
    if not requested <= set(allowed):
        raise AuthorizationError("Requested scopes exceed the caller's current authority")
