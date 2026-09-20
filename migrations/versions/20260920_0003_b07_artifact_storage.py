"""Add B07 durable tenant artifact-storage counters.

Revision ID: 20260920_0003
Revises: 20260920_0002
"""

from alembic import op
from sqlalchemy import Column, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from nexa.infrastructure.persistence import schema_v2
from nexa.infrastructure.persistence.schema_v3 import artifact_storage_counters

revision = "20260920_0003"
down_revision = "20260920_0002"
branch_labels = None
depends_on = None


def _sql(ddl: object) -> str:
    return str(ddl.compile(dialect=postgresql.dialect()))


def upgrade() -> None:
    op.add_column("upload_sessions", Column("idempotency_id", schema_v2.UUID_TYPE))
    op.create_foreign_key(
        "fk_upload_sessions_idempotency",
        "upload_sessions",
        "idempotency_records",
        ["idempotency_id"],
        ["idempotency_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_upload_sessions_idempotency", "upload_sessions", ["idempotency_id"]
    )
    op.execute(_sql(CreateTable(artifact_storage_counters)))
    op.execute(
        text(
            "INSERT INTO artifact_storage_counters "
            "(tenant_id, committed_bytes, reserved_bytes) "
            "SELECT t.tenant_id, "
            "COALESCE(committed.committed_bytes, 0), "
            "COALESCE(reserved.reserved_bytes, 0) "
            "FROM tenants AS t "
            "LEFT JOIN ("
            "  SELECT tenant_id, SUM(size_bytes) AS committed_bytes "
            "  FROM artifacts WHERE state = 'COMMITTED' GROUP BY tenant_id"
            ") AS committed ON committed.tenant_id = t.tenant_id "
            "LEFT JOIN ("
            "  SELECT tenant_id, SUM(expected_size_bytes) AS reserved_bytes "
            "  FROM upload_sessions WHERE state = 'ACTIVE' GROUP BY tenant_id"
            ") AS reserved ON reserved.tenant_id = t.tenant_id "
            "ON CONFLICT (tenant_id) DO NOTHING"
        )
    )


def downgrade() -> None:
    op.drop_table("artifact_storage_counters")
    op.drop_constraint("uq_upload_sessions_idempotency", "upload_sessions", type_="unique")
    op.drop_constraint("fk_upload_sessions_idempotency", "upload_sessions", type_="foreignkey")
    op.drop_column("upload_sessions", "idempotency_id")
