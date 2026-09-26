"""Current persistence metadata assembled from immutable schema generations."""

from sqlalchemy import MetaData

from nexa.infrastructure.persistence import schema_v15

SCHEMA_GENERATION = schema_v15.SCHEMA_GENERATION

metadata = MetaData(naming_convention=dict(schema_v15.metadata.naming_convention))
for _table in schema_v15.metadata.tables.values():
    _table.to_metadata(metadata)

globals().update(metadata.tables)

__all__ = ["SCHEMA_GENERATION", "metadata", *metadata.tables]
