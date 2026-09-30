"""Record the artifact store identity the deployment bound (B14-OBS-01).

Additive: one insert-only singleton row. The API writes it at startup together with the
identity file at the artifact root; restore treats a missing blob as lost only while the
two match. Downgrade drops the table; the identity file is inert without it.
"""

from alembic import op
from sqlalchemy import CheckConstraint, Column, DateTime, PrimaryKeyConstraint, String, func
from sqlalchemy.dialects.postgresql import UUID

revision = "20260929_0022"
down_revision = "20260928_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifact_store_identity",
        Column("singleton_key", String(32), nullable=False),
        Column("store_id", UUID(as_uuid=True), nullable=False),
        Column(
            "bound_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.clock_timestamp(),
        ),
        PrimaryKeyConstraint("singleton_key", name=op.f("pk_artifact_store_identity")),
        CheckConstraint(
            "singleton_key = 'artifacts'", name=op.f("ck_artifact_store_identity_singleton_key")
        ),
    )
    op.execute("""
        CREATE TRIGGER trg_artifact_store_identity_immutable
        BEFORE UPDATE OR DELETE ON artifact_store_identity
        FOR EACH ROW EXECUTE FUNCTION nexa_reject_immutable_mutation()
    """)


def downgrade() -> None:
    op.drop_table("artifact_store_identity")
