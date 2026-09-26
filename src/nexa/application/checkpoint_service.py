"""Fenced CPU checkpoint reservation and publish; no Docker or blob writes here."""

from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy import insert, select, update

from nexa.application.checkpoint_validation import (
    CHECKPOINT_MANIFEST_MAX_BYTES,
    CheckpointManifestError,
    check_file_artifacts,
    checkpoint_provenance,
    expected_compatibility,
    validate_checkpoint_manifest,
)
from nexa.application.errors import ApplicationError
from nexa.application.execution_cleanup import _event
from nexa.application.json_codec import json_wire_value
from nexa.infrastructure.artifacts.store import ArtifactError
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import clock_timestamp
from nexa.infrastructure.persistence.transactions import run_transaction


def _conflict(message):
    raise ApplicationError(code="state_conflict", status=409, message=message)


_INVENTORY_UNREPORTED = "Worker inventory is not reported yet"


def _storage_unavailable(message="Artifact storage is unavailable"):
    return ApplicationError(
        code="dependency_unavailable", status=503, message=message, retry_after=1
    )


def checkpoint_record(row, *, corrupt=False):
    return json_wire_value(
        {
            "checkpoint_id": row["checkpoint_id"],
            "job_id": row["job_id"],
            "attempt_id": row["attempt_id"],
            "sequence": row["sequence"],
            "manifest_artifact_id": row["manifest_artifact_id"],
            "manifest_checksum": row["manifest_checksum"],
            "state": "CORRUPT" if corrupt else "COMMITTED",
            "created_at": row["created_at"],
        }
    )


def spec_and_template(session, job_id):
    spec = session.execute(select(s.job_specs).where(s.job_specs.c.job_id == job_id)).mappings()
    spec = dict(spec.one())
    template = (
        session.execute(
            select(s.template_versions).where(
                s.template_versions.c.template_id == spec["template_id"],
                s.template_versions.c.version == spec["template_version"],
            )
        )
        .mappings()
        .one()
    )
    return spec, dict(template)


def expected_provenance(session, job, spec, template, *, attempt_id, fence):
    session_id = session.execute(
        select(s.logical_sessions.c.session_id).where(s.logical_sessions.c.job_id == job["job_id"])
    ).scalar_one()
    input_checksum = session.execute(
        select(s.artifacts.c.checksum).where(
            s.artifacts.c.artifact_id == spec["input_artifact_id"],
            s.artifacts.c.tenant_id == job["tenant_id"],
        )
    ).scalar_one()
    return checkpoint_provenance(
        job=job,
        spec=spec,
        template=template,
        session_id=session_id,
        input_checksum=input_checksum,
        attempt_id=attempt_id,
        fence=fence,
    )


def worker_architecture(session, worker_id):
    return session.execute(
        select(s.worker_inventories.c.architecture)
        .join(
            s.workers,
            (s.workers.c.worker_id == s.worker_inventories.c.worker_id)
            & (s.workers.c.current_inventory_version == s.worker_inventories.c.inventory_version),
        )
        .where(s.workers.c.worker_id == worker_id)
    ).scalar_one_or_none()


