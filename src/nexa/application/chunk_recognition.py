"""Fenced recognition of ``batch.inference`` chunk outputs (B16).

Runs only inside a fenced checkpoint publish or result completion transaction, after the
caller has validated the committed chunk-output manifest bytes. Deterministic worker
defects raise :class:`ChunkDefect`, which each caller maps to its own rejection; a chunk
that differs from an already recognized one is a ``409 state_conflict`` that commits
nothing. No blob I/O happens here.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nexa.application.errors import ApplicationError
from nexa.infrastructure.artifacts.store import ArtifactError
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.workloads import chunk_manifest

MANIFEST_PURPOSE = "CHUNK_OUTPUT_MANIFEST"
CHUNK_PURPOSE = "CHUNK_OUTPUT"
_RECOGNIZED_FIELDS = (
    "session_id",
    "range_start",
    "range_end",
    "artifact_id",
    "checksum",
    "source_attempt_id",
    "source_job_fence",
)


class ChunkDefect(Exception):
    """The worker's chunk graph is a deterministic defect of this publish."""


def storage_unavailable(message: str = "Artifact storage is unavailable") -> ApplicationError:
    return ApplicationError(
        code="dependency_unavailable", status=503, message=message, retry_after=1
    )


def read_committed(store, row: dict, max_bytes: int) -> bytes:
    """Bounded bytes of a committed artifact; disagreement with its metadata fails closed."""
    if store is None:
        raise storage_unavailable()
    try:
        reader = store.open(row["blob_key"])
        try:
            raw = reader.read(max_bytes + 1)
        finally:
            reader.close()
    except ArtifactError as exc:
        raise storage_unavailable() from exc
    if len(raw) != row["size_bytes"] or (
        "sha256:" + hashlib.sha256(raw).hexdigest() != row["checksum"]
    ):
        raise storage_unavailable("Artifact storage failed integrity validation")
    return raw


def prefetch_rows(session, *, tenant_id, wanted: dict[str, tuple[str, int]]) -> list[dict]:
    """Committed rows for ``{artifact_id: (kind, max_bytes)}``; unknown IDs are skipped."""
    ids = []
    for artifact_id in wanted:
        try:
            ids.append(UUID(str(artifact_id)))
        except ValueError:
            continue
    if not ids or tenant_id is None:
        return []
    rows = []
    for row in session.execute(
        select(s.artifacts).where(
            s.artifacts.c.artifact_id.in_(ids),
            s.artifacts.c.tenant_id == tenant_id,
            s.artifacts.c.state == "COMMITTED",
        )
    ).mappings():
        kind, max_bytes = wanted[str(row["artifact_id"])]
        if row["kind"] == kind and row["size_bytes"] <= max_bytes:
            rows.append(dict(row))
    return rows


def prefetch_blobs(store, rows: list[dict], wanted: dict[str, tuple[str, int]]) -> dict:
    return {
        str(row["artifact_id"]): read_committed(store, row, wanted[str(row["artifact_id"])][1])
        for row in rows
    }


def prefetched_bytes(session, prefetched: dict, *, tenant_id, artifact_id: str) -> bytes:
    """Bytes read before the transaction, or a retry when the artifact committed since."""
    raw = prefetched.get(str(artifact_id))
    if raw is not None:
        return raw
    try:
        identifier = UUID(str(artifact_id))
    except ValueError as exc:
        raise ChunkDefect("artifact identifier is invalid") from exc
    committed = session.execute(
        select(s.artifacts.c.artifact_id).where(
            s.artifacts.c.artifact_id == identifier,
            s.artifacts.c.tenant_id == tenant_id,
            s.artifacts.c.state == "COMMITTED",
        )
    ).scalar_one_or_none()
    if committed is not None:
        raise storage_unavailable()
    raise ChunkDefect("artifact is not committed")


def model_checksum(session, *, tenant_id, spec: dict) -> str | None:
    if spec.get("model_artifact_id") is None:
        return None
    return session.execute(
        select(s.artifacts.c.checksum).where(
            s.artifacts.c.artifact_id == spec["model_artifact_id"],
            s.artifacts.c.tenant_id == tenant_id,
            s.artifacts.c.state == "COMMITTED",
        )
    ).scalar_one_or_none()


def pin_extents(session, *, job, attempt_id, job_fence, item_count, chunk_size) -> None:
    """The first recognition fixes the job's item count; every later one must agree."""
    session.execute(
        pg_insert(s.inference_extents)
        .values(
            job_id=job["job_id"],
            tenant_id=job["tenant_id"],
            item_count=item_count,
            chunk_size=chunk_size,
            source_attempt_id=UUID(str(attempt_id)),
            source_job_fence=job_fence,
        )
        .on_conflict_do_nothing(index_elements=[s.inference_extents.c.job_id])
    )
    pinned = (
        session.execute(
            select(s.inference_extents).where(s.inference_extents.c.job_id == job["job_id"])
        )
        .mappings()
        .one()
    )
    if pinned["item_count"] != item_count or pinned["chunk_size"] != chunk_size:
        raise ChunkDefect("inference extent differs from the job's recognized extent")


