"""Index per-submitter age probes used by bounded resumed queue windows."""

from alembic import op

revision = "20260925_0016"
down_revision = "20260925_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_jobs_b13_submitter_age",
        "jobs",
        (
            "tenant_id",
            "submitter_user_id",
            "base_priority",
            "eligible_since",
            "ready_sequence",
            "job_id",
        ),
        postgresql_where="state = 'QUEUED' AND desired_state = 'RUNNING' "
        "AND recovery_intent IS NULL",
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_b13_submitter_age", table_name="jobs")
