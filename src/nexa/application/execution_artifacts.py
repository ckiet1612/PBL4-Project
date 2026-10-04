"""Attempt scoped adapters over B07 durable streamed artifact storage."""

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nexa.application import storage_pressure
from nexa.application.artifact_service import ArtifactService
from nexa.application.errors import ApplicationError
from nexa.domain import workload_adapters
from nexa.infrastructure.artifacts.store import ArtifactError, BlobRange
from nexa.infrastructure.persistence.locking import clock_timestamp
from nexa.infrastructure.persistence.schema import (
    artifact_reference_guards,
    artifact_references,
    artifacts,
    attempt_authority_grants,
    attempts,
    idempotency_records,
    job_specs,
    template_versions,
    upload_sessions,
)
from nexa.infrastructure.persistence.transactions import run_transaction

_CHUNK_MEDIA_TYPES = frozenset(media for media, _ in workload_adapters.CHUNK_MEDIA_TYPES.values())


@dataclass(frozen=True)
class WorkerUploadPrincipal:
    user_id: UUID


class AttemptArtifactService(ArtifactService):
    _upload_operation = "workerUploadAttemptArtifact"
    # Worker uploads (checkpoint/result/chunk) are refused only at the critical watermark.
    _storage_operation = storage_pressure.WORKER_UPLOAD

    def __init__(self, base, worker, credential, authority, kind=None, media_type=None):
        super().__init__(base.session_factory, base.settings, base.identity, base.store)
        self.worker = worker
        self.credential = credential
        self.authority = authority
        # Checkpoint bytes are accepted only inside a RESERVED checkpoint window
        # and result bytes only outside it, so one upload cannot straddle both.
        self.kind = kind
        self.media_type = media_type
        self._upload_provenance = {}
        self._requested_media = None

    def _upload_request_scope(self):
        # Keep adoption replay stable while separating different attempts.  The
        # current incarnation and grant are still checked by _authorize, so a
        # stale worker cannot use this stable idempotency scope.
        return {
            "worker_id": str(self.authority.worker_id),
            "attempt_id": str(self.authority.attempt_id),
            "allocation_id": str(self.authority.allocation_id),
            "lease_id": str(self.authority.lease_id),
            "job_fence": self.authority.job_fence,
        }

    def _upload_receipt_identity(self):
        return self.authority.model_dump(mode="json")

    def _validate_upload_replay(self, session, record_id):
        receipt = (
            session.execute(
                select(idempotency_records).where(idempotency_records.c.idempotency_id == record_id)
            )
            .mappings()
            .one()
        )
        upload = (
            session.execute(
                select(upload_sessions).where(upload_sessions.c.idempotency_id == record_id)
            )
            .mappings()
            .one_or_none()
        )
        if upload is None or upload["state"] != "COMMITTED":
            raise ApplicationError(
                code="state_conflict", status=409, message="Upload replay has no committed lineage"
            )
        original = receipt["original_authority"]
        if original is None or any(
            original.get(field) != value
            for field, value in self._upload_request_scope().items()
            if field != "worker_id"
        ):
            raise ApplicationError(
                code="idempotency_conflict", status=409, message="Upload replay Authority changed"
            )
        if (
            original.get("worker_id") != str(self.authority.worker_id)
            or original.get("worker_incarnation_id") is None
        ):
            raise ApplicationError(
                code="idempotency_conflict", status=409, message="Upload replay worker changed"
            )
        if (
            upload["job_id"] != self._upload_provenance["job_id"]
            or upload["attempt_id"] != self.authority.attempt_id
        ):
            raise ApplicationError(
                code="idempotency_conflict", status=409, message="Upload replay attempt changed"
            )
        grants = {
            row["grant_id"]: row
            for row in session.execute(
                select(attempt_authority_grants).where(
                    attempt_authority_grants.c.attempt_id == self.authority.attempt_id
                )
            ).mappings()
        }
        origin = grants.get(upload["authority_grant_id"])
        current_id = self._upload_provenance["authority_grant_id"]
        current = grants.get(current_id)
        if (
            origin is None
            or current is None
            or origin["worker_incarnation_id"] != UUID(original["worker_incarnation_id"])
        ):
            raise ApplicationError(
                code="idempotency_conflict", status=409, message="Upload replay grant changed"
            )
        visited = set()
        while current_id is not None and current_id not in visited:
            if current_id == origin["grant_id"]:
                return
            visited.add(current_id)
            member = grants.get(current_id)
            if member is None:
                break
            current_id = member["predecessor_grant_id"]
        raise ApplicationError(
            code="state_conflict", status=409, message="Upload replay lacks adoption lineage"
        )

    def _upload_metadata(self, kind, media_type):
        # Kind-level union before the transaction; the job's adapter narrows it in _authorize.
        allowed = set()
        for adapter in workload_adapters.DESCRIPTORS:
            allowed |= adapter.upload_media_types.get(kind, frozenset())
        if (
            kind != self.kind
            or media_type not in allowed
            or (self.media_type is not None and media_type != self.media_type)
        ):
            raise ArtifactError(
                "validation_failed", "Artifact kind or media is unavailable for CPU execution"
            )
        self._requested_media = (kind, media_type)
        return kind, media_type

    def _job_adapter(self, session, job_id):
        adapter_row = (
            session.execute(
                select(template_versions.c.adapter_id, template_versions.c.adapter_version)
                .select_from(
                    job_specs.join(
                        template_versions,
                        (template_versions.c.template_id == job_specs.c.template_id)
                        & (template_versions.c.version == job_specs.c.template_version),
                    )
                )
                .where(job_specs.c.job_id == job_id)
            )
            .mappings()
            .one_or_none()
        )
        return (
            None
            if adapter_row is None
            else workload_adapters.descriptor(
                adapter_row["adapter_id"], adapter_row["adapter_version"]
            )
        )

    def _upload_phases(self, adapter):
        if str(self.kind).startswith("CHECKPOINT_"):
            return {"CHECKPOINTING"}
        if adapter is not None and adapter.chunked:
            kind, media_type = self.kind, self.media_type
            # B16-R19: chunk files and the chunk-output manifest are bound by the chunked
            # checkpoint cycle inside the reserved window as well as by the result cycle;
            # recognition stays in the fenced publish/complete transaction.
            if kind == "CHUNK_OUTPUT_MANIFEST" or (
                kind == "RESULT_FILE"
                and media_type
                in {media for media, _ in workload_adapters.CHUNK_MEDIA_TYPES.values()}
            ):
                return {"RUNNING", "CHECKPOINTING"}
        return {"RUNNING"}

    def _require_adapter_media(self, adapter):
        if self._requested_media is None:
            return
        kind, media_type = self._requested_media
        if adapter is None or media_type not in adapter.upload_media_types.get(kind, ()):
            raise ApplicationError(
                code="validation_failed",
                status=422,
                message="Artifact kind or media is unavailable for this workload adapter",
            )

    def _authorize(self, session, principal, tenant_id, *, write, require_running=True):
        self.worker._mode(session)
        worker = self.worker._worker_auth(
            session, worker_id=self.authority.worker_id, credential=self.credential
        )
        self.worker._require_current_incarnation(
            session, worker, self.authority.worker_incarnation_id, require_reconciling=False
        )
        job, attempt, lease, allocation, grant = self.worker._authority_rows(
            session, self.authority, worker_id=self.authority.worker_id
        )
        self.worker._live_authority(job, attempt, lease, allocation, clock_timestamp(session))
        adapter = self._job_adapter(session, job["job_id"])
        if job["tenant_id"] != tenant_id or (
            require_running and attempt["state"] not in self._upload_phases(adapter)
        ):
            raise ApplicationError(
                code="state_conflict", status=409, message="Upload execution scope mismatch"
            )
        self._require_adapter_media(adapter)
        self._upload_provenance = {
            "job_id": job["job_id"],
            "attempt_id": attempt["attempt_id"],
            "authority_grant_id": grant["grant_id"],
        }
        return principal

    def _upload_actor(self, live):
        return "WORKER", str(self.authority.worker_id)

    def _commit_upload_reference(self, session, tenant_id, upload_id, artifact_id):
        session.execute(
            pg_insert(artifact_reference_guards)
            .values(tenant_id=tenant_id, artifact_id=artifact_id)
            .on_conflict_do_nothing()
        )
        session.execute(
            insert(artifact_references).values(
                tenant_id=tenant_id,
                artifact_id=artifact_id,
                owner_type="UPLOAD_SESSION",
                owner_id=upload_id,
                purpose="ATTEMPT_UPLOAD",
                logical_name="uploaded",
            )
        )

    def download_execution_artifact(self, artifact_id: UUID, range_header: str | None = None):
        """Read only an artifact present in the claimed execution graph."""

        def metadata_operation(session):
            attempt_tenant = session.execute(
                select(attempts.c.tenant_id).where(
                    attempts.c.attempt_id == self.authority.attempt_id
                )
            ).scalar_one_or_none()
            if attempt_tenant is None:
                raise ApplicationError(
                    code="resource_not_found",
                    status=404,
                    message="Execution artifact was not found",
                )
            self._authorize(
                session,
                WorkerUploadPrincipal(self.authority.worker_id),
                attempt_tenant,
                write=False,
                require_running=False,
            )
            attempt = (
                session.execute(
                    select(attempts).where(attempts.c.attempt_id == self.authority.attempt_id)
                )
                .mappings()
                .one()
            )
            context = attempt.get("execution_context") or {}
            graph = []
            graph.extend(context.get("input_artifacts") or [])
            checkpoint = context.get("restore_checkpoint") or {}
            files = checkpoint.get("files") or []
            if isinstance(files, list):
                graph.extend(files)
            # B16-R21: the recognized chunk files the Attempt carries forward are read, not
            # recomputed; the claim pins their bytes but not their kind or media type.
            recognized = context.get("recognized_chunks") or []
            if isinstance(recognized, list):
                graph.extend({**item, "kind": "RESULT_FILE"} for item in recognized)
            expected = next(
                (item for item in graph if str(item.get("artifact_id")) == str(artifact_id)), None
            )
            if expected is None:
                raise ApplicationError(
                    code="resource_not_found",
                    status=404,
                    message="Execution artifact is outside the claimed graph",
                )
            row = (
                session.execute(
                    select(artifacts).where(
                        artifacts.c.artifact_id == artifact_id,
                        artifacts.c.tenant_id == attempt_tenant,
                        artifacts.c.state == "COMMITTED",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ApplicationError(
                    code="resource_not_found",
                    status=404,
                    message="Execution artifact was not found",
                )
            chunk = "chunk_id" in expected
            for field in ("kind", "media_type", "size_bytes", "checksum"):
                if chunk and field == "media_type":
                    matches = row[field] in _CHUNK_MEDIA_TYPES
                else:
                    matches = expected.get(field) == row[field]
                if not matches:
                    raise ApplicationError(
                        code="dependency_unavailable",
                        status=503,
                        message="Execution artifact metadata failed integrity validation",
                        retry_after=1,
                    )
            return dict(row)

        row = run_transaction(self.session_factory, metadata_operation)
        try:
            stat = self.store.inspect(row["blob_key"])
        except ArtifactError as exc:
            raise ApplicationError(
                code="dependency_unavailable",
                status=503,
                message="Artifact storage is unavailable",
                retry_after=1,
            ) from exc
        if stat.size_bytes != row["size_bytes"] or stat.checksum != row["checksum"]:
            raise ApplicationError(
                code="dependency_unavailable",
                status=503,
                message="Artifact storage failed integrity validation",
                retry_after=1,
            )
        status = 200
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Artifact-Media-Type": row["media_type"],
            "ETag": f'"{row["checksum"]}"',
        }
        byte_range = None
        if range_header is not None:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
            if match is None:
                raise ApplicationError(
                    code="validation_failed", status=400, message="Range header is invalid"
                )
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else row["size_bytes"] - 1
            if start >= row["size_bytes"] or end < start:
                raise ApplicationError(
                    code="validation_failed",
                    status=400,
                    message="Range header is outside the artifact",
                )
            end = min(end, row["size_bytes"] - 1)
            byte_range = BlobRange(start, end)
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{row['size_bytes']}"
            headers["Content-Length"] = str(end - start + 1)
        else:
            headers["Content-Length"] = str(row["size_bytes"])
        try:
            reader = self.store.open(row["blob_key"], byte_range)
        except ArtifactError as exc:
            raise ApplicationError(
                code="dependency_unavailable",
                status=503,
                message="Artifact storage is unavailable",
                retry_after=1,
            ) from exc
        from nexa.application.artifact_service import ArtifactDownload

        return ArtifactDownload(reader, status, headers)

    def tenant_id(self):
        from nexa.infrastructure.persistence.schema import attempts
        from nexa.infrastructure.persistence.transactions import run_transaction

        def operation(session):
            tenant = session.execute(
                select(attempts.c.tenant_id).where(
                    attempts.c.attempt_id == self.authority.attempt_id
                )
            ).scalar_one_or_none()
            if tenant is None:
                raise ApplicationError(
                    code="state_conflict", status=409, message="Attempt unavailable"
                )
            self._authorize(
                session, WorkerUploadPrincipal(self.authority.worker_id), tenant, write=True
            )
            return tenant

        return run_transaction(self.session_factory, operation)
