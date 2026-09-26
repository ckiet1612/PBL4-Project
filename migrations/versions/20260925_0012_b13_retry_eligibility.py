"""Start eligible age at the durable retry-ready boundary."""

from alembic import op

revision = "20260925_0012"
down_revision = "20260925_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE jobs SET eligible_since = retry_ready_at
        WHERE state = 'QUEUED' AND eligible_since IS NOT NULL
          AND retry_ready_at IS NOT NULL AND eligible_since < retry_ready_at;

        CREATE FUNCTION nexa_b13_retry_eligibility() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE boundary timestamptz;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state = 'QUEUED' AND NEW.eligible_since IS NOT NULL
                   AND NEW.retry_ready_at IS NOT NULL THEN
                    NEW.eligible_since := greatest(NEW.eligible_since, NEW.retry_ready_at);
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.state <> 'QUEUED' AND NEW.state = 'QUEUED' THEN
                boundary := clock_timestamp();
                NEW.eligible_since := greatest(boundary, NEW.retry_ready_at);
            ELSIF NEW.state = 'QUEUED'
                  AND OLD.retry_ready_at IS DISTINCT FROM NEW.retry_ready_at
                  AND NEW.eligible_since IS NOT NULL THEN
                IF NEW.retry_ready_at IS NOT NULL THEN
                    NEW.eligible_since := greatest(NEW.eligible_since, NEW.retry_ready_at);
                ELSIF OLD.retry_ready_at > clock_timestamp() THEN
                    NEW.eligible_since := clock_timestamp();
                END IF;
            END IF;
            RETURN NEW;
        END $$;

        CREATE TRIGGER b13_retry_eligibility_insert
        BEFORE INSERT ON jobs FOR EACH ROW
        EXECUTE FUNCTION nexa_b13_retry_eligibility();
        CREATE TRIGGER b13_retry_eligibility_update
        BEFORE UPDATE OF state, retry_ready_at ON jobs FOR EACH ROW
        EXECUTE FUNCTION nexa_b13_retry_eligibility();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER b13_retry_eligibility_update ON jobs")
    op.execute("DROP TRIGGER b13_retry_eligibility_insert ON jobs")
    op.execute("DROP FUNCTION nexa_b13_retry_eligibility()")
