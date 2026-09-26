"""B13 durable eligibility reconciliation marker."""

from sqlalchemy import Column, MetaData, String

from nexa.infrastructure.persistence import schema_v7

SCHEMA_GENERATION = schema_v7.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v7.metadata.naming_convention))
for _table in schema_v7.metadata.tables.values():
    _table.to_metadata(metadata)

fairness_ledgers = metadata.tables["fairness_ledgers"]
fairness_ledgers.append_column(Column("eligibility_signature", String(64)))
