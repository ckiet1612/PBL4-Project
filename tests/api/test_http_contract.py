from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from nexa.api.app import create_app
from nexa.infrastructure.persistence.transactions import TransactionRetryExhausted
from tests.integration.identity_support import make_identity_service

pytestmark = pytest.mark.postgres


def _client(migrated_postgres_engine, tmp_path) -> TestClient:
    identity = make_identity_service(migrated_postgres_engine, tmp_path)
    return TestClient(
        create_app(identity.settings, engine=migrated_postgres_engine),
        base_url="https://nexa.test",
        client=("127.0.0.1", 50000),
    )


def _bootstrap_and_login(client: TestClient) -> tuple[dict, str]:
    bootstrap = client.post(
        "/v1/internal/admin-bootstrap",
        headers={
            "X-Nexa-Bootstrap-Secret": "b" * 32,
            "Idempotency-Key": "bootstrap-admin-http-01",
        },
        json={
            "username": "admin@example.test",
            "display_name": "Initial Admin",
            "password": "correct-horse-battery-staple",
        },
    )
    assert bootstrap.status_code == 201, bootstrap.text
    login = client.post(
        "/v1/auth/login",
        headers={"Origin": "https://nexa.test"},
        json={
            "username": "admin@example.test",
            "password": "correct-horse-battery-staple",
        },
    )
    assert login.status_code == 200, login.text
    return login.json(), login.headers["set-cookie"]


def test_admin_bootstrap_returns_and_replays_the_user_version_etag(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        headers = {
            "X-Nexa-Bootstrap-Secret": "b" * 32,
            "Idempotency-Key": "bootstrap-admin-http-etag1",
        }
        body = {
            "username": "admin@example.test",
            "display_name": "Initial Admin",
            "password": "correct-horse-battery-staple",
        }
        created = client.post("/v1/internal/admin-bootstrap", headers=headers, json=body)
        replay = client.post("/v1/internal/admin-bootstrap", headers=headers, json=body)

        assert created.status_code == replay.status_code == 201
        assert created.headers["etag"] == replay.headers["etag"] == '"v1"'
        assert replay.json() == created.json()


def test_browser_session_cookie_csrf_errors_and_server_request_id(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        login, set_cookie = _bootstrap_and_login(client)
        assert "nexa_session=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "Path=/" in set_cookie
        request_id = UUID(client.get("/v1/auth/session").headers["x-request-id"])
        assert request_id.version == 7

        missing_csrf = client.post("/v1/auth/logout", headers={"Origin": "https://nexa.test"})
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["code"] == "invalid_csrf"
        assert UUID(missing_csrf.json()["request_id"]).version == 7

        forged_origin = client.post(
            "/v1/auth/logout",
            headers={
                "Origin": "https://attacker.test",
                "X-CSRF-Token": login["csrf_token"],
            },
        )
        assert forged_origin.status_code == 403

        logout = client.post(
            "/v1/auth/logout",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": login["csrf_token"],
            },
        )
        assert logout.status_code == 204
        assert "Max-Age=0" in logout.headers["set-cookie"]
        assert client.get("/v1/auth/session").status_code == 401


def test_strict_json_token_one_time_secret_and_mixed_credentials(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        login, _ = _bootstrap_and_login(client)
        duplicate = client.post(
            "/v1/tokens",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": login["csrf_token"],
                "Idempotency-Key": "create-token-http-001",
                "Content-Type": "application/json",
            },
            content=b'{"name":"one","name":"two","scopes":["tokens:write"],"expires_in_seconds":300}',
        )
        assert duplicate.status_code == 400

        unknown = client.post(
            "/v1/tokens",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": login["csrf_token"],
                "Idempotency-Key": "create-token-http-002",
            },
            json={
                "name": "admin-cli",
                "scopes": ["tokens:write", "admin:read"],
                "expires_in_seconds": 300,
                "unexpected": True,
            },
        )
        assert unknown.status_code == 400

        oversized_integer = client.post(
            "/v1/tokens",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": login["csrf_token"],
                "Idempotency-Key": "create-token-http-jcs01",
                "Content-Type": "application/json",
            },
            content=(
                b'{"name":"admin-cli","scopes":["tokens:write"],'
                b'"expires_in_seconds":9007199254740992}'
            ),
        )
        assert oversized_integer.status_code == 400

        invalid_unicode = client.post(
            "/v1/tokens",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": login["csrf_token"],
                "Idempotency-Key": "create-token-http-jcs02",
                "Content-Type": "application/json",
            },
            content=(b'{"name":"\\ud800","scopes":["tokens:write"],"expires_in_seconds":300}'),
        )
        assert invalid_unicode.status_code == 400

        created = client.post(
            "/v1/tokens",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": login["csrf_token"],
                "Idempotency-Key": "create-token-http-003",
            },
            json={
                "name": "admin-cli",
                "scopes": ["tokens:write", "admin:read"],
                "expires_in_seconds": 300,
            },
        )
        assert created.status_code == 201, created.text
        raw_token = created.json()["token"]
        replay = client.post(
            "/v1/tokens",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": login["csrf_token"],
                "Idempotency-Key": "create-token-http-003",
            },
            json={
                "name": "admin-cli",
                "scopes": ["tokens:write", "admin:read"],
                "expires_in_seconds": 300,
            },
        )
        assert replay.status_code == 409
        assert replay.json()["code"] == "one_time_secret_unavailable"
        assert "token" not in replay.text

        mixed = client.get("/v1/tokens", headers={"Authorization": f"Bearer {raw_token}"})
        assert mixed.status_code == 400


