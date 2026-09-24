"""B11 recognized-result callback identity for crash-safe reconciliation."""

from sqlalchemy import Column, MetaData, UniqueConstraint

from nexa.infrastructure.persistence import schema_v5
from nexa.infrastructure.persistence.schema_v2 import UUID_TYPE

SCHEMA_GENERATION = schema_v5.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v5.metadata.naming_convention))
for _table in schema_v5.metadata.tables.values():
    _table.to_metadata(metadata)

results = metadata.tables["results"]
results.append_column(Column("completion_callback_id", UUID_TYPE))
results.append_constraint(
    UniqueConstraint("completion_callback_id", name="uq_results_completion_callback")
)
