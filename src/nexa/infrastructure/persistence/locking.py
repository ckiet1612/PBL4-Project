from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Column, Table, and_, func, select, update


def _canonical_id_key(value: object) -> bytes:
    if isinstance(value, UUID):
        return value.bytes
    return str(value).encode("utf-8")


def lock_rows(
    executor: Any,
    table: Table,
    id_column: Column[Any],
    ids: Iterable[object],
) -> list[Any]:
    ordered_ids = sorted(set(ids), key=_canonical_id_key)
    if not ordered_ids:
        return []
    statement = (
        select(table).where(id_column.in_(ordered_ids)).order_by(id_column).with_for_update()
    )
    return list(executor.execute(statement).mappings().all())


def compare_and_swap(
    executor: Any,
    table: Table,
    key_values: Mapping[str, object],
    *,
    expected_version: int,
    values: Mapping[str, object],
) -> bool:
    if expected_version < 1:
        raise ValueError("expected_version must be positive")
    if "version" in values:
        raise ValueError("compare_and_swap owns the version increment")
    predicates = [table.c[name] == value for name, value in key_values.items()]
    statement = (
        update(table)
        .where(and_(*predicates), table.c.version == expected_version)
        .values(**values, version=expected_version + 1)
    )
    result = executor.execute(statement)
    return result.rowcount == 1


def transaction_timestamp(executor: Any) -> datetime:
    return executor.execute(select(func.transaction_timestamp())).scalar_one()


def clock_timestamp(executor: Any) -> datetime:
    return executor.execute(select(func.clock_timestamp())).scalar_one()
