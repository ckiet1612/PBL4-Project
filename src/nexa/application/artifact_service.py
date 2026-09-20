from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from nexa.application.errors import ApplicationError
from nexa.application.idempotency import begin_idempotency, complete_idempotency
from nexa.application.json_codec import jcs_request_hash, json_wire_value
from nexa.config import Settings
from nexa.domain.identity import (
    AuthorizationError,
    CredentialKind,
    Principal,
    TokenScope,
    require_tenant_membership,
)
from nexa.infrastructure.artifacts.policy import (
    validate_public_artifact_metadata,
)
from nexa.infrastructure.artifacts.store import (
    ArtifactError,
    BlobRange,
    BoundedReader,
    DurableBlob,
    FilesystemArtifactStore,
    GcToken,
)
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import transaction_timestamp
from nexa.infrastructure.persistence.schema import (
    artifact_references,
    artifact_storage_counters,
    artifacts,
    audit_records,
    idempotency_records,
    upload_sessions,
)
from nexa.infrastructure.persistence.transactions import run_transaction
from nexa.infrastructure.security import CursorCodec, CursorError

_CHECKSUM_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_READABLE_ARTIFACT_KINDS = frozenset(
    {
        "INPUT",
        "DATASET",
        "MODEL",
        "CHECKPOINT_FILE",
        "CHECKPOINT_MANIFEST",
        "RESULT_FILE",
        "RESULT_MANIFEST",
        "CHUNK_OUTPUT_MANIFEST",
        "LOG",
    }
)


@dataclass(frozen=True, slots=True)
class ArtifactUploadResult:
    body: dict[str, Any]
    status: int
    headers: dict[str, str]
    orphan_blob: DurableBlob | None = None


@dataclass(frozen=True, slots=True)
class ArtifactDownload:
    reader: BoundedReader
    status: int
    headers: dict[str, str]


