"""Persist the end of a tenant or user concurrency block."""

import sqlalchemy as sa
from alembic import op

revision = "20260925_0011"
down_revision = "20260925_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "admission_counters", sa.Column("eligible_resumed_at", sa.DateTime(timezone=True))
    )
    op.execute("""
        CREATE FUNCTION nexa_b13_counter_resume() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE active_limit integer;
        DECLARE owner_id uuid;
        BEGIN
            IF NEW.scope_type NOT IN ('TENANT', 'USER')
               OR NEW.active_attempts = OLD.active_attempts THEN
                RETURN NEW;
            END IF;
            owner_id := split_part(NEW.scope_id, ':', 1)::uuid;
            SELECT CASE NEW.scope_type
                WHEN 'TENANT' THEN tenant_active_limit
                ELSE user_active_limit END
            INTO active_limit
            FROM tenant_policies
            WHERE tenant_id = owner_id AND is_current;
            IF active_limit IS NULL THEN
                RETURN NEW;
            END IF;
            IF OLD.active_attempts >= active_limit
               AND NEW.active_attempts < active_limit THEN
                NEW.eligible_resumed_at := clock_timestamp();
            END IF;
            RETURN NEW;
        END $$;

        CREATE TRIGGER b13_counter_resume
        BEFORE UPDATE OF active_attempts ON admission_counters
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_counter_resume();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER b13_counter_resume ON admission_counters")
    op.execute("DROP FUNCTION nexa_b13_counter_resume()")
    op.drop_column("admission_counters", "eligible_resumed_at")
