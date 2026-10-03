"""Index the cross-tenant admin job list and the fairness ledger period (B18)."""

import sqlalchemy as sa
from alembic import op

revision = "20261001_0023"
down_revision = "20260929_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # adminListJobs without a tenant filter walks this keyset instead of sorting every job.
    op.create_index(
        "ix_jobs_created_keyset", "jobs", [sa.text("created_at DESC"), sa.text("job_id DESC")]
    )
    # adminQueryFairness reads only segments overlapping the window; open segments
    # (ended_at IS NULL) are unbounded ranges and stay reachable.
    op.create_index(
        "ix_allocation_ledger_segments_period",
        "allocation_ledger_segments",
        [sa.text("tstzrange(started_at, ended_at, '[)')")],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index("ix_allocation_ledger_segments_period", table_name="allocation_ledger_segments")
    op.drop_index("ix_jobs_created_keyset", table_name="jobs")
