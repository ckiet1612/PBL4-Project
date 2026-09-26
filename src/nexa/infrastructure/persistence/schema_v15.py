"""B14 one-way checkpoint corruption marks."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
    func,
)

from nexa.infrastructure.persistence import schema_v14
from nexa.infrastructure.persistence.schema_v2 import UTC_TIMESTAMP, UUID_TYPE

SCHEMA_GENERATION = schema_v14.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v14.metadata.naming_convention))
for _table in schema_v14.metadata.tables.values():
    _table.to_metadata(metadata)

# Checkpoints are immutable and CHECK state = 'COMMITTED'; a corrupt mark is a
# separate insert-only row, so the API derives CORRUPT and restore skips it.
checkpoint_corruptions = Table(
    "checkpoint_corruptions",
    metadata,
    Column("checkpoint_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("reason_code", String(64), nullable=False),
    Column("detected_at", UTC_TIMESTAMP, nullable=False, server_default=func.clock_timestamp()),
    ForeignKeyConstraint(
        ["tenant_id", "checkpoint_id"],
        ["checkpoints.tenant_id", "checkpoints.checkpoint_id"],
        name="fk_checkpoint_corruptions_checkpoint",
        ondelete="RESTRICT",
    ),
    CheckConstraint("reason_code ~ '^[A-Z][A-Z0-9_]{0,63}$'", name="reason_code"),
)
