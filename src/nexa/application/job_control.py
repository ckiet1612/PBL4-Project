"""B15 user job control: one locked pipeline for cancel, pause, resume and manual retry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import exists, insert, select, update
from sqlalchemy.orm import Session

from nexa.api.schemas import ControlRequest, JobSubmitRequest, RetryRequest
from nexa.application.checkpoint_restore import (
    _not_corrupt,
    restore_candidates,
    verify_candidate,
)
from nexa.application.checkpoint_service import _storage_unavailable, spec_and_template
from nexa.application.checkpoint_validation import (
    CheckpointManifestError,
    expected_compatibility,
)
from nexa.application.errors import ApplicationError
from nexa.application.execution_cleanup import _adjust_counters
from nexa.application.idempotency import begin_idempotency, complete_idempotency
from nexa.application.job_recovery import (
    extend_retention,
    fence_attempt,
    restorable_scope,
    retry_lineage,
)
from nexa.application.preconditions import VersionPrecondition, resolve_expected_version
from nexa.domain.identity import Principal, require_tenant_membership
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import clock_timestamp, transaction_timestamp
from nexa.infrastructure.persistence.transactions import run_transaction

_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
_IMMEDIATE_CANCEL = {"QUEUED", "RETRY_WAIT", "PAUSED"}
_ACTIVE = {"DISPATCHING", "RUNNING", "PAUSING"}


def _state_conflict(message: str) -> ApplicationError:
    return ApplicationError(code="state_conflict", status=409, message=message)


def _infeasible(message: str) -> ApplicationError:
    return ApplicationError(code="infeasible_request", status=422, message=message)


def _restorable_checkpoint(session: Session, job_id: UUID, template) -> bool:
    """A committed, non-corrupt checkpoint whose compatibility matches the frozen template.

    Blob integrity is re-verified outside a transaction when the next attempt is claimed.
    """
    rows = session.execute(
        select(s.checkpoints.c.compatibility).where(
            restorable_scope(job_id),
            s.checkpoints.c.state == "COMMITTED",
            ~exists().where(
                s.checkpoint_corruptions.c.checkpoint_id == s.checkpoints.c.checkpoint_id
            ),
        )
    ).scalars()
    for compatibility in rows:
        try:
            if compatibility == expected_compatibility(
                dict(template), compatibility.get("architecture")
            ):
                return True
        except CheckpointManifestError:
            continue
    return False


@dataclass(slots=True)
class ControlContext:
    """Locked rows handed to one control transition; the job dict is updated in place."""

    session: Session
    live: Principal
    job: dict[str, Any]
    counters: list[dict[str, Any]]
    mode: str
    now: Any
    reason: str
    terminal: bool = False


class JobControlMixin:
    def _control(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        job_id: UUID,
        operation_id: str,
        action: str,
        reason: str,
        idempotency_key: str,
        request_hash: str,
        if_match: str | None,
        apply: Callable[[ControlContext], tuple[str, str]],
    ):
        from nexa.application.job_service import JobOperationResult

        def operation(session: Session) -> JobOperationResult:
            live = self._authorize(session, principal, tenant_id, write=True)
            # A MEMBER controls only jobs it submitted; others stay invisible.
            submitter = self._visible_submitter(session, live, tenant_id, job_id)
            started = transaction_timestamp(session)
            outcome = begin_idempotency(
                session,
                context=str(tenant_id),
                principal_id=str(live.user_id),
                operation_id=operation_id,
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=started
                + timedelta(days=self.settings.idempotency_terminal_retention_days),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if outcome.replay is not None:
                return JobOperationResult(
                    status=outcome.replay.status,
                    body=dict(outcome.replay.body or {}),
                    headers=dict(outcome.replay.headers),
                )
            expected = resolve_expected_version(VersionPrecondition(if_match))
            mode = session.execute(
                select(s.policy_versions.c.operational_mode)
                .where(s.policy_versions.c.is_current.is_(True))
                .with_for_update()
            ).scalar_one()
            if mode == "WRITE_FROZEN":
                raise _state_conflict("Writes are frozen")
            session.execute(
                select(s.tenant_policies)
                .where(
                    s.tenant_policies.c.tenant_id == tenant_id,
                    s.tenant_policies.c.is_current.is_(True),
                )
                .with_for_update()
            ).all()
            counters = self._control_counters(session, tenant_id, submitter)
            job = dict(
                session.execute(
                    select(s.jobs)
                    .where(s.jobs.c.tenant_id == tenant_id, s.jobs.c.job_id == job_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            session.execute(
                select(s.logical_sessions)
                .where(s.logical_sessions.c.job_id == job_id)
                .with_for_update()
            ).all()
            if job["version"] != expected:
                raise ApplicationError(
                    code="version_conflict",
                    status=412,
                    message="Job version does not match If-Match",
                )
            now = clock_timestamp(session)
            context = ControlContext(session, live, job, counters, mode, now, reason)
            event_type, event_reason = apply(context)
            self._control_event(context, action, event_type, event_reason)
            row = (
                session.execute(self._job_query(tenant_id).where(s.jobs.c.job_id == job_id))
                .mappings()
                .one()
            )
            body = self._job_view(row)
            headers = {"ETag": f'"v{row["version"]}"'}
            complete_idempotency(
                session,
                outcome.record_id,
                status=202,
                body=body,
                headers=headers,
                resource_id=job_id,
            )
            if context.terminal:
                extend_retention(
                    session, job_id, now, self.settings.idempotency_terminal_retention_days
                )
            return JobOperationResult(status=202, body=body, headers=headers)

        return run_transaction(self.session_factory, operation)

    @staticmethod
    def _control_counters(session: Session, tenant_id: UUID, submitter: UUID):
        counters = []
        for scope_type, scope_id in (
            ("GLOBAL", "global"),
            ("TENANT", str(tenant_id)),
            ("USER", f"{tenant_id}:{submitter}"),
        ):
            row = (
                session.execute(
                    select(s.admission_counters)
                    .where(
                        s.admission_counters.c.scope_type == scope_type,
                        s.admission_counters.c.scope_id == scope_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ApplicationError(
                    code="dependency_unavailable",
                    status=503,
                    message="Admission accounting is unavailable",
                    retry_after=1,
                )
            counters.append(dict(row))
        return counters

    @staticmethod
    def _control_event(context: ControlContext, action: str, event_type: str, reason: str):
        """Commit the job changes already staged in ``context.job`` with one event and audit."""
        session, job, now = context.session, context.job, context.now
        before = job["version"]
        sequence = job["event_sequence"] + 1
        changes = job.pop("_changes", {})
        session.execute(
            update(s.jobs)
            .where(s.jobs.c.job_id == job["job_id"])
            .values(**changes, version=before + 1, event_sequence=sequence, updated_at=now)
        )
        session.execute(
            insert(s.events).values(
                event_id=new_uuid7(),
                tenant_id=job["tenant_id"],
                job_id=job["job_id"],
                sequence=sequence,
                event_type=event_type,
                reason=reason,
                actor_type="USER",
                actor_id=str(context.live.user_id),
                safe_metadata={},
                created_at=now,
            )
        )
        # The free-text reason is stored verbatim only in the audit record.
        session.execute(
            insert(s.audit_records).values(
                audit_id=new_uuid7(),
                actor_type="USER",
                actor_id=str(context.live.user_id),
                tenant_id=job["tenant_id"],
                action=action,
                target_type="JOB",
                target_id=str(job["job_id"]),
                before_version=before,
                after_version=before + 1,
                reason=context.reason,
                safe_metadata={"event_type": event_type},
                created_at=now,
            )
        )

    @staticmethod
    def _lock_live_attempt(session: Session, job_id: UUID):
        """Lock the attempt still holding a lease, then its lease, allocation and grant."""
        attempt = (
            session.execute(
                select(s.attempts)
                .join(s.attempt_leases, s.attempt_leases.c.attempt_id == s.attempts.c.attempt_id)
                .where(s.attempts.c.job_id == job_id, s.attempt_leases.c.revoked_at.is_(None))
                .with_for_update(of=s.attempts)
            )
            .mappings()
            .one_or_none()
        )
        if attempt is None:
            return None
        attempt_id = attempt["attempt_id"]
        for table in (s.attempt_leases, s.allocations):
            session.execute(
                select(table).where(table.c.attempt_id == attempt_id).with_for_update()
            ).all()
        session.execute(
            select(s.attempt_authority_grants)
            .where(
                s.attempt_authority_grants.c.attempt_id == attempt_id,
                s.attempt_authority_grants.c.ended_at.is_(None),
            )
            .with_for_update()
        ).all()
        return dict(attempt)

    def cancel_job(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        job_id: UUID,
        request: ControlRequest,
        idempotency_key: str,
        request_hash: str,
        if_match: str | None,
    ):
        def apply(context: ControlContext) -> tuple[str, str]:
            session, job, now = context.session, context.job, context.now
            state = job["state"]
            if state in _IMMEDIATE_CANCEL:
                # No container exists: close waiting state and the outstanding admission now.
                session.execute(
                    update(s.reservations)
                    .where(
                        s.reservations.c.job_id == job_id, s.reservations.c.invalidated_at.is_(None)
                    )
                    .values(invalidated_at=now, invalidation_reason="job_cancelled")
                )
                session.execute(
                    update(s.retry_schedules)
                    .where(
                        s.retry_schedules.c.job_id == job_id,
                        s.retry_schedules.c.closed_at.is_(None),
                    )
                    .values(closed_at=now)
                )
                _adjust_counters(session, context.counters, now, outstanding=1)
                job["_changes"] = {
                    "state": "CANCELLED",
                    "desired_state": "CANCELLED",
                    "recovery_intent": None,
                    "waiting_reason": None,
                    "terminal_at": now,
                }
                context.terminal = True
                return "JOB_CANCELLED", "USER_CANCEL"
            if state in _ACTIVE:
                attempt = self._lock_live_attempt(session, job_id)
                changes = {"state": "CANCELLING", "desired_state": "CANCELLED"}
                if attempt is not None:
                    fence_attempt(
                        session,
                        attempt_id=attempt["attempt_id"],
                        now=now,
                        state="STOPPING",
                        failure_class="USER_CANCEL",
                        failure_reason="USER_CANCEL",
                        revoke_reason="USER_CANCEL",
                    )
                    changes["job_fence"] = job["job_fence"] + 1
                job["_changes"] = changes
                return "CANCEL_REQUESTED", "USER_CANCEL"
            if state == "RECOVERING":
                # Authority was already fenced; cleanup proof finishes the cancel.
                job["_changes"] = {"state": "CANCELLING", "desired_state": "CANCELLED"}
                return "CANCEL_REQUESTED", "USER_CANCEL"
            raise _state_conflict(f"A {state} job cannot be cancelled")

        return self._control(
            principal,
            tenant_id=tenant_id,
            job_id=job_id,
            operation_id="cancelJob",
            action="job.cancel",
            reason=request.reason,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            if_match=if_match,
            apply=apply,
        )

    def pause_job(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        job_id: UUID,
        request: ControlRequest,
        idempotency_key: str,
        request_hash: str,
        if_match: str | None,
    ):
        def apply(context: ControlContext) -> tuple[str, str]:
            job = context.job
            _, template = spec_and_template(context.session, job_id)
            # SM:53 — capability is checked before state.
            if not template["checkpointable"]:
                raise _infeasible("Job template is not checkpointable")
            if job["state"] != "RUNNING" or job["desired_state"] != "RUNNING":
                raise _state_conflict(f"A {job['state']} job cannot be paused")
            # The lease is kept: the worker sees desired PAUSED on its next renewal.
            job["_changes"] = {"state": "PAUSING", "desired_state": "PAUSED"}
            return "PAUSE_REQUESTED", "USER_PAUSE"

        return self._control(
            principal,
            tenant_id=tenant_id,
            job_id=job_id,
            operation_id="pauseJob",
            action="job.pause",
            reason=request.reason,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            if_match=if_match,
            apply=apply,
        )

    def resume_job(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        job_id: UUID,
        request: ControlRequest,
        idempotency_key: str,
        request_hash: str,
        if_match: str | None,
    ):
        def apply(context: ControlContext) -> tuple[str, str]:
            session, job, now = context.session, context.job, context.now
            if job["state"] != "PAUSED":
                raise _state_conflict(f"A {job['state']} job cannot be resumed")
            _, template = spec_and_template(session, job_id)
            if not (template["restart_safe"] or _restorable_checkpoint(session, job_id, template)):
                raise _infeasible("No compatible checkpoint or restart-safe input")
            # Resume keeps admission and retry budget; it only takes a new queue position.
            global_counter = context.counters[0]
            session.execute(
                update(s.admission_counters)
                .where(
                    s.admission_counters.c.scope_type == "GLOBAL",
                    s.admission_counters.c.scope_id == "global",
                )
                .values(version=global_counter["version"] + 1, updated_at=now)
            )
            job["_changes"] = {
                "state": "QUEUED",
                "desired_state": "RUNNING",
                "recovery_intent": None,
                "waiting_reason": None,
                "ready_sequence": int(global_counter["version"]),
            }
            return "JOB_RESUMED", "USER_RESUME"

        return self._control(
            principal,
            tenant_id=tenant_id,
            job_id=job_id,
            operation_id="resumeJob",
            action="job.resume",
            reason=request.reason,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            if_match=if_match,
            apply=apply,
        )

    def _retry_checkpoint_scan(self, principal, tenant_id, job_id, checkpoint_id):
        """Verify a requested checkpoint's blobs before the retry transaction, read-only.

        Returns ("VALID", id), ("INVALID", reason), ("MISSING", None), ("SKIP", None) or
        ("UNAVAILABLE", None); the retry transaction applies it only after authorization,
        idempotency replay and If-Match, and rechecks the metadata under lock.
        """

        def operation(session: Session):
            try:
                live = self._authorize(session, principal, tenant_id, write=True)
                self._visible_submitter(session, live, tenant_id, job_id)
            except ApplicationError:
                # The retry transaction reports the same error in its own order.
                return None
            row = (
                session.execute(
                    select(s.checkpoints).where(
                        s.checkpoints.c.checkpoint_id == checkpoint_id,
                        s.checkpoints.c.tenant_id == tenant_id,
                        s.checkpoints.c.state == "COMMITTED",
                        _not_corrupt(),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return "MISSING"
            owner = {"tenant_id": tenant_id, "job_id": row["job_id"]}
            spec, template = spec_and_template(session, row["job_id"])
            (candidate,) = restore_candidates(session, owner, spec, template, [dict(row)])
            plan = {
                "tenant_id": tenant_id,
                # The retry creates a new job; B16-R20 keeps inference checkpoints job-scoped.
                "job_id": None,
                "parameters": spec["canonical_spec"].get("parameters") or {},
            }
            return candidate, plan, template

        loaded = run_transaction(self.session_factory, operation)
        if loaded is None:
            return "SKIP", None
        if loaded == "MISSING":
            return "MISSING", None
        candidate, plan, template = loaded
        if self.artifact_store is None:
            return "UNAVAILABLE", None
        row = candidate["row"]
        try:
            compatibility = expected_compatibility(
                template, (row["compatibility"] or {}).get("architecture")
            )
        except CheckpointManifestError as exc:
            return "INVALID", exc.reason_code
        try:
            outcome, value = verify_candidate(self.artifact_store, plan, candidate, compatibility)
        except ApplicationError:
            return "UNAVAILABLE", None
        if outcome != "VALID":
            return "INVALID", value
        return "VALID", row["checkpoint_id"]

    @staticmethod
    def _visible_submitter(session: Session, live: Principal, tenant_id: UUID, job_id: UUID):
        """The submitter of a job `live` may control; a MEMBER sees only its own jobs."""
        role = require_tenant_membership(live, tenant_id).role
        submitter = session.execute(
            select(s.jobs.c.submitter_user_id).where(
                s.jobs.c.tenant_id == tenant_id, s.jobs.c.job_id == job_id
            )
        ).scalar_one_or_none()
        if submitter is None or (role != "TENANT_ADMIN" and submitter != live.user_id):
            raise ApplicationError(
                code="resource_not_found", status=404, message="Job was not found"
            )
        return submitter

    def _lock_retry_checkpoint(self, session, live, tenant_id, source_spec, checkpoint_id, scan):
        """Recheck the scanned checkpoint under lock; the caller holds counters and the source."""
        not_found = ApplicationError(
            code="resource_not_found", status=404, message="Checkpoint was not found"
        )
        row = (
            session.execute(
                select(s.checkpoints)
                .where(
                    s.checkpoints.c.checkpoint_id == checkpoint_id,
                    s.checkpoints.c.tenant_id == tenant_id,
                )
                .with_for_update(read=True)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise not_found
        try:
            self._visible_submitter(session, live, tenant_id, row["job_id"])
        except ApplicationError as exc:
            raise not_found from exc
        if scan[0] == "UNAVAILABLE":
            raise _storage_unavailable()
        owner_spec = session.execute(
            select(s.job_specs.c.spec_checksum).where(s.job_specs.c.job_id == row["job_id"])
        ).scalar_one()
        corrupt = session.execute(
            select(s.checkpoint_corruptions.c.checkpoint_id).where(
                s.checkpoint_corruptions.c.checkpoint_id == checkpoint_id
            )
        ).first()
        # Manual retry keeps the immutable spec/input, so provenance must match exactly,
        # and the owner is the source Job or its ancestor on the retry chain (B15-R05).
        if (
            row["state"] != "COMMITTED"
            or corrupt is not None
            or row["job_id"] not in retry_lineage(session, tenant_id, source_spec["job_id"])
            or owner_spec != source_spec["spec_checksum"]
            or scan != ("VALID", checkpoint_id)
        ):
            raise _infeasible("Checkpoint is not a compatible committed checkpoint")
        files = select(s.artifact_references.c.artifact_id).where(
            s.artifact_references.c.tenant_id == tenant_id,
            s.artifact_references.c.owner_type == "CHECKPOINT",
            s.artifact_references.c.owner_id == checkpoint_id,
            s.artifact_references.c.purpose == "CHECKPOINT_FILE",
        )
        wanted = {row["manifest_artifact_id"], *session.execute(files).scalars()}
        locked = session.execute(
            select(s.artifacts.c.artifact_id)
            .where(
                s.artifacts.c.tenant_id == tenant_id,
                s.artifacts.c.artifact_id.in_(wanted),
                s.artifacts.c.state == "COMMITTED",
            )
            .order_by(s.artifacts.c.artifact_id)
            .with_for_update(read=True)
        ).all()
        if len(locked) != len(wanted):
            raise _infeasible("Checkpoint is not a compatible committed checkpoint")

    def retry_failed_job(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        job_id: UUID,
        request: RetryRequest,
        idempotency_key: str,
        request_hash: str,
        if_match: str | None,
    ):
        """SM:41 — a new Job/LogicalSession from a FAILED source that stays unchanged."""
        from nexa.application.job_service import JobOperationResult

        checkpoint_id = request.checkpoint_id
        scan = (
            self._retry_checkpoint_scan(principal, tenant_id, job_id, checkpoint_id)
            if checkpoint_id is not None
            else None
        )
        new_job_id, new_session_id = new_uuid7(), new_uuid7()

        def operation(session: Session) -> JobOperationResult:
            live = self._authorize(session, principal, tenant_id, write=True)
            self._visible_submitter(session, live, tenant_id, job_id)
            now = transaction_timestamp(session)
            outcome = begin_idempotency(
                session,
                context=str(tenant_id),
                principal_id=str(live.user_id),
                operation_id="retryFailedJob",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=now + timedelta(days=self.settings.idempotency_terminal_retention_days),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if outcome.replay is not None:
                return JobOperationResult(
                    status=outcome.replay.status,
                    body=dict(outcome.replay.body or {}),
                    headers=dict(outcome.replay.headers),
                )
            expected = resolve_expected_version(VersionPrecondition(if_match))
            # Admission exactly as submit, for the retrying principal.
            tenant_policy, global_counter, user_scope_id = self._admit(session, live, tenant_id)
            # The terminal source is only read: no event, version or counter change.
            source = (
                session.execute(
                    select(s.jobs)
                    .where(s.jobs.c.tenant_id == tenant_id, s.jobs.c.job_id == job_id)
                    .with_for_update(read=True)
                )
                .mappings()
                .one()
            )
            if source["version"] != expected:
                raise ApplicationError(
                    code="version_conflict",
                    status=412,
                    message="Job version does not match If-Match",
                )
            if source["state"] != "FAILED":
                raise _state_conflict(f"A {source['state']} job cannot be retried")
            source_spec = (
                session.execute(select(s.job_specs).where(s.job_specs.c.job_id == job_id))
                .mappings()
                .one()
            )
            if checkpoint_id is not None:
                self._lock_retry_checkpoint(
                    session, live, tenant_id, source_spec, checkpoint_id, scan
                )
            try:
                spec = JobSubmitRequest.model_validate({"spec": source_spec["canonical_spec"]}).spec
            except ValidationError as exc:
                raise _infeasible("The source job spec cannot be admitted again") from exc
            metadata = {"retry_of_job_id": str(job_id)}
            if checkpoint_id is not None:
                metadata["checkpoint_id"] = str(checkpoint_id)
            self._create_job(
                session,
                live=live,
                tenant_id=tenant_id,
                job_id=new_job_id,
                session_id=new_session_id,
                spec=spec,
                spec_payload=source_spec["canonical_spec"],
                spec_checksum=source_spec["spec_checksum"],
                tenant_policy=tenant_policy,
                global_counter=global_counter,
                user_scope_id=user_scope_id,
                now=now,
                retry_of_job_id=job_id,
                event_reason="MANUAL_RETRY",
                action="job.retry",
                # The free-text reason is stored verbatim only in the audit record.
                audit_reason=request.reason,
                audit_metadata=metadata,
            )
            if checkpoint_id is not None:
                session.execute(
                    insert(s.checkpoint_references).values(
                        tenant_id=tenant_id,
                        source_checkpoint_id=checkpoint_id,
                        target_job_id=new_job_id,
                        reason="MANUAL_RETRY",
                        created_at=now,
                    )
                )
            row = (
                session.execute(self._job_query(tenant_id).where(s.jobs.c.job_id == new_job_id))
                .mappings()
                .one()
            )
            body = self._job_view(row)
            headers = {"ETag": '"v1"'}
            complete_idempotency(
                session,
                outcome.record_id,
                status=202,
                body=body,
                headers=headers,
                resource_id=new_job_id,
            )
            return JobOperationResult(status=202, body=body, headers=headers)

        return run_transaction(self.session_factory, operation)
