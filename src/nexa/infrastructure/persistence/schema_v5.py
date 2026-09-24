"""B11 immutable dispatch origin and claim snapshot."""

from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, ForeignKey, MetaData
from sqlalchemy.dialects.postgresql import JSONB

from nexa.infrastructure.persistence import schema_v4
from nexa.infrastructure.persistence.schema_v2 import UUID_TYPE

SCHEMA_GENERATION = schema_v4.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v4.metadata.naming_convention))
for _table in schema_v4.metadata.tables.values():
    _table.to_metadata(metadata)

attempts = metadata.tables["attempts"]
upload_sessions = metadata.tables["upload_sessions"]
attempts.append_column(Column("dispatch_coordinator_epoch", BigInteger))
attempts.append_column(Column("execution_context", JSONB))
attempts.append_column(Column("claimed_at", DateTime(timezone=True)))
attempts.append_column(Column("executor_operation_sequence", BigInteger))
attempts.append_constraint(
    CheckConstraint(
        "dispatch_coordinator_epoch IS NULL OR dispatch_coordinator_epoch >= 1",
        name="dispatch_epoch",
    )
)
upload_sessions.append_column(
    Column(
        "authority_grant_id",
        UUID_TYPE,
        ForeignKey(
            "attempt_authority_grants.grant_id",
            ondelete="RESTRICT",
            name="fk_upload_sessions_authority_grant",
        ),
    )
)
attempts.append_constraint(
    CheckConstraint(
        "executor_operation_sequence IS NULL OR executor_operation_sequence >= 1",
        name="executor_sequence",
    )
)
