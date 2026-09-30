"""Remediation B14-OBS-01: the artifact store identity this deployment bound."""

from sqlalchemy import CheckConstraint, Column, MetaData, String, Table, func

from nexa.infrastructure.persistence import schema_v18
from nexa.infrastructure.persistence.schema_v2 import UTC_TIMESTAMP, UUID_TYPE

SCHEMA_GENERATION = schema_v18.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v18.metadata.naming_convention))
for _table in schema_v18.metadata.tables.values():
    _table.to_metadata(metadata)

# One insert-only row naming the store whose root records the same identity; only
# inside that store is a missing blob evidence of a lost checkpoint.
artifact_store_identity = Table(
    "artifact_store_identity",
    metadata,
    Column("singleton_key", String(32), primary_key=True),
    Column("store_id", UUID_TYPE, nullable=False),
    Column("bound_at", UTC_TIMESTAMP, nullable=False, server_default=func.clock_timestamp()),
    CheckConstraint("singleton_key = 'artifacts'", name="singleton_key"),
)

__all__ = ["SCHEMA_GENERATION", "artifact_store_identity", "metadata"]
