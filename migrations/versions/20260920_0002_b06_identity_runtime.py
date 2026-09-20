"""Add durable B06 identity runtime state.

Revision ID: 20260920_0002
Revises: 20260919_0001
"""

from alembic import context, op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from nexa.infrastructure.persistence.schema_v2 import auth_control, login_rate_limits, metadata

revision = "20260920_0002"
down_revision = "20260919_0001"
branch_labels = None
depends_on = None


def _sql(ddl: object) -> str:
    return str(ddl.compile(dialect=postgresql.dialect()))


def _normalize_username(value: str) -> str:
    return value.strip().casefold().lower()


def _normalize_existing_usernames() -> None:
    if context.is_offline_mode():
        op.execute(
            """
            DO $b06_username_normalization$
            BEGIN
              IF EXISTS (
                SELECT lower(btrim(username))
                FROM users
                GROUP BY lower(btrim(username))
                HAVING count(*) > 1
              ) THEN
                RAISE EXCEPTION 'B06 username normalization collision detected';
              END IF;
              IF EXISTS (
                SELECT 1
                FROM users
                WHERE length(lower(btrim(username))) NOT BETWEEN 3 AND 254
              ) THEN
                RAISE EXCEPTION 'B06 username normalization produced an invalid username';
              END IF;
              UPDATE users
              SET username = lower(btrim(username)), updated_at = CURRENT_TIMESTAMP
              WHERE username IS DISTINCT FROM lower(btrim(username));
            END
            $b06_username_normalization$;
            """
        )
        return
    bind = op.get_bind()
    rows = bind.execute(text("SELECT user_id, username FROM users ORDER BY user_id")).all()
    normalized_to_user: dict[str, object] = {}
    updates: list[tuple[object, str]] = []
    for row in rows:
        normalized = _normalize_username(row.username)
        if not 3 <= len(normalized) <= 254:
            raise RuntimeError("B06 username normalization produced an invalid username")
        previous = normalized_to_user.setdefault(normalized, row.user_id)
        if previous != row.user_id:
            raise RuntimeError("B06 username casefold collision detected")
        if normalized != row.username:
            updates.append((row.user_id, normalized))
    for user_id, normalized in updates:
        bind.execute(
            text(
                "UPDATE users SET username = :username, updated_at = CURRENT_TIMESTAMP "
                "WHERE user_id = :user_id"
            ),
            {"user_id": user_id, "username": normalized},
        )


def _backfill_tenant_policies() -> None:
    op.execute(
        "INSERT INTO tenant_policies ("
        "tenant_id, version, weight, cpu_limit_millis, memory_limit_bytes, gpu_limit, "
        "outstanding_limit, user_outstanding_limit, tenant_active_limit, user_active_limit, "
        "tenant_rate_per_second, tenant_rate_burst, user_rate_per_second, user_rate_burst, "
        "is_current"
        ") "
        "SELECT t.tenant_id, 1, '0:1:0', 0, 0, 0, 2000, 2000, 2, 1, "
        "'0:5:0', '0:20:0', '0:2:0', '0:10:0', true "
        "FROM tenants AS t "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM tenant_policies AS p WHERE p.tenant_id = t.tenant_id"
        ")"
    )


def upgrade() -> None:
    op.execute(_sql(CreateTable(auth_control)))
    op.execute(_sql(CreateTable(login_rate_limits)))
    for index in sorted(login_rate_limits.indexes, key=lambda item: item.name or ""):
        op.execute(_sql(CreateIndex(index)))
    worker_credentials = metadata.tables["worker_credentials"]
    current_index = next(
        index
        for index in worker_credentials.indexes
        if index.name == "uq_worker_credentials_current"
    )
    op.execute(
        "WITH ranked AS ("
        "SELECT credential_id, "
        "row_number() OVER ("
        "PARTITION BY worker_id ORDER BY created_at DESC, credential_id DESC"
        ") AS current_rank "
        "FROM worker_credentials WHERE revoked_at IS NULL"
        ") "
        "UPDATE worker_credentials AS credential "
        "SET revoked_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
        "FROM ranked "
        "WHERE credential.credential_id = ranked.credential_id "
        "AND ranked.current_rank > 1"
    )
    op.execute(_sql(CreateIndex(current_index)))
    op.execute("INSERT INTO auth_control (singleton_key) VALUES ('auth')")
    op.execute(
        "INSERT INTO policy_versions "
        "(policy_version, global_outstanding_limit, operational_mode, is_current) "
        "SELECT 1, 100000, 'NORMAL', true "
        "WHERE NOT EXISTS (SELECT 1 FROM policy_versions WHERE is_current)"
    )
    _normalize_existing_usernames()
    _backfill_tenant_policies()
    op.execute("UPDATE nexa_schema_metadata SET schema_generation = 2 WHERE singleton_key = 'nexa'")


def downgrade() -> None:
    op.drop_index("uq_worker_credentials_current", table_name="worker_credentials")
    op.drop_table("login_rate_limits")
    op.drop_table("auth_control")
    op.execute("UPDATE nexa_schema_metadata SET schema_generation = 1 WHERE singleton_key = 'nexa'")
