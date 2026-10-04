"""Finite hyperparameter sweep: parent idempotency plus one normal submit per child.

The parent transaction validates the whole request, stores the canonical request
hash and the ordered expansion, and completes the `submitSweep` record. Each child
then runs in its own transaction through the same admission and Job creation as
`submitJob`, under a deterministic internal key, and commits its immutable
ACCEPTED/REJECTED outcome with the parent counts. A crash or an abort leaves the
unfinished indexes for a replay of the same key and hash; completed indexes are
never admitted twice (workloads-checkpoints.md, "Partial acceptance and replay").

Request-level violations (shape, names, values, product) are 422 and persist
nothing (B16-R04). Admission failures of one child are its REJECTED snapshot.
Authorization loss, an unavailable dependency, WRITE_FROZEN and an exhausted
transaction retry abort the request instead and stay resumable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import null, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from nexa.api.schemas import SweepSubmitRequest, TrainingJobSpec, TrainingParameters
from nexa.application import sweep_expansion
from nexa.application.errors import ApplicationError
from nexa.application.idempotency import begin_idempotency, complete_idempotency
from nexa.application.job_service import JobOperationResult, JobService
from nexa.application.json_codec import jcs_request_hash, json_wire_value
from nexa.domain.identity import Principal
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import transaction_timestamp
from nexa.infrastructure.persistence.schema import (
    idempotency_records,
    jobs,
    policy_versions,
    sweep_children,
    sweep_parents,
)
from nexa.infrastructure.persistence.schema_v17 import SWEEP_PARENT_OPERATION
from nexa.infrastructure.persistence.transactions import run_transaction
from nexa.infrastructure.security import CursorError
from nexa.observability import metrics_api

# Codes that end the request instead of becoming a child outcome; a replay resumes.
_ABORTING_CODES = frozenset(
    {"permission_denied", "dependency_unavailable", "idempotency_in_progress"}
)


def _invalid(message: str) -> ApplicationError:
    return ApplicationError(code="validation_failed", status=422, message=message)


def _write_frozen() -> ApplicationError:
    return ApplicationError(
        code="state_conflict",
        status=409,
        message="Writes are frozen in the current operational mode",
    )


class SweepService:
    def __init__(self, jobs_service: JobService) -> None:
        self.jobs = jobs_service
        self.session_factory = jobs_service.session_factory
        self.settings = jobs_service.settings

    def _retention(self) -> timedelta:
        return timedelta(days=self.settings.idempotency_terminal_retention_days)

    @staticmethod
    def _mode(session: Session, *, share: bool) -> str:
        statement = select(policy_versions.c.operational_mode).where(
            policy_versions.c.is_current.is_(True)
        )
        if share:
            statement = statement.with_for_update(read=True)
        return session.execute(statement).scalar_one()

    @staticmethod
    def expansion(template: dict[str, Any], decoded: dict[str, Any]) -> list[dict[str, Any]]:
        """Validate every dimension value against the child template; return the expansion."""
        base = decoded["base_spec"]["parameters"]
        dimensions = [(item["name"], item["values"]) for item in decoded["dimensions"]]
        declarations = template.get("parameter_schema") or []
        declared = {
            declaration.get("name") for declaration in declarations if isinstance(declaration, dict)
        }
        if any(name not in declared for name, _values in dimensions):
            raise _invalid("Sweep dimension names must exist in the template parameter schema")
        try:
            for name, values in dimensions:
                for value in sweep_expansion.canonical_values(values):
                    candidate = TrainingParameters.model_validate({**base, name: value})
                    JobService._validate_parameter_schema(
                        template, candidate.model_dump(mode="json")
                    )
            children = sweep_expansion.expand(base, dimensions)
        except ValidationError:
            raise _invalid("A sweep value does not match the child template parameters") from None
        except sweep_expansion.SweepExpansionError as exc:
            raise _invalid(str(exc)) from None
        return [
            {
                "parameters": sweep_expansion.wire_parameters(child),
                "parameter_hash": sweep_expansion.parameter_hash(child),
            }
            for child in children
        ]

    def submit(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        request: SweepSubmitRequest,
        decoded: dict[str, Any],
        idempotency_key: str,
        request_hash: str,
        request_id: str,
    ) -> JobOperationResult:
        base_spec = json_wire_value(decoded["base_spec"])
        base_spec_checksum = jcs_request_hash(request.base_spec.model_dump(mode="json"))
        sweep_id = new_uuid7()
        reading = self.jobs.storage_reading()
        replayed: list[bool] = []

        def open_parent(session: Session) -> tuple[UUID, UUID, list[int]]:
            replayed.clear()
            live = self.jobs._authorize(session, principal, tenant_id, write=True)
            now = transaction_timestamp(session)
            outcome = begin_idempotency(
                session,
                context=str(tenant_id),
                principal_id=str(live.user_id),
                operation_id=SWEEP_PARENT_OPERATION,
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=now + self._retention(),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if outcome.replay is not None:
                replayed.append(True)
                existing = UUID(str((outcome.replay.body or {})["sweep_id"]))
                return existing, outcome.record_id, self._unfinished(session, existing)
            template = JobService.enabled_template(
                session, request.base_spec.template_id, request.base_spec.template_version
            )
            expansion = self.expansion(template, decoded)
            # FOR SHARE holds the mode until the parent commits (B16-R28).
            if self._mode(session, share=True) in {"ADMISSION_OFF", "WRITE_FROZEN"}:
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="New sweep submissions are disabled in the current operational mode",
                )
            self.jobs.enforce_storage(session, tenant_id, reading)
            session.execute(
                insert(sweep_parents).values(
                    sweep_id=sweep_id,
                    tenant_id=tenant_id,
                    submitter_user_id=live.user_id,
                    base_spec_checksum=base_spec_checksum,
                    idempotency_context=str(outcome.record_id),
                    child_count=len(expansion),
                    request_hash=request_hash,
                    base_spec=base_spec,
                    expansion=expansion,
                    created_at=now,
                    updated_at=now,
                )
            )
            complete_idempotency(
                session,
                outcome.record_id,
                status=207,
                body={"sweep_id": str(sweep_id)},
                headers={"Location": f"/v1/sweeps/{sweep_id}"},
                resource_id=sweep_id,
            )
            return sweep_id, outcome.record_id, list(range(len(expansion)))

        try:
            opened, record_id, unfinished = run_transaction(self.session_factory, open_parent)
        except ApplicationError as exc:
            metrics_api.admission("sweep", "rejected", exc.code)
            raise
        metrics_api.admission("sweep", "replayed" if replayed else "accepted")
        for child_index in unfinished:
            self._admit_child(
                principal,
                tenant_id=tenant_id,
                sweep_id=opened,
                record_id=record_id,
                child_index=child_index,
                request_id=request_id,
            )
        body = run_transaction(
            self.session_factory,
            lambda session: self._view(session, tenant_id, opened, page_size=100, after=-1),
        )
        return JobOperationResult(
            status=207, body=body, headers={"Location": f"/v1/sweeps/{opened}"}
        )

    @staticmethod
    def _unfinished(session: Session, sweep_id: UUID) -> list[int]:
        child_count = session.execute(
            select(sweep_parents.c.child_count).where(sweep_parents.c.sweep_id == sweep_id)
        ).scalar_one()
        done = set(
            session.execute(
                select(sweep_children.c.child_index).where(sweep_children.c.sweep_id == sweep_id)
            ).scalars()
        )
        return [index for index in range(child_count) if index not in done]

    def _admit_child(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        sweep_id: UUID,
        record_id: UUID,
        child_index: int,
        request_id: str,
    ) -> None:
        def operation(session: Session) -> None:
            live = self.jobs._authorize(session, principal, tenant_id, write=True)
            # The parent record serializes concurrent replays of the same sweep.
            if (
                session.execute(
                    select(idempotency_records.c.idempotency_id)
                    .where(idempotency_records.c.idempotency_id == record_id)
                    .with_for_update()
                ).scalar_one_or_none()
                is None
            ):
                raise ApplicationError(
                    code="dependency_unavailable",
                    status=503,
                    message="The sweep request record is unavailable",
                    retry_after=1,
                )
            if (
                session.execute(
                    select(sweep_children.c.child_index).where(
                        sweep_children.c.sweep_id == sweep_id,
                        sweep_children.c.child_index == child_index,
                    )
                ).scalar_one_or_none()
                is not None
            ):
                return
            # Unlocked pre-check: the child's job admission rechecks the mode under
            # FOR UPDATE, and a FOR SHARE here would upgrade to it and could deadlock.
            if self._mode(session, share=False) == "WRITE_FROZEN":
                raise _write_frozen()
            parent = (
                session.execute(
                    select(sweep_parents.c.base_spec, sweep_parents.c.expansion).where(
                        sweep_parents.c.sweep_id == sweep_id,
                        sweep_parents.c.tenant_id == tenant_id,
                    )
                )
                .mappings()
                .one()
            )
            entry = parent["expansion"][child_index]
            child_spec = {**parent["base_spec"], "parameters": entry["parameters"]}
            job_id, error = self._submit_child(
                session,
                live,
                tenant_id=tenant_id,
                sweep_id=sweep_id,
                child_index=child_index,
                parameter_hash=entry["parameter_hash"],
                child_spec=child_spec,
                request_id=request_id,
            )
            session.execute(
                insert(sweep_children).values(
                    sweep_id=sweep_id,
                    tenant_id=tenant_id,
                    child_index=child_index,
                    parameter_hash=entry["parameter_hash"],
                    job_id=job_id,
                    # SQL NULL, not a JSON null, keeps exactly one outcome column set.
                    rejected_error=null() if error is None else error,
                    created_at=transaction_timestamp(session),
                )
            )
            counts = (
                {"accepted_count": sweep_parents.c.accepted_count + 1}
                if job_id is not None
                else {"rejected_count": sweep_parents.c.rejected_count + 1}
            )
            session.execute(
                update(sweep_parents)
                .where(sweep_parents.c.sweep_id == sweep_id)
                .values(**counts, updated_at=transaction_timestamp(session))
            )

        run_transaction(self.session_factory, operation)

    def _submit_child(
        self,
        session: Session,
        live: Principal,
        *,
        tenant_id: UUID,
        sweep_id: UUID,
        child_index: int,
        parameter_hash: str,
        child_spec: dict[str, Any],
        request_id: str,
    ) -> tuple[UUID | None, dict[str, str] | None]:
        """Run the submitJob use case for one child inside a savepoint."""
        try:
            spec = TrainingJobSpec.model_validate(child_spec)
        except ValidationError:
            return None, {
                "code": "validation_failed",
                "message": "The child spec does not match the approved schema",
                "request_id": request_id,
            }
        spec_payload = spec.model_dump(mode="json")
        child_hash = jcs_request_hash(
            {
                "operation_id": "submitJob",
                "path": "/v1/jobs",
                "tenant_id": str(tenant_id),
                "body": {"spec": child_spec},
            }
        )
        now = transaction_timestamp(session)
        try:
            with session.begin_nested():
                outcome = begin_idempotency(
                    session,
                    context=str(tenant_id),
                    principal_id=str(live.user_id),
                    operation_id="submitJob",
                    key=sweep_expansion.child_idempotency_key(
                        sweep_id, child_index, parameter_hash
                    ),
                    request_hash=child_hash,
                    expires_at=now + self._retention(),
                    pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
                )
                if outcome.replay is not None:
                    return UUID(str((outcome.replay.body or {})["job_id"])), None
                tenant_policy, global_counter, user_scope_id = self.jobs._admit(
                    session, live, tenant_id
                )
                job_id = new_uuid7()
                self.jobs._create_job(
                    session,
                    live=live,
                    tenant_id=tenant_id,
                    job_id=job_id,
                    session_id=new_uuid7(),
                    spec=spec,
                    spec_payload=spec_payload,
                    spec_checksum=jcs_request_hash(spec_payload),
                    tenant_policy=tenant_policy,
                    global_counter=global_counter,
                    user_scope_id=user_scope_id,
                    now=now,
                    audit_metadata={"sweep_id": str(sweep_id), "child_index": child_index},
                )
                row = (
                    session.execute(JobService._job_query(tenant_id).where(jobs.c.job_id == job_id))
                    .mappings()
                    .one()
                )
                complete_idempotency(
                    session,
                    outcome.record_id,
                    status=202,
                    body=JobService._job_view(row),
                    headers={"Location": f"/v1/jobs/{job_id}", "ETag": '"v1"'},
                    resource_id=job_id,
                )
                return job_id, None
        except ApplicationError as exc:
            if exc.code in _ABORTING_CODES:
                raise
            # ADMISSION_OFF is a child outcome; WRITE_FROZEN must not write one.
            if self._mode(session, share=True) == "WRITE_FROZEN":
                raise _write_frozen() from None
            return None, {"code": exc.code, "message": exc.message, "request_id": request_id}

    def _view(
        self, session: Session, tenant_id: UUID, sweep_id: UUID, *, page_size: int, after: int
    ) -> dict[str, Any]:
        parent = (
            session.execute(
                select(sweep_parents).where(
                    sweep_parents.c.sweep_id == sweep_id, sweep_parents.c.tenant_id == tenant_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if parent is None:
            raise ApplicationError(
                code="resource_not_found", status=404, message="The sweep was not found"
            )
        rows = (
            session.execute(
                select(sweep_children)
                .where(
                    sweep_children.c.sweep_id == sweep_id,
                    sweep_children.c.child_index > after,
                )
                .order_by(sweep_children.c.child_index)
                .limit(page_size + 1)
            )
            .mappings()
            .all()
        )
        visible = rows[:page_size]
        return json_wire_value(
            {
                "sweep_id": parent["sweep_id"],
                "tenant_id": parent["tenant_id"],
                "child_count": parent["child_count"],
                "accepted_count": parent["accepted_count"],
                "rejected_count": parent["rejected_count"],
                "children": [
                    {
                        "child_index": row["child_index"],
                        "parameter_hash": row["parameter_hash"],
                        "status": "ACCEPTED" if row["job_id"] is not None else "REJECTED",
                        "job_id": row["job_id"],
                        "error": row["rejected_error"],
                    }
                    for row in visible
                ],
                "page": {
                    "next_cursor": visible[-1]["child_index"]
                    if len(rows) > page_size and visible
                    else None,
                    "page_size": page_size,
                },
                "created_at": parent["created_at"],
            }
        )

    def get(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        sweep_id: UUID,
        page_size: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        if not 1 <= page_size <= 100:
            raise ApplicationError(
                code="validation_failed", status=422, message="Page size must be between 1 and 100"
            )

        def operation(session: Session) -> dict[str, Any]:
            live = self.jobs._authorize(session, principal, tenant_id, write=False)
            now = transaction_timestamp(session)
            binding = {
                "actor_id": str(live.user_id),
                "tenant_id": str(tenant_id),
                "operation_id": "getSweep",
                "sweep_id": str(sweep_id),
            }
            after = -1
            if cursor:
                after = self._decode_cursor(cursor, binding=binding, now=now)
            view = self._view(session, tenant_id, sweep_id, page_size=page_size, after=after)
            last = view["page"]["next_cursor"]
            if last is not None:
                view["page"]["next_cursor"] = self.jobs._cursor(now).encode(
                    binding=binding, position={"child_index": str(last)}
                )
            return view

        return run_transaction(self.session_factory, operation)

    def _decode_cursor(self, cursor: str, *, binding: dict[str, str], now: datetime) -> int:
        try:
            position = self.jobs._cursor(now).decode(cursor, expected_binding=binding)
            text = position["child_index"]
            if set(position) != {"child_index"} or not text.isascii() or not text.isdigit():
                raise ValueError("cursor position is invalid")
            child_index = int(text)
            if not 0 <= child_index <= 98:
                raise ValueError("cursor position is invalid")
            return child_index
        except (CursorError, KeyError, ValueError, TypeError):
            raise ApplicationError(
                code="invalid_cursor", status=400, message="The pagination cursor is invalid"
            ) from None


__all__ = ["SweepService"]
