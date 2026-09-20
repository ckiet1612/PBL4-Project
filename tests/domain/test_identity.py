from uuid import UUID

import pytest

from nexa.domain.identity import (
    AuthorizationError,
    CredentialKind,
    Principal,
    TenantMembership,
    TokenScope,
    require_admin,
    require_tenant_membership,
    validate_requested_token_scopes,
)

USER_ID = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f1")
TENANT_ID = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f2")


def _principal(
    *,
    kind: CredentialKind = CredentialKind.CLI,
    scopes: frozenset[TokenScope] = frozenset(),
    memberships: tuple[TenantMembership, ...] = (),
    system_admin: bool = False,
) -> Principal:
    return Principal(
        user_id=USER_ID,
        credential_kind=kind,
        credential_id="credential-1",
        scopes=scopes,
        memberships=memberships,
        system_admin=system_admin,
    )


def test_write_scope_does_not_grant_corresponding_read_scope() -> None:
    principal = _principal(scopes=frozenset({TokenScope.JOBS_WRITE}))

    assert principal.has_scope(TokenScope.JOBS_WRITE) is True
    assert principal.has_scope(TokenScope.JOBS_READ) is False


def test_system_admin_without_membership_cannot_use_tenant_route() -> None:
    principal = _principal(
        scopes=frozenset({TokenScope.ADMIN_READ}),
        system_admin=True,
    )

    with pytest.raises(AuthorizationError, match="membership"):
        require_tenant_membership(principal, TENANT_ID)


def test_cli_admin_read_and_write_are_independent_and_need_live_grant() -> None:
    read_only_admin = _principal(
        scopes=frozenset({TokenScope.ADMIN_READ}),
        system_admin=True,
    )

    require_admin(read_only_admin, write=False)
    with pytest.raises(AuthorizationError, match="admin:write"):
        require_admin(read_only_admin, write=True)

    stale_token = _principal(scopes=frozenset({TokenScope.ADMIN_READ}), system_admin=False)
    with pytest.raises(AuthorizationError, match="SYSTEM_ADMIN"):
        require_admin(stale_token, write=False)


def test_browser_admin_does_not_need_cli_scope() -> None:
    principal = _principal(kind=CredentialKind.BROWSER, system_admin=True)

    require_admin(principal, write=True)


def test_bearer_token_creation_cannot_expand_existing_scope() -> None:
    principal = _principal(scopes=frozenset({TokenScope.TOKENS_WRITE, TokenScope.JOBS_WRITE}))

    validate_requested_token_scopes(principal, {TokenScope.JOBS_WRITE})
    with pytest.raises(AuthorizationError, match="exceed"):
        validate_requested_token_scopes(
            principal,
            {TokenScope.JOBS_WRITE, TokenScope.JOBS_READ},
        )


def test_browser_scope_authority_depends_on_live_membership_and_system_grant() -> None:
    member = TenantMembership(tenant_id=TENANT_ID, role="MEMBER")
    principal = _principal(
        kind=CredentialKind.BROWSER,
        memberships=(member,),
        system_admin=False,
    )

    validate_requested_token_scopes(
        principal,
        {TokenScope.TOKENS_WRITE, TokenScope.JOBS_READ, TokenScope.ARTIFACTS_WRITE},
    )
    with pytest.raises(AuthorizationError, match="exceed"):
        validate_requested_token_scopes(principal, {TokenScope.ADMIN_READ})
