"""B13 concurrency-resume boundary for eligibility age."""

from sqlalchemy import Column, DateTime, MetaData

from nexa.infrastructure.persistence import schema_v9

SCHEMA_GENERATION = schema_v9.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v9.metadata.naming_convention))
for _table in schema_v9.metadata.tables.values():
    _table.to_metadata(metadata)

admission_counters = metadata.tables["admission_counters"]
admission_counters.append_column(Column("eligible_resumed_at", DateTime(timezone=True)))
