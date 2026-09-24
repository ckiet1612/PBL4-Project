"""Persist immutable B11 dispatch and claim facts.

Revision ID: 20260922_0005
Revises: 20260921_0004
"""

from alembic import op
from sqlalchemy import BigInteger, Column, DateTime
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260922_0005"
down_revision = "20260921_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("attempts", Column("dispatch_coordinator_epoch", BigInteger))
    op.add_column("attempts", Column("execution_context", JSONB))
    op.add_column("attempts", Column("claimed_at", DateTime(timezone=True)))
    op.add_column("attempts", Column("executor_operation_sequence", BigInteger))
    op.create_check_constraint(
        "ck_attempts_dispatch_epoch",
        "attempts",
        "dispatch_coordinator_epoch IS NULL OR dispatch_coordinator_epoch >= 1",
    )
    op.create_check_constraint(
        "ck_attempts_executor_sequence",
        "attempts",
        "executor_operation_sequence IS NULL OR executor_operation_sequence >= 1",
    )
    op.execute("""
        CREATE FUNCTION nexa_b11_immutable_claim() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (OLD.dispatch_coordinator_epoch IS NOT NULL AND
                NEW.dispatch_coordinator_epoch IS DISTINCT FROM OLD.dispatch_coordinator_epoch)
            OR (OLD.execution_context IS NOT NULL AND
                NEW.execution_context IS DISTINCT FROM OLD.execution_context)
            OR (OLD.claimed_at IS NOT NULL AND NEW.claimed_at IS DISTINCT FROM OLD.claimed_at)
            OR (OLD.executor_operation_sequence IS NOT NULL AND
                NEW.executor_operation_sequence IS DISTINCT FROM OLD.executor_operation_sequence)
            THEN
                RAISE EXCEPTION 'immutable dispatch or claim fact' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER b11_immutable_claim BEFORE UPDATE ON attempts
        FOR EACH ROW EXECUTE FUNCTION nexa_b11_immutable_claim();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER b11_immutable_claim ON attempts")
    op.execute("DROP FUNCTION nexa_b11_immutable_claim()")
    op.drop_constraint("ck_attempts_executor_sequence", "attempts", type_="check")
    op.drop_constraint("ck_attempts_dispatch_epoch", "attempts", type_="check")
    for name in (
        "executor_operation_sequence",
        "claimed_at",
        "execution_context",
        "dispatch_coordinator_epoch",
    ):
        op.drop_column("attempts", name)
