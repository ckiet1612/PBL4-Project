"""B18 admin reads: the cross-tenant job keyset and the fairness ledger period index."""

from sqlalchemy import Index, MetaData, func, literal_column

from nexa.infrastructure.persistence import schema_v19

SCHEMA_GENERATION = schema_v19.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v19.metadata.naming_convention))
for _table in schema_v19.metadata.tables.values():
    _table.to_metadata(metadata)

jobs = metadata.tables["jobs"]
Index("ix_jobs_created_keyset", jobs.c.created_at.desc(), jobs.c.job_id.desc())

allocation_ledger_segments = metadata.tables["allocation_ledger_segments"]
# Matches the fairness query's overlap predicate; an open segment is an unbounded range.
Index(
    "ix_allocation_ledger_segments_period",
    func.tstzrange(
        allocation_ledger_segments.c.started_at,
        allocation_ledger_segments.c.ended_at,
        literal_column("'[)'"),
    ),
    postgresql_using="gist",
)

__all__ = ["SCHEMA_GENERATION", "metadata"]
