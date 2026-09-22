from datetime import UTC, datetime, timedelta

import pytest

from tests.api.test_http_contract import _bootstrap_and_login, _client

pytestmark = pytest.mark.postgres

EXPECTED_B06_OPERATIONS = {
    "loginBrowserSession",
    "logoutBrowserSession",
    "getBrowserSession",
    "listCliTokens",
    "createCliToken",
    "revokeCliToken",
    "adminListTenants",
    "adminCreateTenant",
    "adminGetTenant",
    "adminUpdateTenant",
    "adminListUsers",
    "adminCreateUser",
    "adminGetUser",
    "adminUpdateUser",
    "adminListMemberships",
    "adminUpsertMembership",
    "adminDeleteMembership",
    "adminGetGlobalPolicy",
    "adminUpdateGlobalPolicy",
    "adminGetTenantPolicy",
    "adminUpdateTenantPolicy",
    "bootstrapInitialAdmin",
    "bootstrapLocalWorker",
    "adminListAuditRecords",
}

EXPECTED_B07_OPERATIONS = {
    "listArtifacts",
    "uploadArtifact",
    "getArtifactMetadata",
    "downloadArtifact",
}

EXPECTED_B08_OPERATIONS = {
    "submitJob",
    "listJobs",
    "getJob",
    "getLogicalSession",
    "listJobEvents",
}

EXPECTED_B10_OPERATIONS = {
    "workerCreateIncarnation",
    "workerGetReconciliation",
    "workerHeartbeat",
    "workerPollDispatch",
    "workerAdoptAttempt",
    "workerRenewAttempt",
}

_DUMMY_ID = "018f05c4-a922-7d0d-9f55-f9084a72d0f9"
ADMIN_OPERATION_CASES = (
    ("GET", "/v1/admin/tenants", None, 200, 403),
    (
        "POST",
        "/v1/admin/tenants",
        {"slug": "matrix-denied", "display_name": "Denied"},
        403,
        201,
    ),
    ("GET", f"/v1/admin/tenants/{_DUMMY_ID}", None, 404, 403),
    ("PATCH", f"/v1/admin/tenants/{_DUMMY_ID}", {"display_name": "Denied"}, 403, 404),
    ("GET", "/v1/admin/users", None, 200, 403),
    (
        "POST",
        "/v1/admin/users",
        {
            "username": "denied@example.test",
            "display_name": "Denied",
            "password": "denied-password-value",
            "system_roles": [],
        },
        403,
        201,
    ),
    ("GET", f"/v1/admin/users/{_DUMMY_ID}", None, 404, 403),
    ("PATCH", f"/v1/admin/users/{_DUMMY_ID}", {"display_name": "Denied"}, 403, 404),
    ("GET", f"/v1/admin/tenants/{_DUMMY_ID}/memberships", None, 404, 403),
    (
        "POST",
        f"/v1/admin/tenants/{_DUMMY_ID}/memberships",
        {"user_id": _DUMMY_ID, "role": "MEMBER"},
        403,
        404,
    ),
    (
        "DELETE",
        f"/v1/admin/tenants/{_DUMMY_ID}/memberships/{_DUMMY_ID}",
        None,
        403,
        404,
    ),
    ("GET", "/v1/admin/policy", None, 200, 403),
    ("PATCH", "/v1/admin/policy", {"global_outstanding_limit": 99999}, 403, 200),
    ("GET", f"/v1/admin/tenants/{_DUMMY_ID}/policy", None, 404, 403),
    ("PATCH", f"/v1/admin/tenants/{_DUMMY_ID}/policy", {"weight": 1}, 403, 404),
    (
        "GET",
        "/v1/admin/audit?from=2026-09-20T00:00:00Z&to=2026-09-21T00:00:00Z",
        None,
        200,
        403,
    ),
)


