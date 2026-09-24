"""Correlate recognized results with their committed completion callback."""

from alembic import op
from sqlalchemy import Column

from nexa.infrastructure.persistence.schema_v2 import UUID_TYPE

revision = "20260923_0007"
down_revision = "20260922_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("results", Column("completion_callback_id", UUID_TYPE))
    op.create_unique_constraint(
        "uq_results_completion_callback", "results", ["completion_callback_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_results_completion_callback", "results", type_="unique")
    op.drop_column("results", "completion_callback_id")