class CheckpointMixin:
    def reserve_checkpoint(self, *, credential, attempt_id, callback_id, payload_hash, authority):
        def operation(session):
            receipt, replay, rows = self._callback(
                session,
                authority=authority,
                credential=credential,
                attempt_id=attempt_id,
                callback_id=callback_id,
                payload_hash=payload_hash,
                operation_id="workerReserveCheckpoint",
            )
            if replay is not None:
                return replay
            if self._mode(session) == "WRITE_FROZEN":
                _conflict("Writes are frozen")
            job, attempt, lease, allocation, grant = rows
            now = clock_timestamp(session)
            self._live_authority(job, attempt, lease, allocation, now)
            # CHECKPOINT_FOR_PAUSE and PAUSING belong to B15; B14 checkpoints only RUN.
            if (
                job["state"] != "RUNNING"
                or job["desired_state"] != "RUNNING"
                or job["recovery_intent"] is not None
                or attempt["state"] != "RUNNING"
                or attempt["execution_intent"] != "RUN"
            ):
                _conflict("Attempt cannot reserve a checkpoint now")
            _, template = spec_and_template(session, job["job_id"])
            if not template["checkpointable"]:
                _conflict("Template is not checkpointable")
            reserved = session.execute(
                select(s.checkpoint_reservations.c.checkpoint_id)
                .where(
                    s.checkpoint_reservations.c.attempt_id == attempt_id,
                    s.checkpoint_reservations.c.state == "RESERVED",
                )
                .with_for_update()
            ).first()
            result = session.execute(
                select(s.result_reservations.c.result_id).where(
                    s.result_reservations.c.job_id == job["job_id"],
                    s.result_reservations.c.state == "ACTIVE",
                )
            ).first()
            # A second reserve never abandons the first; only publish, failure,
            # revoke or adoption may end or rebind a RESERVED identity.
            if reserved is not None or result is not None:
                _conflict("A checkpoint or result reservation is already active")
            sequence = job["checkpoint_sequence"] + 1
            checkpoint_id = new_uuid7()
            session.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == job["job_id"])
                .values(checkpoint_sequence=sequence)
            )
            session.execute(
                insert(s.checkpoint_reservations).values(
                    checkpoint_id=checkpoint_id,
                    tenant_id=job["tenant_id"],
                    job_id=job["job_id"],
                    attempt_id=attempt_id,
                    authority_grant_id=grant["grant_id"],
                    sequence=sequence,
                    callback_id=callback_id,
                    state="RESERVED",
                    reserved_at=now,
                )
            )
            session.execute(
                update(s.attempts)
                .where(s.attempts.c.attempt_id == attempt_id)
                .values(state="CHECKPOINTING", updated_at=now)
            )
            body = json_wire_value(
                {
                    "callback_id": callback_id,
                    "checkpoint_id": checkpoint_id,
                    "job_id": job["job_id"],
                    "attempt_id": attempt_id,
                    "sequence": sequence,
                    "reserved_at": now,
                }
            )
            self._complete_callback(session, receipt, body)
            return body

        return run_transaction(self.session_factory, operation)

    def _prefetch_checkpoint_manifest(self, *, credential, authority, callback_id, artifact_id):
        """Read bounded manifest bytes before the publish transaction."""

        def operation(session):
            self._mode(session)
            self._worker_auth(session, worker_id=authority.worker_id, credential=credential)
            acknowledgment = session.execute(
                select(s.callback_receipts.c.acknowledgment).where(
                    s.callback_receipts.c.worker_id == authority.worker_id,
                    s.callback_receipts.c.operation_id == "workerPublishCheckpoint",
                    s.callback_receipts.c.callback_id == callback_id,
                )
            ).scalar_one_or_none()
            if acknowledgment:
                return None
            tenant_id = session.execute(
                select(s.attempts.c.tenant_id).where(
                    s.attempts.c.attempt_id == authority.attempt_id,
                    s.attempts.c.worker_id == authority.worker_id,
                )
            ).scalar_one_or_none()
            row = (
                session.execute(
                    select(s.artifacts).where(
                        s.artifacts.c.artifact_id == artifact_id,
                        s.artifacts.c.tenant_id == tenant_id,
                        s.artifacts.c.state == "COMMITTED",
                        s.artifacts.c.kind == "CHECKPOINT_MANIFEST",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["size_bytes"] > CHECKPOINT_MANIFEST_MAX_BYTES:
                return None
            return dict(row)

        row = run_transaction(self.session_factory, operation)
        if row is None:
            return None
        if self.artifact_store is None:
            raise _storage_unavailable()
        try:
            reader = self.artifact_store.open(row["blob_key"])
            try:
                raw = reader.read(CHECKPOINT_MANIFEST_MAX_BYTES + 1)
            finally:
                reader.close()
        except ArtifactError as exc:
            raise _storage_unavailable() from exc
        # Bytes that disagree with committed metadata are a storage fault, not a
        # worker defect, so they fail closed without rejecting the reservation.
        if len(raw) != row["size_bytes"] or (
            "sha256:" + hashlib.sha256(raw).hexdigest() != row["checksum"]
        ):
            raise _storage_unavailable("Artifact storage failed integrity validation")
        return row["artifact_id"], raw

    def _verified_checkpoint_graph(
        self, session, *, job, attempt, grant, reservation, request, prefetched
    ):
        """Return (manifest artifact, file artifacts, parsed manifest) or raise a defect."""
        artifact = (
            session.execute(
                select(s.artifacts)
                .where(
                    s.artifacts.c.artifact_id == request.manifest_artifact_id,
                    s.artifacts.c.tenant_id == job["tenant_id"],
                )
                .with_for_update(read=True)
            )
            .mappings()
            .one_or_none()
        )
        if (
            artifact is None
            or artifact["state"] != "COMMITTED"
            or artifact["kind"] != "CHECKPOINT_MANIFEST"
            or artifact["size_bytes"] > CHECKPOINT_MANIFEST_MAX_BYTES
        ):
            raise CheckpointManifestError(
                "CHECKPOINT_MANIFEST_INVALID", "Checkpoint manifest artifact is invalid"
            )
        try:
            self._upload_from_attempt(
                session, artifact=artifact, job=job, attempt=attempt, grant=grant
            )
        except ApplicationError as exc:
            raise CheckpointManifestError(
                "CHECKPOINT_MANIFEST_INVALID", "Checkpoint manifest has no upload lineage"
            ) from exc
        if prefetched is None or prefetched[0] != artifact["artifact_id"]:
            # The artifact committed after the prefetch read; retry reads it.
            raise _storage_unavailable()
        spec, template = spec_and_template(session, job["job_id"])
        provenance = expected_provenance(
            session, job, spec, template, attempt_id=attempt["attempt_id"], fence=job["job_fence"]
        )
        architecture = worker_architecture(session, attempt["worker_id"])
        if architecture is None:
            # A restarted incarnation has not reported inventory yet; that is
            # transient, never a manifest defect, so nothing commits here.
            raise _storage_unavailable(_INVENTORY_UNREPORTED)
        compatibility = expected_compatibility(template, architecture)
        entries = validate_checkpoint_manifest(
            request.manifest,
            raw=prefetched[1],
            artifact=dict(artifact),
            checkpoint_id=reservation["checkpoint_id"],
            sequence=reservation["sequence"],
            provenance=provenance,
            compatibility=compatibility,
            parameters=spec["canonical_spec"].get("parameters") or {},
        )
        rows = {
            str(row["artifact_id"]): dict(row)
            for row in session.execute(
                select(s.artifacts)
                .where(
                    s.artifacts.c.artifact_id.in_([UUID(e["artifact_id"]) for e in entries]),
                    s.artifacts.c.tenant_id == job["tenant_id"],
                )
                .order_by(s.artifacts.c.artifact_id)
                .with_for_update(read=True)
            ).mappings()
        }
        files = check_file_artifacts(entries, rows, tenant_id=job["tenant_id"])
        for row in files:
            try:
                self._upload_from_attempt(
                    session, artifact=row, job=job, attempt=attempt, grant=grant
                )
            except ApplicationError as exc:
                raise CheckpointManifestError(
                    "CHECKPOINT_FILES_INVALID", "Checkpoint file has no upload lineage"
                ) from exc
        return artifact, list(zip(entries, files, strict=True)), request.manifest

    def publish_checkpoint(self, *, credential, attempt_id, callback_id, payload_hash, request):
        authority = request.authority
        prefetched = self._prefetch_checkpoint_manifest(
            credential=credential,
            authority=authority,
            callback_id=callback_id,
            artifact_id=request.manifest_artifact_id,
        )

        def operation(session):
            receipt, replay, rows = self._callback(
                session,
                authority=authority,
                credential=credential,
                attempt_id=attempt_id,
                callback_id=callback_id,
                payload_hash=payload_hash,
                operation_id="workerPublishCheckpoint",
            )
            if replay is not None:
                return replay
            if self._mode(session) == "WRITE_FROZEN":
                _conflict("Writes are frozen")
            job, attempt, lease, allocation, grant = rows
            now = clock_timestamp(session)
            self._live_authority(job, attempt, lease, allocation, now)
            if (
                job["state"] != "RUNNING"
                or job["desired_state"] != "RUNNING"
                or attempt["state"] != "CHECKPOINTING"
                or attempt["execution_intent"] != "RUN"
            ):
                _conflict("Attempt has no checkpoint in progress")
            reservation = (
                session.execute(
                    select(s.checkpoint_reservations)
                    .where(
                        s.checkpoint_reservations.c.attempt_id == attempt_id,
                        s.checkpoint_reservations.c.state == "RESERVED",
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if reservation is None or reservation["authority_grant_id"] != grant["grant_id"]:
                _conflict("Checkpoint reservation is not held by this Authority")
            try:
                artifact, files, manifest = self._verified_checkpoint_graph(
                    session,
                    job=job,
                    attempt=attempt,
                    grant=grant,
                    reservation=reservation,
                    request=request,
                    prefetched=prefetched,
                )
            except CheckpointManifestError as exc:
                # A deterministic defect ends this identity; its sequence is never reused.
                session.execute(
                    update(s.checkpoint_reservations)
                    .where(
                        s.checkpoint_reservations.c.checkpoint_id == reservation["checkpoint_id"]
                    )
                    .values(state="REJECTED", ended_at=now)
                )
                session.execute(
                    update(s.attempts)
                    .where(s.attempts.c.attempt_id == attempt_id)
                    .values(state="RUNNING", updated_at=now)
                )
                _event(
                    session, job, authority.worker_id, now, "CHECKPOINT_REJECTED", exc.reason_code
                )
                body = {"error": {"code": exc.code, "message": exc.message}}
                self._complete_callback(session, receipt, body)
                return body
            checkpoint = {
                "checkpoint_id": reservation["checkpoint_id"],
                "tenant_id": job["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": attempt_id,
                "sequence": reservation["sequence"],
                "manifest_artifact_id": artifact["artifact_id"],
                "manifest_checksum": artifact["checksum"],
                "provenance": manifest["provenance"],
                "compatibility": manifest["compatibility"],
                "state": "COMMITTED",
                "created_at": now,
            }
            # The reservation must be COMMITTED before the deferred recognized-record check.
            session.execute(
                update(s.checkpoint_reservations)
                .where(s.checkpoint_reservations.c.checkpoint_id == reservation["checkpoint_id"])
                .values(state="COMMITTED", ended_at=now)
            )
            session.execute(insert(s.checkpoints).values(**checkpoint))
            references = [(artifact["artifact_id"], "CHECKPOINT_MANIFEST", "manifest")]
            references += [
                (row["artifact_id"], "CHECKPOINT_FILE", entry["logical_name"])
                for entry, row in files
            ]
            for artifact_id, purpose, name in references:
                session.execute(
                    insert(s.artifact_references).values(
                        tenant_id=job["tenant_id"],
                        artifact_id=artifact_id,
                        owner_type="CHECKPOINT",
                        owner_id=reservation["checkpoint_id"],
                        purpose=purpose,
                        logical_name=name,
                    )
                )
            session.execute(
                update(s.attempts)
                .where(s.attempts.c.attempt_id == attempt_id)
                .values(state="RUNNING", updated_at=now)
            )
            _event(
                session,
                job,
                authority.worker_id,
                now,
                "CHECKPOINT_COMMITTED",
                "CHECKPOINT_COMMITTED",
            )
            body = checkpoint_record(checkpoint)
            self._complete_callback(session, receipt, body)
            return body

        body = run_transaction(self.session_factory, operation)
        if "error" in body:
            raise ApplicationError(
                code=body["error"]["code"], status=422, message=body["error"]["message"]
            )
        return body
