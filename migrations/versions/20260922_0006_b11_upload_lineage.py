"""Bind upload sessions to the authority grant that authorized them."""

from alembic import op
from sqlalchemy import Column

from nexa.infrastructure.persistence.schema_v2 import UUID_TYPE

revision = "20260922_0006"
down_revision = "20260922_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("upload_sessions", Column("authority_grant_id", UUID_TYPE))
    op.create_foreign_key(
        "fk_upload_sessions_authority_grant",
        "upload_sessions",
        "attempt_authority_grants",
        ["authority_grant_id"],
        ["grant_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_upload_sessions_authority_grant", "upload_sessions", type_="foreignkey")
    op.drop_column("upload_sessions", "authority_grant_id")
