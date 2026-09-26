"""Record one-way checkpoint corruption marks for restore selection."""

from alembic import op
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    PrimaryKeyConstraint,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

revision = "20260926_0018"
down_revision = "20260925_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # checkpoints stays immutable with CHECK state = 'COMMITTED'; CORRUPT is
    # derived from an insert-only mark that can never be removed or changed.
    op.create_table(
        "checkpoint_corruptions",
        Column("checkpoint_id", UUID(as_uuid=True), nullable=False),
        Column("tenant_id", UUID(as_uuid=True), nullable=False),
        Column("reason_code", String(64), nullable=False),
        Column(
            "detected_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.clock_timestamp(),
        ),
        PrimaryKeyConstraint("checkpoint_id", name=op.f("pk_checkpoint_corruptions")),
        ForeignKeyConstraint(
            ["tenant_id", "checkpoint_id"],
            ["checkpoints.tenant_id", "checkpoints.checkpoint_id"],
            name=op.f("fk_checkpoint_corruptions_checkpoint"),
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "reason_code ~ '^[A-Z][A-Z0-9_]{0,63}$'",
            name=op.f("ck_checkpoint_corruptions_reason_code"),
        ),
    )
    op.execute("""
        CREATE TRIGGER trg_checkpoint_corruptions_immutable
        BEFORE UPDATE OR DELETE ON checkpoint_corruptions
        FOR EACH ROW EXECUTE FUNCTION nexa_reject_immutable_mutation()
    """)


def downgrade() -> None:
    op.drop_table("checkpoint_corruptions")