def test_admin_tenant_and_policy_routes_enforce_etag(migrated_postgres_engine, tmp_path) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        login, _ = _bootstrap_and_login(client)
        mutation_headers = {
            "Origin": "https://nexa.test",
            "X-CSRF-Token": login["csrf_token"],
        }
        created = client.post(
            "/v1/admin/tenants",
            headers={**mutation_headers, "Idempotency-Key": "create-tenant-http01"},
            json={"slug": "http-team", "display_name": "HTTP Team"},
        )
        assert created.status_code == 201, created.text
        tenant_id = created.json()["tenant_id"]
        fetched = client.get(f"/v1/admin/tenants/{tenant_id}")
        assert fetched.status_code == 200
        assert fetched.headers["etag"] == '"v1"'

        missing = client.patch(
            f"/v1/admin/tenants/{tenant_id}",
            headers={**mutation_headers, "Idempotency-Key": "update-tenant-http01"},
            json={"display_name": "Updated"},
        )
        assert missing.status_code == 428
        updated = client.patch(
            f"/v1/admin/tenants/{tenant_id}",
            headers={
                **mutation_headers,
                "Idempotency-Key": "update-tenant-http02",
                "If-Match": '"v1"',
            },
            json={"display_name": "Updated"},
        )
        assert updated.status_code == 200
        assert updated.headers["etag"] == '"v2"'
        replay_without_if_match = client.patch(
            f"/v1/admin/tenants/{tenant_id}",
            headers={
                **mutation_headers,
                "Idempotency-Key": "update-tenant-http02",
            },
            json={"display_name": "Updated"},
        )
        assert replay_without_if_match.status_code == 200
        assert replay_without_if_match.json() == updated.json()
        assert replay_without_if_match.headers["etag"] == '"v2"'

        policy = client.get(f"/v1/admin/tenants/{tenant_id}/policy")
        assert policy.status_code == 200
        assert policy.headers["etag"] == '"v1"'
        changed_policy = client.patch(
            f"/v1/admin/tenants/{tenant_id}/policy",
            headers={
                **mutation_headers,
                "Idempotency-Key": "update-policy-http01",
                "If-Match": '"v1"',
            },
            json={"weight": 0.5},
        )
        assert changed_policy.status_code == 200, changed_policy.text
        assert changed_policy.headers["etag"] == '"v2"'


def test_database_failure_is_fail_closed_without_sql_details(
    migrated_postgres_engine, tmp_path, monkeypatch
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        _bootstrap_and_login(client)

        def fail_session_lookup(_cookie: str):
            raise OperationalError(
                "SELECT password_hash FROM users WHERE secret = 'do-not-leak'",
                {},
                RuntimeError("database unavailable"),
            )

        monkeypatch.setattr(
            client.app.state.services.identity,
            "get_browser_session",
            fail_session_lookup,
        )
        response = client.get("/v1/auth/session")

        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert response.json()["code"] == "dependency_unavailable"
        assert "password_hash" not in response.text
        assert "do-not-leak" not in response.text


def test_transaction_retry_exhaustion_maps_to_retryable_dependency_failure(
    migrated_postgres_engine, tmp_path, monkeypatch
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        _bootstrap_and_login(client)

        def exhaust_retries(_cookie: str):
            raise TransactionRetryExhausted("persistent serialization conflict")

        monkeypatch.setattr(
            client.app.state.services.identity,
            "get_browser_session",
            exhaust_retries,
        )
        response = client.get("/v1/auth/session")

        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert response.json()["code"] == "dependency_unavailable"
        assert "serialization" not in response.text


def test_bootstrap_rejects_untrusted_source_and_forged_forwarding_header(
    migrated_postgres_engine, tmp_path
) -> None:
    identity = make_identity_service(migrated_postgres_engine, tmp_path)
    with TestClient(
        create_app(identity.settings, engine=migrated_postgres_engine),
        base_url="https://nexa.test",
        client=("198.51.100.10", 50000),
    ) as client:
        response = client.post(
            "/v1/internal/admin-bootstrap",
            headers={
                "X-Nexa-Bootstrap-Secret": "b" * 32,
                "Idempotency-Key": "bootstrap-admin-http-02",
                "X-Forwarded-For": "127.0.0.1",
            },
            json={
                "username": "admin@example.test",
                "display_name": "Initial Admin",
                "password": "correct-horse-battery-staple",
            },
        )

        assert response.status_code == 401
        assert response.json()["code"] == "authentication_required"
