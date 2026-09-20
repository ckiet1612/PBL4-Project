import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from nexa.application.errors import ApplicationError
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema import idempotency_records

_KEY_PATTERN = re.compile(r"^[!-~]{16,128}$")
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    body: dict[str, Any] | None
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class IdempotencyOutcome:
    record_id: UUID
    replay: StoredResponse | None


def _scope_predicates(
    *, context: str, principal_id: str, operation_id: str, key: str
) -> tuple[Any, ...]:
    return (
        idempotency_records.c.context == context,
        idempotency_records.c.principal_id == principal_id,
        idempotency_records.c.operation_id == operation_id,
        idempotency_records.c.idempotency_key == key,
    )


def begin_idempotency(
    session: Session,
    *,
    context: str,
    principal_id: str,
    operation_id: str,
    key: str,
    request_hash: str,
    expires_at: datetime,
    pending_wait_milliseconds: int,
) -> IdempotencyOutcome:
    if not _KEY_PATTERN.fullmatch(key):
        raise ApplicationError(
            code="validation_failed",
            status=422,
            message="Idempotency-Key must contain 16 to 128 visible ASCII characters",
        )
    if not _HASH_PATTERN.fullmatch(request_hash):
        raise ValueError("request_hash must be an RFC 8785 sha256 value")
    if not 0 <= pending_wait_milliseconds <= 30_000:
        raise ValueError("pending_wait_milliseconds is outside the contract range")

    record_id = new_uuid7()
    if pending_wait_milliseconds:
        session.execute(text(f"SET LOCAL lock_timeout = '{pending_wait_milliseconds}ms'"))
    statement = (
        insert(idempotency_records)
        .values(
            idempotency_id=record_id,
            context=context,
            principal_id=principal_id,
            operation_id=operation_id,
            idempotency_key=key,
            request_hash=request_hash,
            state="PENDING",
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(
            index_elements=[
                idempotency_records.c.context,
                idempotency_records.c.principal_id,
                idempotency_records.c.operation_id,
                idempotency_records.c.idempotency_key,
            ]
        )
        .returning(idempotency_records.c.idempotency_id)
    )
    try:
        inserted = session.execute(statement).scalar_one_or_none()
    except OperationalError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if sqlstate == "55P03":
            raise ApplicationError(
                code="idempotency_in_progress",
                status=409,
                message="An identical request is still in progress",
                retry_after=1,
            ) from exc
        raise
    if inserted is not None:
        return IdempotencyOutcome(record_id=inserted, replay=None)

    row = (
        session.execute(
            select(idempotency_records)
            .where(
                *_scope_predicates(
                    context=context,
                    principal_id=principal_id,
                    operation_id=operation_id,
                    key=key,
                )
            )
            .with_for_update()
        )
        .mappings()
        .one()
    )
    if row["request_hash"] != request_hash:
        raise ApplicationError(
            code="idempotency_conflict",
            status=409,
            message="Idempotency-Key was already used with a different request",
        )
    if row["state"] != "COMPLETED":
        raise ApplicationError(
            code="idempotency_in_progress",
            status=409,
            message="An identical request is still in progress",
            retry_after=1,
        )
    response_headers = dict(row["response_headers"] or {})
    if row["one_time_secret"]:
        raise ApplicationError(
            code="one_time_secret_unavailable",
            status=409,
            message="The one-time secret was already committed and cannot be replayed",
            location=response_headers.get("Location"),
        )
    return IdempotencyOutcome(
        record_id=row["idempotency_id"],
        replay=StoredResponse(
            status=int(row["response_status"]),
            body=dict(row["response_body"]) if row["response_body"] is not None else None,
            headers=response_headers,
        ),
    )


def complete_idempotency(
    session: Session,
    record_id: UUID,
    *,
    status: int,
    body: dict[str, Any] | None,
    headers: dict[str, str],
    resource_id: UUID | None = None,
    one_time_secret: bool = False,
) -> None:
    if not 100 <= status <= 599:
        raise ValueError("response status is outside the HTTP range")
    result = session.execute(
        update(idempotency_records)
        .where(
            idempotency_records.c.idempotency_id == record_id,
            idempotency_records.c.state == "PENDING",
        )
        .values(
            state="COMPLETED",
            response_status=status,
            response_body=body,
            response_headers=headers,
            resource_id=resource_id,
            one_time_secret=one_time_secret,
        )
    )
    if result.rowcount != 1:
        raise RuntimeError("Idempotency record is not pending")
