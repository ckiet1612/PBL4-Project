"""Track queued submitters without scanning every job on a blocked tick."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260925_0010"
down_revision = "20260925_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "queue_submitters",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(("tenant_id",), ("tenants.tenant_id",), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(("user_id",), ("users.user_id",), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", name="pk_queue_submitters"),
    )
    op.create_index(
        "ix_jobs_b13_submitter",
        "jobs",
        ("tenant_id", "submitter_user_id", "base_priority", "ready_sequence", "job_id"),
        postgresql_where="state = 'QUEUED' AND desired_state = 'RUNNING' "
        "AND recovery_intent IS NULL",
    )
    op.execute("""
        INSERT INTO queue_submitters (tenant_id, user_id)
        SELECT DISTINCT tenant_id, submitter_user_id FROM jobs
        WHERE state = 'QUEUED' AND desired_state = 'RUNNING'
          AND recovery_intent IS NULL;

        CREATE FUNCTION nexa_b13_refresh_queue_submitter(p_tenant uuid, p_user uuid)
        RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_tenant::text || ':' || p_user::text, 130014)
            );
            IF EXISTS (
                SELECT 1 FROM jobs
                WHERE tenant_id = p_tenant AND submitter_user_id = p_user
                  AND state = 'QUEUED' AND desired_state = 'RUNNING'
                  AND recovery_intent IS NULL LIMIT 1
            ) THEN
                INSERT INTO queue_submitters (tenant_id, user_id)
                VALUES (p_tenant, p_user) ON CONFLICT DO NOTHING;
            ELSE
                DELETE FROM queue_submitters
                WHERE tenant_id = p_tenant AND user_id = p_user;
            END IF;
        END $$;

        CREATE FUNCTION nexa_b13_job_submitter_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state = 'QUEUED' AND NEW.desired_state = 'RUNNING'
                   AND NEW.recovery_intent IS NULL THEN
                    PERFORM nexa_b13_refresh_queue_submitter(
                        NEW.tenant_id, NEW.submitter_user_id
                    );
                END IF;
            ELSE
                IF OLD.state = NEW.state
                   AND OLD.desired_state = NEW.desired_state
                   AND OLD.recovery_intent IS NOT DISTINCT FROM NEW.recovery_intent
                   AND OLD.tenant_id = NEW.tenant_id
                   AND OLD.submitter_user_id = NEW.submitter_user_id THEN
                    RETURN NEW;
                END IF;
                IF OLD.state = 'QUEUED' AND OLD.desired_state = 'RUNNING'
                   AND OLD.recovery_intent IS NULL THEN
                    PERFORM nexa_b13_refresh_queue_submitter(
                        OLD.tenant_id, OLD.submitter_user_id
                    );
                END IF;
                IF NEW.state = 'QUEUED' AND NEW.desired_state = 'RUNNING'
                   AND NEW.recovery_intent IS NULL THEN
                    PERFORM nexa_b13_refresh_queue_submitter(
                        NEW.tenant_id, NEW.submitter_user_id
                    );
                END IF;
            END IF;
            RETURN NEW;
        END $$;

        CREATE TRIGGER b13_job_submitter_insert AFTER INSERT ON jobs
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_job_submitter_change();
        CREATE TRIGGER b13_job_submitter_update
        AFTER UPDATE OF state, desired_state, recovery_intent,
                        tenant_id, submitter_user_id ON jobs
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_job_submitter_change();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER b13_job_submitter_update ON jobs")
    op.execute("DROP TRIGGER b13_job_submitter_insert ON jobs")
    op.execute("DROP FUNCTION nexa_b13_job_submitter_change()")
    op.execute("DROP FUNCTION nexa_b13_refresh_queue_submitter(uuid, uuid)")
    op.drop_index("ix_jobs_b13_submitter", table_name="jobs")
    op.drop_table("queue_submitters")
