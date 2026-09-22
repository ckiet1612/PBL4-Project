"""B10 durable per-incarnation reconciliation progress."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Index,
    MetaData,
    Sequence,
    String,
)

from nexa.infrastructure.persistence import schema_v3
from nexa.infrastructure.persistence.schema_v2 import UUID_TYPE

SCHEMA_GENERATION = schema_v3.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v3.metadata.naming_convention))
for _table in schema_v3.metadata.tables.values():
    _table.to_metadata(metadata)

worker_incarnations = metadata.tables["worker_incarnations"]
allocations = metadata.tables["allocations"]

allocation_reconciliation_sequence = Sequence("allocation_reconciliation_sequence")
allocations.append_column(
    Column(
        "reconciliation_sequence",
        BigInteger,
        allocation_reconciliation_sequence,
        server_default=allocation_reconciliation_sequence.next_value(),
        nullable=False,
    )
)
Index(
    "ix_allocations_worker_reconciliation",
    allocations.c.worker_id,
    allocations.c.reconciliation_sequence.desc(),
    postgresql_where=allocations.c.state != "RELEASED",
)

worker_incarnations.append_column(Column("reconciliation_snapshot", String(64)))
worker_incarnations.append_column(Column("reconciliation_after", UUID_TYPE))
worker_incarnations.append_column(
    Column("reconciliation_drained", Boolean, nullable=False, server_default="false")
)
worker_incarnations.append_constraint(
    CheckConstraint(
        "reconciliation_snapshot IS NOT NULL OR "
        "(reconciliation_after IS NULL AND reconciliation_drained = false)",
        name="reconciliation_progress",
    )
)

__all__ = [
    "SCHEMA_GENERATION",
    "allocation_reconciliation_sequence",
    "allocations",
    "metadata",
    "worker_incarnations",
]
