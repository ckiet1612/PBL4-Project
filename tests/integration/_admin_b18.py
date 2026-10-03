"""Shared B18 admin read helpers: an admin browser session and a plain member session."""

from contextlib import contextmanager

from sqlalchemy import select

from nexa.infrastructure.persistence import schema as s
from tests.api.test_http_contract import _bootstrap_and_login, _client

MEMBER_PASSWORD = "b18-member-password-value"


@contextmanager
def admin_session(engine, tmp_path):
    """Yield (client, write_headers) for the bootstrap SYSTEM_ADMIN."""
    with _client(engine, tmp_path) as client:
        login, _ = _bootstrap_and_login(client)
        yield client, {"Origin": "https://nexa.test", "X-CSRF-Token": login["csrf_token"]}


@contextmanager
def member_session(
    engine, tmp_path, admin_client, write_headers, *, label="b18-member", system_roles=()
):
    """Create a user (no system roles by default) and yield a logged-in client for it."""
    created = admin_client.post(
        "/v1/admin/users",
        headers={**write_headers, "Idempotency-Key": f"{label}-user-create-key"},
        json={
            "username": f"{label}@example.test",
            "display_name": label,
            "password": MEMBER_PASSWORD,
            "system_roles": list(system_roles),
        },
    )
    assert created.status_code == 201, created.text
    with _client(engine, tmp_path) as client:
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={"username": f"{label}@example.test", "password": MEMBER_PASSWORD},
        )
        assert login.status_code == 200, login.text
        yield client


def audit_rows(engine, action):
    with engine.connect() as connection:
        return (
            connection.execute(
                select(s.audit_records)
                .where(s.audit_records.c.action == action)
                .order_by(s.audit_records.c.created_at)
            )
            .mappings()
            .all()
        )
