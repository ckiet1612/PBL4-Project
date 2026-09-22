"""Persist B10 reconciliation traversal progress.

Revision ID: 20260921_0004
Revises: 20260920_0003
"""

from alembic import op
from sqlalchemy import BigInteger, Boolean, Column, String, text

from nexa.infrastructure.persistence.schema_v2 import UUID_TYPE

revision = "20260921_0004"
down_revision = "20260920_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE allocation_reconciliation_sequence")
    op.add_column(
        "allocations",
        Column(
            "reconciliation_sequence",
            BigInteger,
            nullable=False,
            server_default=text("nextval('allocation_reconciliation_sequence')"),
        ),
    )
    op.create_index(
        "ix_allocations_worker_reconciliation",
        "allocations",
        ["worker_id", "reconciliation_sequence"],
        unique=False,
        postgresql_where=text("state <> 'RELEASED'"),
    )
    op.add_column("worker_incarnations", Column("reconciliation_snapshot", String(64)))
    op.add_column("worker_incarnations", Column("reconciliation_after", UUID_TYPE))
    op.add_column(
        "worker_incarnations",
        Column("reconciliation_drained", Boolean, nullable=False, server_default=text("false")),
    )
    op.create_check_constraint(
        "ck_worker_incarnations_reconciliation_progress",
        "worker_incarnations",
        "reconciliation_snapshot IS NOT NULL OR "
        "(reconciliation_after IS NULL AND reconciliation_drained = false)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_worker_incarnations_reconciliation_progress", "worker_incarnations", type_="check"
    )
    op.drop_column("worker_incarnations", "reconciliation_drained")
    op.drop_column("worker_incarnations", "reconciliation_after")
    op.drop_column("worker_incarnations", "reconciliation_snapshot")
    op.drop_index("ix_allocations_worker_reconciliation", table_name="allocations")
    op.drop_column("allocations", "reconciliation_sequence")
    op.execute("DROP SEQUENCE allocation_reconciliation_sequence")
