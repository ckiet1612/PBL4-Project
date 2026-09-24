"""Committed dispatch and attempt callbacks for the CPU execution lifecycle."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import insert, select, update

from nexa.api.schemas import Authority
from nexa.application.artifact_service import ArtifactService
from nexa.application.errors import ApplicationError
from nexa.application.execution_cleanup import (
    ExecutionCleanupMixin,
    _adjust_counters,
    _lock_policy_counters,
)
from nexa.application.json_codec import json_wire_value
from nexa.application.result_validation import validate_cpu_result_manifest
from nexa.application.worker_service import WorkerService
from nexa.infrastructure.artifacts.store import ArtifactError
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import clock_timestamp
from nexa.infrastructure.persistence.schema import (
    allocations,
    artifact_references,
    artifacts,
    attempt_authority_grants,
    attempt_leases,
    attempts,
    job_specs,
    jobs,
    logical_sessions,
    result_reservations,
    results,
    template_versions,
    upload_sessions,
)
from nexa.infrastructure.persistence.transactions import run_transaction


class ExecutionService(ExecutionCleanupMixin, WorkerService):
    def __init__(self, *args, artifact_store=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.artifact_store = artifact_store

    def poll(self, *, worker_id, credential, incarnation_id, long_poll_seconds):
        """Return one committed coordinator offer without creating authority."""

        def operation(session):
            mode = self._mode(session)
            worker = self._worker_auth(session, worker_id=worker_id, credential=credential)
            self._require_current_incarnation(
                session, worker, incarnation_id, require_reconciling=False
            )
            if (
                mode != "NORMAL"
                or worker["health"] != "READY"
                or worker["admin_state"] != "ENABLED"
            ):
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Worker is not eligible to poll for dispatch",
                )
            now = clock_timestamp(session)
            row = (
                session.execute(
                    select(
                        attempts,
                        jobs,
                        allocations,
                        attempt_leases,
                        job_specs.c.canonical_spec,
                        job_specs.c.input_artifact_id,
                    )
                    .join(jobs, jobs.c.job_id == attempts.c.job_id)
                    .join(allocations, allocations.c.attempt_id == attempts.c.attempt_id)
                    .join(attempt_leases, attempt_leases.c.attempt_id == attempts.c.attempt_id)
                    .join(job_specs, job_specs.c.job_id == jobs.c.job_id)
                    .where(
                        attempts.c.worker_id == worker_id,
                        attempts.c.worker_incarnation_id == incarnation_id,
                        attempts.c.state == "CREATED",
                        jobs.c.state == "DISPATCHING",
                        allocations.c.state == "HELD",
                        attempt_leases.c.revoked_at.is_(None),
                        attempt_leases.c.expires_at > now,
                    )
                    .order_by(attempts.c.created_at, attempts.c.attempt_id)
                )
                .mappings()
                .first()
            )
            offer = None
            if row is not None:
                artifact = (
                    session.execute(
                        select(artifacts).where(
                            artifacts.c.artifact_id == row["input_artifact_id"],
                            artifacts.c.tenant_id == row["tenant_id"],
                            artifacts.c.state == "COMMITTED",
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if artifact is None:
                    raise ApplicationError(
                        code="dependency_unavailable",
                        status=503,
                        message="Dispatch input artifact is unavailable",
                        retry_after=1,
                    )
                authority = {
                    "worker_id": row["worker_id"],
                    "worker_incarnation_id": row["worker_incarnation_id"],
                    "attempt_id": row["attempt_id"],
                    "allocation_id": row["allocation_id"],
                    "lease_id": row["lease_id"],
                    "job_fence": row["job_fence"],
                }
                offer = {
                    "authority": authority,
                    "dispatch_coordinator_epoch": row["dispatch_coordinator_epoch"],
                    "spec": row["canonical_spec"],
                    "input_artifact": ArtifactService._view(artifact),
                    "checkpoint": None,
                    "startup_limit_seconds": 30,
                    "lease_duration_seconds": 45,
                }
            return json_wire_value(
                {
                    "server_time": now,
                    "offer": offer,
                    "retry_after_seconds": min(max(long_poll_seconds, 0), 30),
                }
            )

        return run_transaction(self.session_factory, operation)

    def _callback(
        self, session, *, authority, credential, attempt_id, callback_id, payload_hash, operation_id
    ):
        if authority.attempt_id != attempt_id:
            raise ApplicationError(
                code="state_conflict", status=409, message="Attempt identity mismatch"
            )
        receipt, replay = self._begin_callback(
            session,
            worker_id=authority.worker_id,
            operation_id=operation_id,
            callback_id=callback_id,
            payload_hash=payload_hash,
        )
        self._mode(session)
        worker = self._worker_auth(session, worker_id=authority.worker_id, credential=credential)
        self._require_current_incarnation(
            session, worker, authority.worker_incarnation_id, require_reconciling=False
        )
        # The committed payload hash contains the full Authority and artifact IDs.
        # Once cleanup ends the grant, the original callback can still replay after
        # credential and current-incarnation checks. A new callback needs live authority.
        if replay is not None:
            return receipt, replay, None
        rows = self._authority_rows(session, authority, worker_id=authority.worker_id)
        return receipt, replay, rows

    def reserve_result(
        self,
        *,
        credential: str,
        attempt_id: UUID,
        callback_id: UUID,
        payload_hash: str,
        authority: Authority,
    ):
        def operation(session):
            receipt, replay, rows = self._callback(
                session,
                authority=authority,
                credential=credential,
                attempt_id=attempt_id,
                callback_id=callback_id,
                payload_hash=payload_hash,
                operation_id="workerReserveResult",
            )
            if replay is not None:
                return replay
            job, attempt, lease, allocation, grant = rows
            now = clock_timestamp(session)
            self._live_authority(job, attempt, lease, allocation, now)
            if attempt["state"] != "RUNNING":
                raise ApplicationError(
                    code="state_conflict", status=409, message="Attempt has not started"
                )
            existing = (
                session.execute(
                    select(result_reservations)
                    .where(
                        result_reservations.c.job_id == job["job_id"],
                        result_reservations.c.state == "ACTIVE",
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="A different result reservation callback is already active",
                )
            result_id, reserved_at = new_uuid7(), now
            session.execute(
                insert(result_reservations).values(
                    result_id=result_id,
                    tenant_id=job["tenant_id"],
                    job_id=job["job_id"],
                    attempt_id=attempt_id,
                    authority_grant_id=grant["grant_id"],
                    callback_id=callback_id,
                    state="ACTIVE",
                    reserved_at=now,
                )
            )
            body = json_wire_value(
                dict(
                    callback_id=callback_id,
                    result_id=result_id,
                    job_id=job["job_id"],
                    attempt_id=attempt_id,
                    reserved_at=reserved_at,
                )
            )
            self._complete_callback(session, receipt, body)
            return body

        return run_transaction(self.session_factory, operation)

    @staticmethod
    def _transition(session, job, authority, *, state, event_type, now):
        from sqlalchemy import update

        from nexa.infrastructure.persistence.schema import audit_records, events, jobs

        session.execute(
            update(jobs)
            .where(jobs.c.job_id == job["job_id"])
            .values(
                state=state,
                version=job["version"] + 1,
                event_sequence=job["event_sequence"] + 1,
                updated_at=now,
            )
        )
        session.execute(
            insert(events).values(
                event_id=new_uuid7(),
                tenant_id=job["tenant_id"],
                job_id=job["job_id"],
                sequence=job["event_sequence"] + 1,
                event_type=event_type,
                reason=event_type.lower(),
                actor_type="WORKER",
                actor_id=str(authority.worker_id),
            )
        )
        session.execute(
            insert(audit_records).values(
                audit_id=new_uuid7(),
                tenant_id=job["tenant_id"],
                actor_type="WORKER",
                actor_id=str(authority.worker_id),
                action=event_type.lower(),
                target_type="JOB",
                target_id=str(job["job_id"]),
                before_version=job["version"],
                after_version=job["version"] + 1,
                reason=event_type.lower(),
            )
        )
        job["state"], job["version"], job["event_sequence"] = (
            state,
            job["version"] + 1,
            job["event_sequence"] + 1,
        )

    def claim_attempt(self, *, credential, attempt_id, callback_id, payload_hash, authority):
        from sqlalchemy import update

        from nexa.application.artifact_service import ArtifactService
        from nexa.infrastructure.persistence.schema import (
            artifacts,
            attempts,
            job_specs,
            logical_sessions,
            template_versions,
            templates,
        )

        def operation(session):
            receipt, replay, rows = self._callback(
                session,
                authority=authority,
                credential=credential,
                attempt_id=attempt_id,
                callback_id=callback_id,
                payload_hash=payload_hash,
                operation_id="workerClaimAttempt",
            )
            if replay is not None:
                return replay
            job, attempt, lease, allocation, grant = rows
            now = clock_timestamp(session)
            self._live_authority(job, attempt, lease, allocation, now)
            context = attempt["execution_context"]
            if context is None:
                if attempt["state"] != "CREATED" or job["state"] != "DISPATCHING":
                    raise ApplicationError(
                        code="state_conflict", status=409, message="Attempt is not claimable"
                    )
                spec = (
                    session.execute(select(job_specs).where(job_specs.c.job_id == job["job_id"]))
                    .mappings()
                    .one()
                )
                template = (
                    session.execute(
                        select(template_versions).where(
                            template_versions.c.template_id == spec["template_id"],
                            template_versions.c.version == spec["template_version"],
                        )
                    )
                    .mappings()
                    .one()
                )
                current = (
                    session.execute(
                        select(templates).where(templates.c.template_id == spec["template_id"])
                    )
                    .mappings()
                    .one()
                )
                input_ids = [
                    i
                    for i in [spec["input_artifact_id"], spec["model_artifact_id"]]
                    if i is not None
                ]
                inputs = (
                    session.execute(
                        select(artifacts)
                        .where(
                            artifacts.c.tenant_id == job["tenant_id"],
                            artifacts.c.artifact_id.in_(input_ids),
                            artifacts.c.state == "COMMITTED",
                        )
                        .order_by(artifacts.c.artifact_id)
                    )
                    .mappings()
                    .all()
                )
                if len(inputs) != len(set(input_ids)):
                    raise ApplicationError(
                        code="state_conflict", status=409, message="Execution input unavailable"
                    )
                requirements = template["capability_requirements"]
                template_view = {
                    key: template[key]
                    for key in [
                        "template_id",
                        "version",
                        "adapter_id",
                        "adapter_version",
                        "image_digest",
                        "checkpointable",
                        "restart_safe",
                        "parameter_schema",
                    ]
                }
                template_view.update(
                    display_name=current["display_name"],
                    enabled=current["enabled"],
                    allowed_devices=[requirements.get("device", "CPU")],
                    capability_requirement=requirements,
                )
                item = self._reconciliation_item(session, allocation, now)
                context = json_wire_value(
                    dict(
                        job_id=job["job_id"],
                        logical_session_id=session.execute(
                            select(logical_sessions.c.session_id).where(
                                logical_sessions.c.job_id == job["job_id"]
                            )
                        ).scalar_one(),
                        authority=authority.model_dump(mode="json"),
                        execution_intent=attempt["execution_intent"],
                        spec=spec["canonical_spec"],
                        template_snapshot=template_view,
                        adapter_id=template["adapter_id"],
                        adapter_version=template["adapter_version"],
                        image_digest=template["image_digest"],
                        allocation=item["allocation"],
                        input_artifacts=[ArtifactService._view(row) for row in inputs],
                        restore_checkpoint=None,
                        startup_nonce=attempt["startup_nonce"],
                        startup_limit_seconds=30,
                        lease_duration_seconds=45,
                    )
                )
                session.execute(
                    update(attempts)
                    .where(attempts.c.attempt_id == attempt_id)
                    .values(state="CLAIMED", execution_context=context, claimed_at=now)
                )
            body = json_wire_value(
                dict(
                    callback_id=callback_id,
                    accepted=True,
                    server_time=now,
                    job_state=job["state"],
                    job_version=job["version"],
                    execution_context=context,
                )
            )
            self._complete_callback(session, receipt, body)
            return body

        return run_transaction(self.session_factory, operation)

    def start_attempt(self, *, credential, attempt_id, callback_id, payload_hash, request):
        from datetime import timedelta

        from sqlalchemy import update

        from nexa.infrastructure.persistence.schema import (
            attempt_leases,
            attempts,
            container_identities,
        )

        authority = request.authority

        def operation(session):
            receipt, replay, rows = self._callback(
                session,
                authority=authority,
                credential=credential,
                attempt_id=attempt_id,
                callback_id=callback_id,
                payload_hash=payload_hash,
                operation_id="workerStartAttempt",
            )
            if replay is not None:
                return replay
            job, attempt, lease, allocation, grant = rows
            now = clock_timestamp(session)
            self._live_authority(job, attempt, lease, allocation, now)
            if (
                attempt["state"] != "CLAIMED"
                or attempt["claimed_at"] is None
                or now >= attempt["created_at"] + timedelta(seconds=30)
                or request.startup_nonce != attempt["startup_nonce"]
            ):
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Attempt startup identity or budget is invalid",
                )
            session.execute(
                insert(container_identities).values(
                    tenant_id=job["tenant_id"],
                    job_id=job["job_id"],
                    attempt_id=attempt_id,
                    allocation_id=authority.allocation_id,
                    startup_nonce=request.startup_nonce,
                    executor_create_sequence=request.executor_operation_sequence,
                    container_id=request.container.container_id,
                    runtime_identity_digest=request.container.runtime_identity_digest,
                    created_at=now,
                )
            )
            expires = now + timedelta(seconds=45)
            session.execute(
                update(attempts)
                .where(attempts.c.attempt_id == attempt_id)
                .values(
                    state="RUNNING",
                    started_at=now,
                    executor_operation_sequence=request.executor_operation_sequence,
                )
            )
            session.execute(
                update(attempt_leases)
                .where(attempt_leases.c.lease_id == authority.lease_id)
                .values(expires_at=expires)
            )
            self._transition(
                session, job, authority, state="RUNNING", event_type="ATTEMPT_STARTED", now=now
            )
            body = json_wire_value(
                dict(
                    callback_id=callback_id,
                    accepted=True,
                    server_time=now,
                    job_state=job["state"],
                    job_version=job["version"],
                    lease_expires_at=expires,
                    lease_duration_seconds=45,
                    renew_interval_seconds=5,
                    safety_margin_seconds=5,
                )
            )
            self._complete_callback(session, receipt, body)
            return body

        return run_transaction(self.session_factory, operation)

    @staticmethod
    def _upload_from_attempt(session, *, artifact, job, attempt, grant):
        """Require a committed upload session in this exact Authority lineage."""
        uploaded = (
            session.execute(
                select(upload_sessions)
                .join(
                    artifact_references,
                    (artifact_references.c.owner_id == upload_sessions.c.upload_id)
                    & (artifact_references.c.owner_type == "UPLOAD_SESSION"),
                )
                .where(
                    artifact_references.c.artifact_id == artifact["artifact_id"],
                    artifact_references.c.tenant_id == job["tenant_id"],
                    upload_sessions.c.tenant_id == job["tenant_id"],
                    upload_sessions.c.job_id == job["job_id"],
                    upload_sessions.c.attempt_id == attempt["attempt_id"],
                    upload_sessions.c.state == "COMMITTED",
                    upload_sessions.c.expected_checksum == artifact["checksum"],
                    upload_sessions.c.expected_size_bytes == artifact["size_bytes"],
                )
            )
            .mappings()
            .all()
        )
        grants = {
            row["grant_id"]: row
            for row in session.execute(
                select(attempt_authority_grants).where(
                    attempt_authority_grants.c.attempt_id == attempt["attempt_id"]
                )
            ).mappings()
        }
        ancestors = set()
        current = grant["grant_id"]
        while current is not None and current not in ancestors:
            ancestors.add(current)
            member = grants.get(current)
            if member is None:
                break
            current = member["predecessor_grant_id"]
        for upload in uploaded:
            origin = grants.get(upload["authority_grant_id"])
            if (
                origin is not None
                and origin["grant_id"] in ancestors
                and origin["job_id"] == job["job_id"]
                and origin["attempt_id"] == attempt["attempt_id"]
                and origin["allocation_id"] == grant["allocation_id"]
                and origin["lease_id"] == grant["lease_id"]
                and origin["job_fence"] == grant["job_fence"]
            ):
                return
        raise ApplicationError(
            code="state_conflict",
            status=409,
            message="Result artifact has no matching upload lineage",
        )

    def _manifest_bytes(self, artifact):
        if self.artifact_store is None:
            raise ApplicationError(
                code="dependency_unavailable",
                status=503,
                message="Artifact storage is unavailable",
                retry_after=1,
            )
        # The CPU graph contains one file, so its canonical manifest is bounded.
        if artifact["size_bytes"] > 1024 * 1024:
            raise ApplicationError(
                code="validation_failed", status=422, message="CPU manifest exceeds its size bound"
            )
        try:
            reader = self.artifact_store.open(artifact["blob_key"])
            try:
                raw = reader.read(1024 * 1024 + 1)
            finally:
                reader.close()
        except ArtifactError as exc:
            raise ApplicationError(
                code="dependency_unavailable",
                status=503,
                message="Artifact storage is unavailable",
                retry_after=1,
            ) from exc
        return raw

    def complete_attempt(self, *, credential, attempt_id, callback_id, payload_hash, request):
        def operation(session):
            _, counters = _lock_policy_counters(session, attempt_id)
            receipt, replay, rows = self._callback(
                session,
                authority=request.authority,
                credential=credential,
                attempt_id=attempt_id,
                callback_id=callback_id,
                payload_hash=payload_hash,
                operation_id="workerCompleteAttempt",
            )
            if replay is not None:
                return replay
            job, attempt, lease, allocation, grant = rows
            now = clock_timestamp(session)
            self._live_authority(job, attempt, lease, allocation, now)
            if job["state"] != "RUNNING" or attempt["state"] != "RUNNING":
                raise ApplicationError(
                    code="state_conflict", status=409, message="Attempt is not running"
                )
            reservation = (
                session.execute(
                    select(result_reservations)
                    .where(
                        result_reservations.c.job_id == job["job_id"],
                        result_reservations.c.attempt_id == attempt_id,
                        result_reservations.c.authority_grant_id == grant["grant_id"],
                        result_reservations.c.state == "ACTIVE",
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if reservation is None or request.manifest.get("result_id") != str(
                reservation["result_id"]
            ):
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Result reservation identity is invalid",
                )
            artifact = (
                session.execute(
                    select(artifacts).where(
                        artifacts.c.artifact_id == request.result_manifest_artifact_id,
                        artifacts.c.tenant_id == job["tenant_id"],
                        artifacts.c.state == "COMMITTED",
                        artifacts.c.kind == "RESULT_MANIFEST",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if artifact is None:
                raise ApplicationError(
                    code="state_conflict", status=409, message="Result manifest is not committed"
                )
            self._upload_from_attempt(
                session, artifact=artifact, job=job, attempt=attempt, grant=grant
            )
            spec = (
                session.execute(select(job_specs).where(job_specs.c.job_id == job["job_id"]))
                .mappings()
                .one()
            )
            template = (
                session.execute(
                    select(template_versions).where(
                        template_versions.c.template_id == spec["template_id"],
                        template_versions.c.version == spec["template_version"],
                    )
                )
                .mappings()
                .one()
            )
            input_artifact = (
                session.execute(
                    select(artifacts).where(
                        artifacts.c.artifact_id == spec["input_artifact_id"],
                        artifacts.c.tenant_id == job["tenant_id"],
                        artifacts.c.state == "COMMITTED",
                    )
                )
                .mappings()
                .one()
            )
            session_id = session.execute(
                select(logical_sessions.c.session_id).where(
                    logical_sessions.c.job_id == job["job_id"]
                )
            ).scalar_one()
            provenance = json_wire_value(
                {
                    "tenant_id": job["tenant_id"],
                    "job_id": job["job_id"],
                    "session_id": session_id,
                    "attempt_id": attempt_id,
                    "job_fence": job["job_fence"],
                    "input_checksum": input_artifact["checksum"],
                    "spec_checksum": spec["spec_checksum"],
                    "template_id": spec["template_id"],
                    "template_version": spec["template_version"],
                    "adapter_id": template["adapter_id"],
                    "adapter_version": template["adapter_version"],
                    "image_digest": template["image_digest"],
                }
            )
            binding = validate_cpu_result_manifest(
                request.manifest,
                raw=self._manifest_bytes(artifact),
                artifact=artifact,
                expected_result_id=reservation["result_id"],
                provenance=provenance,
            )
            output = (
                session.execute(
                    select(artifacts).where(
                        artifacts.c.artifact_id == UUID(binding["artifact_id"]),
                        artifacts.c.tenant_id == job["tenant_id"],
                        artifacts.c.state == "COMMITTED",
                        artifacts.c.kind == "RESULT_FILE",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if output is None or any(
                output[field] != binding[field]
                for field in ("media_type", "size_bytes", "checksum")
            ):
                raise ApplicationError(
                    code="state_conflict", status=409, message="Result file binding is invalid"
                )
            self._upload_from_attempt(
                session, artifact=output, job=job, attempt=attempt, grant=grant
            )
            session.execute(
                insert(results).values(
                    result_id=reservation["result_id"],
                    tenant_id=job["tenant_id"],
                    job_id=job["job_id"],
                    attempt_id=attempt_id,
                    manifest_artifact_id=artifact["artifact_id"],
                    manifest_checksum=artifact["checksum"],
                    completion_callback_id=callback_id,
                    created_at=now,
                )
            )
            for item, purpose, name in (
                (artifact, "RESULT_MANIFEST", "manifest"),
                (output, "RESULT_FILE", binding["logical_name"]),
            ):
                session.execute(
                    insert(artifact_references).values(
                        tenant_id=job["tenant_id"],
                        artifact_id=item["artifact_id"],
                        owner_type="RESULT",
                        owner_id=reservation["result_id"],
                        purpose=purpose,
                        logical_name=name,
                    )
                )
            session.execute(
                update(result_reservations)
                .where(result_reservations.c.result_id == reservation["result_id"])
                .values(state="COMMITTED")
            )
            session.execute(
                update(attempts)
                .where(attempts.c.attempt_id == attempt_id)
                .values(state="SUCCEEDED", ended_at=now, updated_at=now)
            )
            _adjust_counters(session, counters, now, outstanding=1)
            self._transition(
                session,
                job,
                request.authority,
                state="SUCCEEDED",
                event_type="RESULT_RECOGNIZED",
                now=now,
            )
            body = json_wire_value(
                {
                    "callback_id": callback_id,
                    "accepted": True,
                    "server_time": now,
                    "job_state": "SUCCEEDED",
                    "job_version": job["version"],
                }
            )
            self._complete_callback(session, receipt, body)
            return body

        return run_transaction(self.session_factory, operation)