def lock_manifest(session, *, job, reference: dict) -> dict:
    """The referenced chunk-output manifest row, share-locked for the publish."""
    try:
        artifact_id = UUID(str(reference["artifact_id"]))
    except (KeyError, ValueError) as exc:
        raise ChunkDefect("chunk-output manifest reference is invalid") from exc
    row = (
        session.execute(
            select(s.artifacts)
            .where(
                s.artifacts.c.artifact_id == artifact_id,
                s.artifacts.c.tenant_id == job["tenant_id"],
            )
            .with_for_update(read=True)
        )
        .mappings()
        .one_or_none()
    )
    if (
        row is None
        or row["state"] != "COMMITTED"
        or row["kind"] != "CHUNK_OUTPUT_MANIFEST"
        or row["checksum"] != reference.get("checksum")
    ):
        raise ChunkDefect("chunk-output manifest artifact is invalid")
    return dict(row)


def _expected_row(job, session_id, entry) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "range_start": entry["start_index"],
        "range_end": entry["end_index_exclusive"],
        "artifact_id": UUID(entry["file"]["artifact_id"]),
        "checksum": entry["file"]["checksum"],
        "source_attempt_id": UUID(entry["source_attempt_id"]),
        "source_job_fence": entry["source_job_fence"],
    }


def _recognized(session, job, chunk_id):
    return (
        session.execute(
            select(s.recognized_chunks).where(
                s.recognized_chunks.c.job_id == job["job_id"],
                s.recognized_chunks.c.chunk_id == chunk_id,
            )
        )
        .mappings()
        .one_or_none()
    )


def _same(row, expected) -> bool:
    return all(row[field] == expected[field] for field in _RECOGNIZED_FIELDS)


def recognize(
    session, *, job, attempt_id, job_fence, session_id, entries: list[dict], lineage
) -> list[tuple[UUID, str, str]]:
    """Insert-or-verify every entry; return the publish owner's reference edges.

    ``lineage(artifact_row)`` raises :class:`ApplicationError` unless the artifact was
    uploaded by the current Authority lineage.
    """
    current = (str(attempt_id), job_fence)
    ids = [UUID(entry["file"]["artifact_id"]) for entry in entries]
    artifacts = {
        row["artifact_id"]: dict(row)
        for row in session.execute(
            select(s.artifacts)
            .where(
                s.artifacts.c.artifact_id.in_(ids),
                s.artifacts.c.tenant_id == job["tenant_id"],
            )
            .order_by(s.artifacts.c.artifact_id)
            .with_for_update(read=True)
        ).mappings()
    }
    edges = []
    for entry in entries:
        expected = _expected_row(job, session_id, entry)
        artifact = artifacts.get(expected["artifact_id"])
        if (
            artifact is None
            or artifact["state"] != "COMMITTED"
            or artifact["kind"] != "RESULT_FILE"
            or artifact["media_type"] != entry["file"]["media_type"]
            or artifact["size_bytes"] != entry["file"]["size_bytes"]
            or artifact["checksum"] != entry["file"]["checksum"]
        ):
            raise ChunkDefect(f"{entry['chunk_id']} is not a committed chunk file")
        existing = _recognized(session, job, entry["chunk_id"])
        if (entry["source_attempt_id"], entry["source_job_fence"]) != current:
            # Carry-forward: only a fact an earlier fenced publish recognized.
            if existing is None or not _same(existing, expected):
                raise ChunkDefect(f"{entry['chunk_id']} has no exact prior recognition")
        else:
            try:
                lineage(artifact)
            except ApplicationError as exc:
                raise ChunkDefect(f"{entry['chunk_id']} has no current upload lineage") from exc
            if existing is None:
                existing = _insert(session, job, entry, expected)
            if not _same(existing, expected):
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Chunk output differs from its recognized output",
                )
        edges.append((expected["artifact_id"], CHUNK_PURPOSE, entry["file"]["logical_name"]))
    return edges


def _insert(session, job, entry, expected):
    recognized_chunk_id = new_uuid7()
    inserted = session.execute(
        pg_insert(s.recognized_chunks)
        .values(
            recognized_chunk_id=recognized_chunk_id,
            tenant_id=job["tenant_id"],
            job_id=job["job_id"],
            chunk_id=entry["chunk_id"],
            **expected,
        )
        .on_conflict_do_nothing(
            index_elements=[s.recognized_chunks.c.job_id, s.recognized_chunks.c.chunk_id]
        )
        .returning(s.recognized_chunks.c.recognized_chunk_id)
    ).scalar_one_or_none()
    if inserted is not None:
        session.execute(
            insert(s.artifact_references).values(
                tenant_id=job["tenant_id"],
                artifact_id=expected["artifact_id"],
                owner_type="RECOGNIZED_CHUNK",
                owner_id=recognized_chunk_id,
                purpose=CHUNK_PURPOSE,
                logical_name=entry["file"]["logical_name"],
            )
        )
    return _recognized(session, job, entry["chunk_id"])


def manifest_edge(row: dict) -> tuple[UUID, str, str]:
    return row["artifact_id"], MANIFEST_PURPOSE, chunk_manifest.LOGICAL_NAME


__all__ = [
    "CHUNK_PURPOSE",
    "MANIFEST_PURPOSE",
    "ChunkDefect",
    "lock_manifest",
    "manifest_edge",
    "model_checksum",
    "pin_extents",
    "prefetch_blobs",
    "prefetch_rows",
    "prefetched_bytes",
    "read_committed",
    "recognize",
    "storage_unavailable",
]