class ArtifactService:
    def __init__(
        self, session_factory, settings: Settings, identity, store: FilesystemArtifactStore
    ):
        self.session_factory = session_factory
        self.settings = settings
        self.identity = identity
        self.store = store

    @staticmethod
    def _view(row: Any) -> dict[str, Any]:
        return json_wire_value(
            {
                "artifact_id": row["artifact_id"],
                "tenant_id": row["tenant_id"],
                "kind": row["kind"],
                "media_type": row["media_type"],
                "size_bytes": row["size_bytes"],
                "checksum": row["checksum"],
                "state": "COMMITTED",
                "version": row["version"],
                "created_at": row["created_at"],
            }
        )

    def _authorize(self, session: Session, principal: Principal, tenant_id: UUID, *, write: bool):
        live = self.identity.revalidate_principal(session, principal)
        try:
            require_tenant_membership(live, tenant_id)
        except AuthorizationError as exc:
            raise ApplicationError(
                code="permission_denied", status=403, message="Active tenant membership is required"
            ) from exc
        if live.credential_kind is CredentialKind.CLI:
            required = TokenScope.ARTIFACTS_WRITE if write else TokenScope.ARTIFACTS_READ
            if not live.has_scope(required):
                raise ApplicationError(
                    code="permission_denied",
                    status=403,
                    message=f"The exact {required.value} scope is required",
                )
        return live

    @staticmethod
    def _validate_checksum(checksum: str) -> None:
        if not _CHECKSUM_PATTERN.fullmatch(checksum):
            raise ApplicationError(
                code="validation_failed", status=422, message="Artifact checksum is invalid"
            )

    def _counter_lock(self, session: Session, tenant_id: UUID) -> Any:
        session.execute(
            pg_insert(artifact_storage_counters)
            .values(tenant_id=tenant_id)
            .on_conflict_do_nothing(index_elements=[artifact_storage_counters.c.tenant_id])
        )
        row = (
            session.execute(
                select(artifact_storage_counters)
                .where(artifact_storage_counters.c.tenant_id == tenant_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApplicationError(
                code="resource_not_found", status=404, message="Tenant was not found"
            )
        return row

    def _check_disk(self, expected_size: int) -> None:
        try:
            _total, _free, used_percent = self.store.disk_usage(expected_size)
        except ArtifactError as exc:
            raise ApplicationError(
                code=exc.code,
                status=503,
                message="Artifact storage capacity cannot be checked",
                retry_after=1,
            ) from exc
        if used_percent >= self.settings.storage_high_watermark_percent:
            raise ApplicationError(
                code="storage_pressure",
                status=503,
                message="Artifact storage is under pressure",
                retry_after=1,
            )

    def _abort_upload(
        self,
        upload_id: UUID,
        tenant_id: UUID,
        expected_size: int,
        bytes_received: int,
        record_id: UUID,
        failure: ApplicationError,
    ) -> None:
        def operation(session: Session) -> None:
            record = (
                session.execute(
                    select(idempotency_records)
                    .where(idempotency_records.c.idempotency_id == record_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if record["state"] == "COMPLETED":
                return
            counter = self._counter_lock(session, tenant_id)
            row = (
                session.execute(
                    select(upload_sessions)
                    .where(
                        upload_sessions.c.upload_id == upload_id,
                        upload_sessions.c.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["state"] != "ACTIVE":
                return
            reserved = int(counter["reserved_bytes"])
            if reserved < expected_size:
                raise ApplicationError(
                    code="dependency_unavailable",
                    status=503,
                    message="Artifact reservation state is inconsistent",
                    retry_after=1,
                )
            now = transaction_timestamp(session)
            session.execute(
                update(artifact_storage_counters)
                .where(artifact_storage_counters.c.tenant_id == tenant_id)
                .values(
                    reserved_bytes=artifact_storage_counters.c.reserved_bytes - expected_size,
                    updated_at=now,
                    version=artifact_storage_counters.c.version + 1,
                )
            )
            session.execute(
                update(upload_sessions)
                .where(upload_sessions.c.upload_id == upload_id)
                .values(state="ABORTED", bytes_received=bytes_received, updated_at=now)
            )
            complete_idempotency(
                session,
                record_id,
                status=failure.status,
                body={"code": failure.code, "message": failure.message},
                headers=(
                    {"Retry-After": str(failure.retry_after)}
                    if failure.retry_after is not None
                    else {}
                ),
            )

        run_transaction(self.session_factory, operation)

    async def upload(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        idempotency_key: str,
        kind: str,
        media_type: str,
        expected_size: int,
        expected_checksum: str,
        chunks: AsyncIterable[bytes],
        content_length: int | None = None,
    ) -> ArtifactUploadResult:
        if expected_size < 0:
            raise ApplicationError(
                code="validation_failed", status=422, message="Artifact size is invalid"
            )
        if expected_size > self.settings.artifact_max_file_bytes:
            raise ApplicationError(
                code="payload_too_large",
                status=413,
                message="Artifact size exceeds the configured limit",
            )
        if content_length is not None and content_length != expected_size:
            raise ApplicationError(
                code="validation_failed",
                status=422,
                message="Content-Length does not match X-Artifact-Size",
            )
        self._validate_checksum(expected_checksum)
        try:
            normalized_kind, normalized_media = validate_public_artifact_metadata(kind, media_type)
        except ArtifactError as exc:
            raise ApplicationError(code=exc.code, status=422, message=exc.message) from exc
        request_hash = jcs_request_hash(
            {
                "tenant_id": str(tenant_id),
                "operation": "uploadArtifact",
                "kind": normalized_kind,
                "media_type": normalized_media,
                "size_bytes": expected_size,
                "checksum": expected_checksum,
            }
        )
        upload_id = new_uuid7()
        staged_key = f"staging/{upload_id.hex}"

        def begin_operation(session: Session) -> tuple[dict[str, Any] | None, UUID]:
            live = self._authorize(session, principal, tenant_id, write=True)
            now = transaction_timestamp(session)
            idempotency = begin_idempotency(
                session,
                context=str(tenant_id),
                principal_id=str(live.user_id),
                operation_id="uploadArtifact",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=now + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if idempotency.replay is not None:
                return dict(idempotency.replay.body or {}), idempotency.record_id
            self._check_disk(expected_size)
            counter = self._counter_lock(session, tenant_id)
            if (
                int(counter["committed_bytes"]) + int(counter["reserved_bytes"]) + expected_size
                > self.settings.tenant_artifact_quota_bytes
            ):
                raise ApplicationError(
                    code="quota_exceeded",
                    status=429,
                    message="Tenant artifact quota exceeded",
                    retry_after=1,
                )
            session.execute(
                update(artifact_storage_counters)
                .where(artifact_storage_counters.c.tenant_id == tenant_id)
                .values(
                    reserved_bytes=artifact_storage_counters.c.reserved_bytes + expected_size,
                    updated_at=now,
                    version=artifact_storage_counters.c.version + 1,
                )
            )
            session.execute(
                upload_sessions.insert().values(
                    upload_id=upload_id,
                    tenant_id=tenant_id,
                    expected_size_bytes=expected_size,
                    expected_checksum=expected_checksum,
                    staged_key=staged_key,
                    idempotency_id=idempotency.record_id,
                    bytes_received=0,
                    expires_at=now + timedelta(seconds=self.settings.staging_ttl_seconds),
                    state="ACTIVE",
                )
            )
            return None, idempotency.record_id

        try:
            replay_body, record_id = run_transaction(self.session_factory, begin_operation)
        except BaseException:
            raise
        if replay_body is not None:
            # Fetching the replay headers is deliberately done from the durable
            # record so callers receive the exact original response.
            def replay_operation(session: Session) -> ArtifactUploadResult:
                row = (
                    session.execute(
                        select(idempotency_records).where(
                            idempotency_records.c.idempotency_id == record_id
                        )
                    )
                    .mappings()
                    .one()
                )
                body = dict(row["response_body"] or {})
                status = int(row["response_status"])
                headers = dict(row["response_headers"] or {})
                if status >= 400:
                    raise ApplicationError(
                        code=body["code"],
                        status=status,
                        message=body["message"],
                        retry_after=int(headers["Retry-After"])
                        if "Retry-After" in headers
                        else None,
                    )
                return ArtifactUploadResult(body, status, headers)

            return run_transaction(self.session_factory, replay_operation)

        try:
            handle = self.store.begin_staging(
                owner=str(tenant_id),
                expected_size=expected_size,
                expected_checksum=expected_checksum,
                media_type=normalized_media,
                upload_id=upload_id.hex,
            )
        except ArtifactError as exc:
            status = 413 if exc.code == "payload_too_large" else 503
            failure = ApplicationError(
                code=exc.code,
                status=status,
                message=exc.message,
                retry_after=1 if status == 503 else None,
            )
            self._abort_upload(upload_id, tenant_id, expected_size, 0, record_id, failure)
            raise failure from exc

        received = 0
        try:
            async for chunk in chunks:
                progress = self.store.append(handle, chunk)
                received = progress.received_bytes
                if received > expected_size:
                    raise ArtifactError(
                        "payload_too_large", "Artifact stream exceeds the declared size"
                    )
        except ArtifactError as exc:
            if exc.received_bytes is not None:
                received = exc.received_bytes
            if exc.code == "payload_too_large":
                status = 413
            elif exc.code == "storage_unavailable":
                status = 503
            else:
                status = 422
            failure = ApplicationError(
                code=exc.code,
                status=status,
                message=exc.message,
                retry_after=1 if status == 503 else None,
            )
            try:
                self._abort_upload(
                    upload_id, tenant_id, expected_size, received, record_id, failure
                )
            finally:
                self.store.abort_staging(handle)
            raise failure from exc
        except asyncio.CancelledError:
            # Cancellation is a BaseException in Python 3.11+. Preserve it for
            # the caller, but first terminalize the DB reservation and close the
            # staging capability so cancellation cannot strand tenant quota.
            failure = ApplicationError(
                code="validation_failed", status=422, message="Artifact stream was interrupted"
            )
            try:
                with suppress(Exception):
                    self._abort_upload(
                        upload_id, tenant_id, expected_size, received, record_id, failure
                    )
            finally:
                self.store.abort_staging(handle)
            raise
        except Exception as exc:
            failure = ApplicationError(
                code="validation_failed", status=422, message="Artifact stream was interrupted"
            )
            try:
                self._abort_upload(
                    upload_id, tenant_id, expected_size, received, record_id, failure
                )
            finally:
                self.store.abort_staging(handle)
            raise failure from exc
        if received != expected_size:
            failure = ApplicationError(
                code="validation_failed",
                status=422,
                message="Artifact stream ended before the declared size",
            )
            try:
                self._abort_upload(
                    upload_id, tenant_id, expected_size, received, record_id, failure
                )
            finally:
                self.store.abort_staging(handle)
            raise failure

        try:
            self._check_disk(0)
        except ApplicationError as failure:
            try:
                self._abort_upload(
                    upload_id, tenant_id, expected_size, received, record_id, failure
                )
            finally:
                self.store.abort_staging(handle)
            raise
        try:
            blob = self.store.commit_blob(handle)
        except ArtifactError as exc:
            status = 422 if exc.code in {"checksum_mismatch", "size_mismatch"} else 503
            failure = ApplicationError(
                code=exc.code,
                status=status,
                message=exc.message,
                retry_after=1 if status == 503 else None,
            )
            try:
                self._abort_upload(
                    upload_id, tenant_id, expected_size, received, record_id, failure
                )
            finally:
                self.store.abort_staging(handle)
            raise failure from exc

        artifact_id = new_uuid7()

        def commit_operation(session: Session) -> ArtifactUploadResult:
            live = self._authorize(session, principal, tenant_id, write=True)
            record = (
                session.execute(
                    select(idempotency_records)
                    .where(idempotency_records.c.idempotency_id == record_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if record["state"] == "COMPLETED":
                return ArtifactUploadResult(
                    dict(record["response_body"] or {}),
                    int(record["response_status"]),
                    dict(record["response_headers"] or {}),
                    orphan_blob=blob,
                )
            counter = self._counter_lock(session, tenant_id)
            if int(counter["reserved_bytes"]) < expected_size:
                raise ApplicationError(
                    code="dependency_unavailable",
                    status=503,
                    message="Artifact reservation state is inconsistent",
                    retry_after=1,
                )
            now = transaction_timestamp(session)
            existing = (
                session.execute(
                    select(artifacts)
                    .where(
                        artifacts.c.tenant_id == tenant_id,
                        artifacts.c.checksum == expected_checksum,
                        artifacts.c.kind == normalized_kind,
                        artifacts.c.media_type == normalized_media,
                        artifacts.c.state == "COMMITTED",
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                session.execute(
                    artifacts.insert().values(
                        artifact_id=artifact_id,
                        tenant_id=tenant_id,
                        kind=normalized_kind,
                        media_type=normalized_media,
                        size_bytes=expected_size,
                        checksum=expected_checksum,
                        blob_key=blob.blob_key,
                        state="COMMITTED",
                        version=1,
                    )
                )
                body = self._view(
                    {
                        "artifact_id": artifact_id,
                        "tenant_id": tenant_id,
                        "kind": normalized_kind,
                        "media_type": normalized_media,
                        "size_bytes": expected_size,
                        "checksum": expected_checksum,
                        "version": 1,
                        "created_at": now,
                    }
                )
                committed_delta = expected_size
            else:
                body = self._view(existing)
                committed_delta = 0
            session.execute(
                update(artifact_storage_counters)
                .where(artifact_storage_counters.c.tenant_id == tenant_id)
                .values(
                    reserved_bytes=artifact_storage_counters.c.reserved_bytes - expected_size,
                    committed_bytes=artifact_storage_counters.c.committed_bytes + committed_delta,
                    updated_at=now,
                    version=artifact_storage_counters.c.version + 1,
                )
            )
            session.execute(
                update(upload_sessions)
                .where(
                    upload_sessions.c.upload_id == upload_id, upload_sessions.c.state == "ACTIVE"
                )
                .values(state="COMMITTED", bytes_received=expected_size, updated_at=now)
            )
            location = f"/v1/artifacts/{body['artifact_id']}"
            headers = {"Location": location, "ETag": f'"v{body["version"]}"'}
            complete_idempotency(
                session,
                record_id,
                status=201,
                body=body,
                headers=headers,
                resource_id=UUID(body["artifact_id"]),
            )
            session.execute(
                audit_records.insert().values(
                    audit_id=new_uuid7(),
                    actor_type="USER",
                    actor_id=str(live.user_id),
                    tenant_id=tenant_id,
                    action="artifact.upload.commit",
                    target_type="ARTIFACT",
                    target_id=str(body["artifact_id"]),
                    reason="Durable artifact upload committed",
                    safe_metadata={"kind": normalized_kind, "size_bytes": expected_size},
                )
            )
            return ArtifactUploadResult(
                body,
                201,
                headers,
                orphan_blob=blob if existing is not None else None,
            )

        try:
            result = run_transaction(self.session_factory, commit_operation)
        except BaseException:
            # The blob is intentionally retained as an orphan when the DB outcome
            # is unknown; cleanup requires a later DB-issued claim.
            raise
        if result.orphan_blob is not None:
            try:
                token = self.issue_gc_token(
                    blob_key=result.orphan_blob.blob_key,
                    checksum=result.orphan_blob.checksum,
                    size_bytes=result.orphan_blob.size_bytes,
                    expires_at=datetime.now(UTC)
                    + timedelta(seconds=self.settings.orphan_ttl_seconds),
                )
                self.store.delete_unreferenced(result.orphan_blob.blob_key, token)
            except ArtifactError:
                # A failed cleanup is retained as an orphan for a later reconciliation pass.
                pass
        return result

    def list_artifacts(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        page_size: int,
        cursor: str | None,
        kind: str | None,
    ) -> dict[str, Any]:
        if not 1 <= page_size <= 100:
            raise ApplicationError(
                code="validation_failed", status=400, message="Page size is invalid"
            )

        filter_kind = kind

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, tenant_id, write=False)
            binding = {
                "actor_id": str(live.user_id),
                "tenant_id": str(tenant_id),
                "kind": filter_kind or "",
                "operation_id": "listArtifacts",
            }
            codec = CursorCodec(
                self.identity._server_secret,
                ttl_seconds=self.settings.cursor_ttl_seconds,
                now=lambda: transaction_timestamp(session),
            )
            statement = select(artifacts).where(
                artifacts.c.tenant_id == tenant_id, artifacts.c.state == "COMMITTED"
            )
            if filter_kind is not None:
                try:
                    normalized_filter_kind = str(filter_kind)
                    if normalized_filter_kind not in _READABLE_ARTIFACT_KINDS:
                        raise ValueError
                except ValueError:
                    raise ApplicationError(
                        code="validation_failed",
                        status=400,
                        message="Artifact kind filter is invalid",
                    ) from None
                statement = statement.where(artifacts.c.kind == normalized_filter_kind)
            if cursor is not None:
                try:
                    position = codec.decode(cursor, expected_binding=binding)
                    from datetime import datetime

                    created_at = datetime.fromisoformat(position["created_at"])
                    artifact_id = UUID(position["artifact_id"])
                except (CursorError, KeyError, ValueError):
                    raise ApplicationError(
                        code="invalid_cursor",
                        status=400,
                        message="The pagination cursor is invalid for this request",
                    ) from None
                statement = statement.where(
                    or_(
                        artifacts.c.created_at < created_at,
                        and_(
                            artifacts.c.created_at == created_at,
                            artifacts.c.artifact_id < artifact_id,
                        ),
                    )
                )
            rows = (
                session.execute(
                    statement.order_by(
                        artifacts.c.created_at.desc(), artifacts.c.artifact_id.desc()
                    ).limit(page_size + 1)
                )
                .mappings()
                .all()
            )
            visible = rows[:page_size]
            next_cursor = None
            if len(rows) > page_size:
                last = visible[-1]
                next_cursor = codec.encode(
                    binding=binding,
                    position={
                        "created_at": last["created_at"].isoformat(),
                        "artifact_id": str(last["artifact_id"]),
                    },
                )
            return {
                "items": [self._view(row) for row in visible],
                "page": {"next_cursor": next_cursor, "page_size": page_size},
            }

        return run_transaction(self.session_factory, operation)

    def get_artifact(
        self, principal: Principal, *, tenant_id: UUID, artifact_id: UUID
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            self._authorize(session, principal, tenant_id, write=False)
            row = (
                session.execute(
                    select(artifacts).where(
                        artifacts.c.artifact_id == artifact_id,
                        artifacts.c.tenant_id == tenant_id,
                        artifacts.c.state == "COMMITTED",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Artifact was not found"
                )
            return self._view(row)

        return run_transaction(self.session_factory, operation)

    def download_artifact(
        self, principal: Principal, *, tenant_id: UUID, artifact_id: UUID, range_header: str | None
    ) -> ArtifactDownload:
        def metadata_operation(session: Session) -> dict[str, Any]:
            self._authorize(session, principal, tenant_id, write=False)
            row = (
                session.execute(
                    select(artifacts).where(
                        artifacts.c.artifact_id == artifact_id,
                        artifacts.c.tenant_id == tenant_id,
                        artifacts.c.state == "COMMITTED",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Artifact was not found"
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
        byte_range = None
        status = 200
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Artifact-Media-Type": row["media_type"],
            "ETag": f'"{row["checksum"]}"',
        }
        if range_header is not None:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
            if match is None:
                raise ApplicationError(
                    code="validation_failed", status=400, message="Range header is invalid"
                )
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else None
            if start >= row["size_bytes"] or (end is not None and end < start):
                raise ApplicationError(
                    code="validation_failed",
                    status=400,
                    message="Range header is outside the artifact",
                )
            end = min(end, row["size_bytes"] - 1) if end is not None else row["size_bytes"] - 1
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
        return ArtifactDownload(reader, status, headers)

    def expire_uploads(self, *, limit: int = 100) -> int:
        """Expire DB-owned staging sessions and release their reservations.

        The caller may run this bounded operation from a later reconciliation loop;
        filesystem staging/orphan reclamation remains outside the DB transaction.
        """
        if not 1 <= limit <= 1000:
            raise ApplicationError(
                code="validation_failed", status=400, message="Expiry batch size is invalid"
            )

        def operation(session: Session) -> int:
            now = transaction_timestamp(session)
            candidates = (
                session.execute(
                    select(upload_sessions)
                    .where(
                        upload_sessions.c.state == "ACTIVE",
                        upload_sessions.c.expires_at <= now,
                    )
                    .order_by(upload_sessions.c.expires_at, upload_sessions.c.upload_id)
                    .limit(limit)
                )
                .mappings()
                .all()
            )

            # All upload commits lock idempotency -> counter -> upload session.
            # Acquire each lock class in that same order for the whole batch;
            # locking upload sessions during candidate selection would invert
            # the order and allow expiry/commit deadlocks.
            idempotency_rows: dict[UUID, Any] = {}
            for candidate in sorted(
                (row for row in candidates if row["idempotency_id"] is not None),
                key=lambda row: str(row["idempotency_id"]),
            ):
                idempotency_id = candidate["idempotency_id"]
                idempotency_rows[idempotency_id] = (
                    session.execute(
                        select(idempotency_records)
                        .where(idempotency_records.c.idempotency_id == idempotency_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )

            counters: dict[UUID, Any] = {}
            for tenant_id in sorted({row["tenant_id"] for row in candidates}, key=str):
                counters[tenant_id] = self._counter_lock(session, tenant_id)

            locked_rows: list[Any] = []
            for candidate in sorted(candidates, key=lambda row: str(row["upload_id"])):
                row = (
                    session.execute(
                        select(upload_sessions)
                        .where(upload_sessions.c.upload_id == candidate["upload_id"])
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is not None:
                    locked_rows.append(row)

            expired = 0
            for row in locked_rows:
                if row["state"] != "ACTIVE" or row["expires_at"] > now:
                    continue
                counter = counters[row["tenant_id"]]
                expected_size = int(row["expected_size_bytes"])
                if int(counter["reserved_bytes"]) < expected_size:
                    raise ApplicationError(
                        code="dependency_unavailable",
                        status=503,
                        message="Artifact reservation state is inconsistent",
                        retry_after=1,
                    )
                session.execute(
                    update(artifact_storage_counters)
                    .where(artifact_storage_counters.c.tenant_id == row["tenant_id"])
                    .values(
                        reserved_bytes=artifact_storage_counters.c.reserved_bytes - expected_size,
                        updated_at=now,
                        version=artifact_storage_counters.c.version + 1,
                    )
                )
                session.execute(
                    update(upload_sessions)
                    .where(
                        upload_sessions.c.upload_id == row["upload_id"],
                        upload_sessions.c.state == "ACTIVE",
                    )
                    .values(
                        state="EXPIRED",
                        updated_at=now,
                    )
                )
                if row["idempotency_id"] is not None:
                    record = idempotency_rows.get(row["idempotency_id"])
                    if record is not None and record["state"] == "PENDING":
                        complete_idempotency(
                            session,
                            row["idempotency_id"],
                            status=422,
                            body={
                                "code": "validation_failed",
                                "message": "Artifact upload staging expired",
                            },
                            headers={},
                        )
                expired += 1
            return expired

        return run_transaction(self.session_factory, operation)

    def issue_gc_token(
        self, *, blob_key: str, checksum: str, size_bytes: int, expires_at
    ) -> GcToken:
        def operation(session: Session) -> None:
            row = (
                session.execute(
                    select(artifacts).where(artifacts.c.blob_key == blob_key).with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is not None:
                if row["state"] != "COMMITTED":
                    raise ApplicationError(
                        code="state_conflict",
                        status=409,
                        message="Only unreferenced committed artifacts can be reclaimed",
                    )
                referenced = session.execute(
                    select(artifact_references.c.artifact_id)
                    .where(artifact_references.c.artifact_id == row["artifact_id"])
                    .limit(1)
                ).first()
                if referenced is not None:
                    raise ApplicationError(
                        code="state_conflict",
                        status=409,
                        message="Referenced artifacts cannot be reclaimed",
                    )

        run_transaction(self.session_factory, operation)
        return self.store.issue_gc_token(blob_key, checksum, size_bytes, expires_at)
