"""Persist the inputs used to reconcile queued eligibility per tenant."""

from alembic import op
from sqlalchemy import Column, String

revision = "20260925_0009"
down_revision = "20260925_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fairness_ledgers", Column("eligibility_signature", String(64)))


def downgrade() -> None:
    op.drop_column("fairness_ledgers", "eligibility_signature")