def test_b06_and_b07_operation_ids_are_registered(migrated_postgres_engine, tmp_path) -> None:
    client = _client(migrated_postgres_engine, tmp_path)
    operation_ids = {
        operation["operationId"]
        for path in client.app.openapi()["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    assert operation_ids == (
        EXPECTED_B06_OPERATIONS
        | EXPECTED_B07_OPERATIONS
        | EXPECTED_B08_OPERATIONS
        | EXPECTED_B10_OPERATIONS
    )


def test_every_admin_operation_rejects_anonymous_member_worker_and_bootstrap_credentials(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as admin_client:
        for index, (method, path, body, _, _) in enumerate(ADMIN_OPERATION_CASES):
            response = admin_client.request(
                method,
                path,
                headers={
                    "Idempotency-Key": f"anonymous-matrix-{index:04d}",
                    "If-Match": '"v1"',
                },
                json=body,
            )
            assert response.status_code == 401, (method, path, response.text)

        admin_login, _ = _bootstrap_and_login(admin_client)
        write = {
            "Origin": "https://nexa.test",
            "X-CSRF-Token": admin_login["csrf_token"],
        }
        member = admin_client.post(
            "/v1/admin/users",
            headers={**write, "Idempotency-Key": "create-matrix-member1"},
            json={
                "username": "matrix-member@example.test",
                "display_name": "Matrix Member",
                "password": "matrix-member-password",
                "system_roles": [],
            },
        )
        assert member.status_code == 201
        worker = admin_client.post(
            "/v1/internal/worker-bootstrap",
            headers={
                "X-Nexa-Bootstrap-Secret": "b" * 32,
                "Idempotency-Key": "worker-matrix-0001",
            },
            json={
                "installation_id": "018f05c4-a922-7d0d-9f55-f9084a72d0f1",
                "credential_public_fingerprint": "sha256:" + "a" * 64,
            },
        )
        assert worker.status_code == 201
        worker_credential = worker.json()["credential"]

        with (
            _client(migrated_postgres_engine, tmp_path) as member_client,
            _client(migrated_postgres_engine, tmp_path) as credential_client,
        ):
            member_login = member_client.post(
                "/v1/auth/login",
                headers={"Origin": "https://nexa.test"},
                json={
                    "username": "matrix-member@example.test",
                    "password": "matrix-member-password",
                },
            )
            assert member_login.status_code == 200
            member_headers = {
                "Origin": "https://nexa.test",
                "X-CSRF-Token": member_login.json()["csrf_token"],
            }

            for index, (method, path, body, _, _) in enumerate(ADMIN_OPERATION_CASES):
                common = {
                    "Idempotency-Key": f"member-matrix-{index:04d}",
                    "If-Match": '"v1"',
                }
                member_response = member_client.request(
                    method,
                    path,
                    headers={**common, **member_headers},
                    json=body,
                )
                assert member_response.status_code == 403, (
                    method,
                    path,
                    member_response.text,
                )
                worker_response = credential_client.request(
                    method,
                    path,
                    headers={**common, "Authorization": f"Bearer {worker_credential}"},
                    json=body,
                )
                assert worker_response.status_code == 401, (
                    method,
                    path,
                    worker_response.text,
                )
                bootstrap_response = credential_client.request(
                    method,
                    path,
                    headers={**common, "X-Nexa-Bootstrap-Secret": "b" * 32},
                    json=body,
                )
                assert bootstrap_response.status_code == 401, (
                    method,
                    path,
                    bootstrap_response.text,
                )


def test_every_admin_operation_requires_the_exact_cli_system_admin_scope(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as admin_client:
        login, _ = _bootstrap_and_login(admin_client)
        browser_headers = {
            "Origin": "https://nexa.test",
            "X-CSRF-Token": login["csrf_token"],
        }
        token_scopes = {
            "unscoped": ["tokens:write"],
            "read": ["tokens:write", "admin:read"],
            "write": ["tokens:write", "admin:write"],
        }
        tokens: dict[str, str] = {}
        for index, (label, scopes) in enumerate(token_scopes.items()):
            created = admin_client.post(
                "/v1/tokens",
                headers={
                    **browser_headers,
                    "Idempotency-Key": f"cli-scope-matrix-token-{index:04d}",
                },
                json={
                    "name": f"matrix-{label}",
                    "scopes": scopes,
                    "expires_in_seconds": 300,
                },
            )
            assert created.status_code == 201, created.text
            tokens[label] = created.json()["token"]

    with _client(migrated_postgres_engine, tmp_path) as credential_client:
        for label in ("unscoped", "read", "write"):
            token = tokens[label]
            for index, (
                method,
                path,
                body,
                read_status,
                write_status,
            ) in enumerate(ADMIN_OPERATION_CASES):
                expected_status = {
                    "unscoped": 403,
                    "read": read_status,
                    "write": write_status,
                }[label]
                response = credential_client.request(
                    method,
                    path,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Idempotency-Key": f"cli-{label}-matrix-{index:04d}",
                        "If-Match": '"v1"',
                    },
                    json=body,
                )
                assert response.status_code == expected_status, (
                    label,
                    method,
                    path,
                    response.text,
                )


def test_remaining_identity_policy_audit_and_worker_operations(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        login, _ = _bootstrap_and_login(client)
        write = {
            "Origin": "https://nexa.test",
            "X-CSRF-Token": login["csrf_token"],
        }
        user = client.post(
            "/v1/admin/users",
            headers={**write, "Idempotency-Key": "create-user-http-001"},
            json={
                "username": "member@example.test",
                "display_name": "Member",
                "password": "member-password-value",
                "system_roles": [],
            },
        )
        assert user.status_code == 201, user.text
        user_id = user.json()["user_id"]
        assert client.get("/v1/admin/users").status_code == 200
        fetched_user = client.get(f"/v1/admin/users/{user_id}")
        assert fetched_user.headers["etag"] == '"v1"'
        updated_user = client.patch(
            f"/v1/admin/users/{user_id}",
            headers={
                **write,
                "Idempotency-Key": "update-user-http-001",
                "If-Match": '"v1"',
            },
            json={"display_name": "Member Updated"},
        )
        assert updated_user.status_code == 200

        tenant = client.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": "create-tenant-http11"},
            json={"slug": "matrix-team", "display_name": "Matrix Team"},
        )
        tenant_id = tenant.json()["tenant_id"]
        empty = client.get(f"/v1/admin/tenants/{tenant_id}/memberships")
        assert empty.status_code == 200
        assert empty.headers["etag"] == '"v1"'
        membership = client.post(
            f"/v1/admin/tenants/{tenant_id}/memberships",
            headers={
                **write,
                "Idempotency-Key": "upsert-member-http01",
                "If-Match": '"v1"',
            },
            json={"user_id": user_id, "role": "TENANT_ADMIN"},
        )
        assert membership.status_code == 200
        assert membership.headers["etag"] == '"v2"'
        removed = client.delete(
            f"/v1/admin/tenants/{tenant_id}/memberships/{user_id}",
            headers={
                **write,
                "Idempotency-Key": "delete-member-http01",
                "If-Match": '"v2"',
            },
        )
        assert removed.status_code == 204
        assert removed.headers["etag"] == '"v3"'

        global_policy = client.get("/v1/admin/policy")
        assert global_policy.status_code == 200
        global_updated = client.patch(
            "/v1/admin/policy",
            headers={
                **write,
                "Idempotency-Key": "global-policy-http01",
                "If-Match": global_policy.headers["etag"],
            },
            json={"global_outstanding_limit": 99999},
        )
        assert global_updated.status_code == 200
        assert global_updated.headers["etag"] == '"v2"'

        worker = client.post(
            "/v1/internal/worker-bootstrap",
            headers={
                "X-Nexa-Bootstrap-Secret": "b" * 32,
                "Idempotency-Key": "worker-bootstrap-http1",
            },
            json={
                "installation_id": "018f05c4-a922-7d0d-9f55-f9084a72d0f1",
                "credential_public_fingerprint": "sha256:" + "a" * 64,
            },
        )
        assert worker.status_code == 201, worker.text
        assert "credential" in worker.json()
        worker_replay = client.post(
            "/v1/internal/worker-bootstrap",
            headers={
                "X-Nexa-Bootstrap-Secret": "b" * 32,
                "Idempotency-Key": "worker-bootstrap-http1",
            },
            json={
                "installation_id": "018f05c4-a922-7d0d-9f55-f9084a72d0f1",
                "credential_public_fingerprint": "sha256:" + "a" * 64,
            },
        )
        assert worker_replay.status_code == 409
        assert worker_replay.json()["code"] == "one_time_secret_unavailable"
        with _client(migrated_postgres_engine, tmp_path) as worker_client:
            assert (
                worker_client.get(
                    "/v1/admin/tenants",
                    headers={"Authorization": f"Bearer {worker.json()['credential']}"},
                ).status_code
                == 401
            )

        now = datetime.now(UTC)
        audit = client.get(
            "/v1/admin/audit",
            params={
                "from": (now - timedelta(minutes=5)).isoformat(),
                "to": (now + timedelta(minutes=5)).isoformat(),
                "action": "admin.user.create",
            },
        )
        assert audit.status_code == 200, audit.text
        assert [item["action"] for item in audit.json()["items"]] == ["admin.user.create"]


def test_http_authorization_matrix_keeps_role_scope_and_credential_types_separate(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        assert client.get("/v1/admin/tenants").status_code == 401
        admin_login, _ = _bootstrap_and_login(client)
        write = {
            "Origin": "https://nexa.test",
            "X-CSRF-Token": admin_login["csrf_token"],
        }
        user = client.post(
            "/v1/admin/users",
            headers={**write, "Idempotency-Key": "create-user-http-101"},
            json={
                "username": "tenant-admin@example.test",
                "display_name": "Tenant Admin",
                "password": "tenant-admin-password",
                "system_roles": [],
            },
        ).json()
        tenant = client.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": "create-tenant-http21"},
            json={"slug": "auth-team", "display_name": "Auth Team"},
        ).json()
        client.post(
            f"/v1/admin/tenants/{tenant['tenant_id']}/memberships",
            headers={
                **write,
                "Idempotency-Key": "upsert-member-http11",
                "If-Match": '"v1"',
            },
            json={"user_id": user["user_id"], "role": "TENANT_ADMIN"},
        )
        with _client(migrated_postgres_engine, tmp_path) as member_client:
            member_client.cookies.clear()
            member_login = member_client.post(
                "/v1/auth/login",
                headers={"Origin": "https://nexa.test"},
                json={
                    "username": "tenant-admin@example.test",
                    "password": "tenant-admin-password",
                },
            )
            assert member_login.status_code == 200
            assert member_client.get("/v1/admin/tenants").status_code == 403

        token = client.post(
            "/v1/tokens",
            headers={
                **write,
                "Idempotency-Key": "create-token-http-101",
            },
            json={
                "name": "admin-read",
                "scopes": ["tokens:write", "admin:read"],
                "expires_in_seconds": 300,
            },
        ).json()["token"]
        client.cookies.clear()
        assert (
            client.get(
                "/v1/admin/tenants", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 200
        )
        denied = client.post(
            "/v1/admin/tenants",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "create-tenant-http22",
            },
            json={"slug": "scope-denied", "display_name": "Denied"},
        )
        assert denied.status_code == 403
        assert (
            client.get("/v1/auth/session", headers={"Authorization": f"Bearer {token}"}).status_code
            == 401
        )


def test_user_disable_revokes_existing_session_and_token_and_hides_cross_user_token(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as admin_client:
        admin_login, _ = _bootstrap_and_login(admin_client)
        admin_write = {
            "Origin": "https://nexa.test",
            "X-CSRF-Token": admin_login["csrf_token"],
        }
        created_user = admin_client.post(
            "/v1/admin/users",
            headers={**admin_write, "Idempotency-Key": "create-user-http-201"},
            json={
                "username": "disabled@example.test",
                "display_name": "Disabled Later",
                "password": "member-password-value",
                "system_roles": [],
            },
        )
        assert created_user.status_code == 201
        user_id = created_user.json()["user_id"]

        with _client(migrated_postgres_engine, tmp_path) as member_client:
            member_login = member_client.post(
                "/v1/auth/login",
                headers={"Origin": "https://nexa.test"},
                json={
                    "username": "disabled@example.test",
                    "password": "member-password-value",
                },
            )
            assert member_login.status_code == 200
            member_csrf = member_login.json()["csrf_token"]
            created_token = member_client.post(
                "/v1/tokens",
                headers={
                    "Origin": "https://nexa.test",
                    "X-CSRF-Token": member_csrf,
                    "Idempotency-Key": "create-token-http-201",
                },
                json={
                    "name": "member-token",
                    "scopes": ["tokens:write"],
                    "expires_in_seconds": 300,
                },
            )
            assert created_token.status_code == 201
            token_id = created_token.json()["token_id"]
            raw_token = created_token.json()["token"]

            cross_user = admin_client.delete(
                f"/v1/tokens/{token_id}",
                headers={
                    **admin_write,
                    "Idempotency-Key": "revoke-token-http-201",
                },
            )
            assert cross_user.status_code == 404

            disabled = admin_client.patch(
                f"/v1/admin/users/{user_id}",
                headers={
                    **admin_write,
                    "Idempotency-Key": "disable-user-http-201",
                    "If-Match": '"v1"',
                },
                json={"enabled": False},
            )
            assert disabled.status_code == 200

            assert member_client.get("/v1/auth/session").status_code == 401
            member_client.cookies.clear()
            assert (
                member_client.get(
                    "/v1/tokens",
                    headers={"Authorization": f"Bearer {raw_token}"},
                ).status_code
                == 401
            )
