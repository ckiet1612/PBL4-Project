"""Attempt scoped adapters over B07 durable streamed artifact storage."""

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nexa.application.artifact_service import ArtifactService
from nexa.application.errors import ApplicationError
from nexa.infrastructure.artifacts.store import ArtifactError, BlobRange
from nexa.infrastructure.persistence.locking import clock_timestamp
from nexa.infrastructure.persistence.schema import (
    artifact_reference_guards,
    artifact_references,
    artifacts,
    attempt_authority_grants,
    attempts,
    idempotency_records,
    upload_sessions,
)
from nexa.infrastructure.persistence.transactions import run_transaction


@dataclass(frozen=True)
class WorkerUploadPrincipal:
    user_id: UUID


class AttemptArtifactService(ArtifactService):
    _upload_operation = "workerUploadAttemptArtifact"

    def __init__(self, base, worker, credential, authority):
        super().__init__(base.session_factory, base.settings, base.identity, base.store)
        self.worker = worker
        self.credential = credential
        self.authority = authority
        self._upload_provenance = {}

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

    @staticmethod
    def _upload_metadata(kind, media_type):
        allowed = {
            "RESULT_FILE": {"application/vnd.nexa.cpu-iterative-result+json", "application/json"},
            "RESULT_MANIFEST": {"application/json"},
        }
        if media_type not in allowed.get(kind, set()):
            raise ArtifactError(
                "validation_failed", "Artifact kind or media is unavailable for CPU execution"
            )
        return kind, media_type

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
        if job["tenant_id"] != tenant_id or (require_running and attempt["state"] != "RUNNING"):
            raise ApplicationError(
                code="state_conflict", status=409, message="Upload execution scope mismatch"
            )
        self._upload_provenance = {
            "job_id": job["job_id"],
            "attempt_id": attempt["attempt_id"],
            "authority_grant_id": grant["grant_id"],
        }
        return principal

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
            for field in ("kind", "media_type", "size_bytes", "checksum"):
                if expected.get(field) != row[field]:
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
