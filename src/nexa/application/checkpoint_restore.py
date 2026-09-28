"""Restore selection for a new Attempt: verify blobs outside transactions, decide at claim."""

from __future__ import annotations

import hashlib

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nexa.application import chunk_recognition
from nexa.application.artifact_service import ArtifactService
from nexa.application.checkpoint_service import (
    _INVENTORY_UNREPORTED,
    _conflict,
    _storage_unavailable,
    checkpoint_record,
    spec_and_template,
    worker_architecture,
)
from nexa.application.checkpoint_validation import (
    CHECKPOINT_MANIFEST_MAX_BYTES,
    CPU_STATE_MAX_BYTES,
    CheckpointManifestError,
    check_chunk_manifest,
    check_file_artifacts,
    checkpoint_provenance,
    expected_compatibility,
    parse_checkpoint_manifest,
    validate_checkpoint_manifest,
    validate_cpu_state,
    validate_inference_state_files,
    validate_training_state_files,
)
from nexa.application.execution_cleanup import _event
from nexa.application.job_recovery import restorable_scope, restore_order
from nexa.domain import workload_adapters
from nexa.infrastructure.artifacts.store import ArtifactError
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.locking import clock_timestamp
from nexa.infrastructure.persistence.transactions import run_transaction
from nexa.workloads import chunk_manifest

# Reasons that describe the destination rather than the stored bytes; they never
# mark a checkpoint corrupt because another worker or version could restore it.
_INCOMPATIBLE = {"CHECKPOINT_COMPATIBILITY_MISMATCH", "CHECKPOINT_TEMPLATE_UNSUPPORTED"}


def _not_corrupt():
    return ~exists().where(
        s.checkpoint_corruptions.c.checkpoint_id == s.checkpoints.c.checkpoint_id
    )


def _read_blob(store, row, limit):
    """Return (bytes, None) or (None, corruption reason); outages raise 503."""
    try:
        reader = store.open(row["blob_key"])
        try:
            raw = reader.read(limit + 1)
        finally:
            reader.close()
    except ArtifactError as exc:
        # Only a missing file under a present storage root is evidence about this
        # blob. Any other storage error may be transient and must not be one-way.
        if exc.code == "not_found":
            return None, "CHECKPOINT_BLOB_MISSING"
        raise _storage_unavailable() from exc
    if len(raw) != row["size_bytes"] or (
        "sha256:" + hashlib.sha256(raw).hexdigest() != row["checksum"]
    ):
        return None, "CHECKPOINT_CHECKSUM_MISMATCH"
    return raw, None


def _inherits(session, job_id):
    return (
        session.execute(
            select(s.checkpoint_references.c.source_checkpoint_id)
            .where(s.checkpoint_references.c.target_job_id == job_id)
            .limit(1)
        ).first()
        is not None
    )


def chunked_template(template) -> bool:
    adapter = workload_adapters.descriptor(template["adapter_id"], template["adapter_version"])
    return adapter is not None and adapter.chunked


def _chunk_scope(session, tenant_id, spec, job_ids):
    """Per source job: recognized chunks, pinned extent and model checksum (B16)."""
    scope = {job_id: {"recognized": {}, "extent": None} for job_id in job_ids}
    for row in session.execute(
        select(s.recognized_chunks).where(
            s.recognized_chunks.c.tenant_id == tenant_id,
            s.recognized_chunks.c.job_id.in_(job_ids),
        )
    ).mappings():
        scope[row["job_id"]]["recognized"][row["chunk_id"]] = dict(row)
    for row in session.execute(
        select(s.inference_extents).where(
            s.inference_extents.c.tenant_id == tenant_id,
            s.inference_extents.c.job_id.in_(job_ids),
        )
    ).mappings():
        scope[row["job_id"]]["extent"] = (row["item_count"], row["chunk_size"])
    model = chunk_recognition.model_checksum(session, tenant_id=tenant_id, spec=spec)
    for value in scope.values():
        value["model_checksum"] = model
    return scope


def restore_candidates(session, job, spec, template, rows):
    """Metadata and expected provenance of each checkpoint row, in the given order.

    Provenance names the job/session that produced the checkpoint; spec, input,
    template, adapter and image are the (identical, immutable) ones of `job`.
    """
    if not rows:
        return []
    ids = [row["checkpoint_id"] for row in rows]
    fences = dict(
        session.execute(
            select(s.attempts.c.attempt_id, s.attempts.c.job_fence).where(
                s.attempts.c.attempt_id.in_({row["attempt_id"] for row in rows})
            )
        ).all()
    )
    sessions = dict(
        session.execute(
            select(s.logical_sessions.c.job_id, s.logical_sessions.c.session_id).where(
                s.logical_sessions.c.job_id.in_({row["job_id"] for row in rows})
            )
        ).all()
    )
    chunked = chunked_template(template)
    purposes = ["CHECKPOINT_FILE"]
    if chunked:
        purposes += [chunk_recognition.MANIFEST_PURPOSE, chunk_recognition.CHUNK_PURPOSE]
    edges = session.execute(
        select(
            s.artifact_references.c.owner_id,
            s.artifact_references.c.artifact_id,
            s.artifact_references.c.logical_name,
            s.artifact_references.c.purpose,
        ).where(
            s.artifact_references.c.tenant_id == job["tenant_id"],
            s.artifact_references.c.owner_type == "CHECKPOINT",
            s.artifact_references.c.owner_id.in_(ids),
            s.artifact_references.c.purpose.in_(purposes),
        )
    ).all()
    references = [edge for edge in edges if edge.purpose == "CHECKPOINT_FILE"]
    artifact_ids = {row["manifest_artifact_id"] for row in rows}
    artifact_ids |= {edge.artifact_id for edge in edges}
    artifacts = {
        str(row["artifact_id"]): dict(row)
        for row in session.execute(
            select(s.artifacts).where(
                s.artifacts.c.tenant_id == job["tenant_id"],
                s.artifacts.c.artifact_id.in_(artifact_ids),
            )
        ).mappings()
    }
    input_checksum = session.execute(
        select(s.artifacts.c.checksum).where(
            s.artifacts.c.artifact_id == spec["input_artifact_id"],
            s.artifacts.c.tenant_id == job["tenant_id"],
        )
    ).scalar_one()
    scope = (
        _chunk_scope(session, job["tenant_id"], spec, {row["job_id"] for row in rows})
        if chunked
        else {}
    )
    candidates = []
    for row in rows:
        owned = [r for r in references if r.owner_id == row["checkpoint_id"]]
        candidate = {
            "row": row,
            "manifest": artifacts.get(str(row["manifest_artifact_id"])),
            "references": sorted((str(r.artifact_id), r.logical_name) for r in owned),
            "files": {
                str(r.artifact_id): artifacts[str(r.artifact_id)]
                for r in owned
                if str(r.artifact_id) in artifacts
            },
            # Provenance names the Attempt and fence that produced the checkpoint.
            "provenance": checkpoint_provenance(
                job={"tenant_id": job["tenant_id"], "job_id": row["job_id"]},
                spec=spec,
                template=template,
                session_id=sessions[row["job_id"]],
                input_checksum=input_checksum,
                attempt_id=row["attempt_id"],
                fence=fences[row["attempt_id"]],
            ),
        }
        if chunked:
            chunk_edges = [e for e in edges if e.owner_id == row["checkpoint_id"]]
            manifests = [
                artifacts.get(str(e.artifact_id))
                for e in chunk_edges
                if e.purpose == chunk_recognition.MANIFEST_PURPOSE
            ]
            files = [e for e in chunk_edges if e.purpose == chunk_recognition.CHUNK_PURPOSE]
            candidate["chunks"] = {
                **scope[row["job_id"]],
                "manifest": manifests[0] if len(manifests) == 1 else None,
                "references": sorted((str(e.artifact_id), e.logical_name) for e in files),
                "files": {
                    str(e.artifact_id): artifacts[str(e.artifact_id)]
                    for e in files
                    if str(e.artifact_id) in artifacts
                },
            }
        candidates.append(candidate)
    return candidates


def _verify_chunks(store, manifest, state, chunks, provenance):
    """B16-R18: the chunk-output manifest and every chunk it names, from committed bytes."""
    extent = (state["item_count"], state["chunk_size"])
    if chunks["extent"] != extent:
        return "CORRUPT", "CHECKPOINT_STATE_INVALID"
    row = chunks["manifest"]
    reference = manifest["chunk_output_manifest"]
    if row is None or str(row["artifact_id"]) != reference["artifact_id"]:
        return "CORRUPT", "CHECKPOINT_FILES_INVALID"
    raw, reason = _read_blob(store, row, chunk_manifest.MAX_MANIFEST_BYTES)
    if reason is not None:
        return "CORRUPT", reason
    try:
        entries = check_chunk_manifest(
            raw,
            artifact=row,
            reference=reference,
            provenance=provenance,
            model_checksum=chunks["model_checksum"],
            state=state,
            chunk_total=manifest["cursor"]["step"],
        )
    except chunk_manifest.ChunkManifestError:
        return "CORRUPT", "CHECKPOINT_FILES_INVALID"
    named = sorted((e["file"]["artifact_id"], e["file"]["logical_name"]) for e in entries)
    if named != chunks["references"]:
        return "CORRUPT", "CHECKPOINT_FILES_INVALID"
    for entry in entries:
        recognized = chunks["recognized"].get(entry["chunk_id"])
        file_row = chunks["files"].get(entry["file"]["artifact_id"])
        if (
            recognized is None
            or str(recognized["artifact_id"]) != entry["file"]["artifact_id"]
            or recognized["checksum"] != entry["file"]["checksum"]
            or (recognized["range_start"], recognized["range_end"])
            != (entry["start_index"], entry["end_index_exclusive"])
            or str(recognized["source_attempt_id"]) != entry["source_attempt_id"]
            or recognized["source_job_fence"] != entry["source_job_fence"]
            or str(recognized["session_id"]) != provenance["session_id"]
            or file_row is None
            or file_row["state"] != "COMMITTED"
            or file_row["kind"] != "RESULT_FILE"
            or file_row["media_type"] != entry["file"]["media_type"]
            or file_row["size_bytes"] != entry["file"]["size_bytes"]
            or file_row["checksum"] != entry["file"]["checksum"]
        ):
            return "CORRUPT", "CHECKPOINT_FILES_INVALID"
        # A missing or corrupt referenced chunk is never skipped: the checkpoint falls back.
        _, reason = _read_blob(store, file_row, chunk_manifest.MAX_CHUNK_FILE_BYTES)
        if reason is not None:
            return "CORRUPT", reason
    return "VALID", row


def verify_candidate(store, plan, candidate, compatibility):
    """Return ("VALID", (manifest, files)) or (outcome, reason) for one candidate."""
    row, manifest_row = candidate["row"], candidate["manifest"]
    if "chunks" in candidate and row["job_id"] != plan.get("job_id"):
        # B16-R20: chunk carry-forward is job-scoped, so another job's inference
        # checkpoint cannot be restored here; the destination falls back visibly.
        return "INCOMPATIBLE", "CHECKPOINT_COMPATIBILITY_MISMATCH"
    if (
        manifest_row is None
        or manifest_row["state"] != "COMMITTED"
        or manifest_row["kind"] != "CHECKPOINT_MANIFEST"
        or manifest_row["checksum"] != row["manifest_checksum"]
    ):
        return "CORRUPT", "CHECKPOINT_MANIFEST_INVALID"
    raw, reason = _read_blob(store, manifest_row, CHECKPOINT_MANIFEST_MAX_BYTES)
    if reason is not None:
        return "CORRUPT", reason
    try:
        manifest = parse_checkpoint_manifest(raw)
        entries = validate_checkpoint_manifest(
            manifest,
            raw=raw,
            artifact=manifest_row,
            checkpoint_id=row["checkpoint_id"],
            sequence=row["sequence"],
            provenance=candidate["provenance"],
            compatibility=compatibility,
            parameters=plan["parameters"],
        )
        if (
            sorted((e["artifact_id"], e["logical_name"]) for e in entries)
            != candidate["references"]
        ):
            return "CORRUPT", "CHECKPOINT_FILES_INVALID"
        files = check_file_artifacts(entries, candidate["files"], tenant_id=plan["tenant_id"])
    except CheckpointManifestError as exc:
        outcome = "INCOMPATIBLE" if exc.reason_code in _INCOMPATIBLE else "CORRUPT"
        return outcome, exc.reason_code
    cpu = manifest["provenance"]["adapter_id"] == "cpu.iterative"
    contents = {}
    for entry, file_row in zip(entries, files, strict=True):
        # Every size is bounded by the validated manifest before any byte is read.
        limit = CPU_STATE_MAX_BYTES if cpu else file_row["size_bytes"]
        content, reason = _read_blob(store, file_row, limit)
        if reason is not None:
            return "CORRUPT", reason
        contents[entry["logical_name"]] = content
    try:
        if cpu:
            for content in contents.values():
                validate_cpu_state(content, manifest=manifest)
        elif "chunks" in candidate:
            state = validate_inference_state_files(
                contents,
                manifest=manifest,
                parameters=plan["parameters"],
                model_checksum=candidate["chunks"]["model_checksum"],
            )
        else:
            validate_training_state_files(
                contents, manifest=manifest, parameters=plan["parameters"]
            )
    except CheckpointManifestError as exc:
        return "CORRUPT", exc.reason_code
    if "chunks" in candidate:
        outcome, value = _verify_chunks(
            store, manifest, state, candidate["chunks"], candidate["provenance"]
        )
        if outcome != "VALID":
            return outcome, value
        # The runner carries the exact chunk entries forward from this restore file.
        files = [*files, value]
    return "VALID", (manifest, files)


class CheckpointRestoreMixin:
    def _restore_plan(self, *, credential, authority, callback_id):
        """Snapshot restore candidates for an unclaimed Attempt; None means no decision."""

        def operation(session):
            self._mode(session)
            worker = self._worker_auth(
                session, worker_id=authority.worker_id, credential=credential
            )
            self._require_current_incarnation(
                session, worker, authority.worker_incarnation_id, require_reconciling=False
            )
            acknowledgment = session.execute(
                select(s.callback_receipts.c.acknowledgment).where(
                    s.callback_receipts.c.worker_id == authority.worker_id,
                    s.callback_receipts.c.operation_id == "workerClaimAttempt",
                    s.callback_receipts.c.callback_id == callback_id,
                )
            ).scalar_one_or_none()
            if acknowledgment:
                return None
            attempt = (
                session.execute(
                    select(s.attempts).where(
                        s.attempts.c.attempt_id == authority.attempt_id,
                        s.attempts.c.worker_id == authority.worker_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if attempt is None or attempt["execution_context"] is not None:
                return None
            job = dict(
                session.execute(select(s.jobs).where(s.jobs.c.job_id == attempt["job_id"]))
                .mappings()
                .one()
            )
            spec, template = spec_and_template(session, job["job_id"])
            rows = [
                dict(row)
                for row in session.execute(
                    select(s.checkpoints)
                    .where(restorable_scope(job["job_id"]), _not_corrupt())
                    .order_by(*restore_order(job["job_id"]))
                ).mappings()
            ]
            return {
                "attempt_number": attempt["attempt_number"],
                "restart_safe": bool(template["restart_safe"]),
                # A manual retry that referenced a checkpoint must restore it or
                # fall back visibly, exactly like a later Attempt of the same Job.
                "inherited": _inherits(session, job["job_id"]),
                "tenant_id": job["tenant_id"],
                "job_id": job["job_id"],
                "template": template,
                "architecture": worker_architecture(session, authority.worker_id),
                "parameters": spec["canonical_spec"].get("parameters") or {},
                "candidates": restore_candidates(session, job, spec, template, rows),
            }

        return run_transaction(self.session_factory, operation)

    def _scan_restore(self, plan):
        """Verify newest first outside transactions and stop at the first valid checkpoint."""
        scan = {"marks": [], "incompatible": [], "selected": None, "remaining": []}
        if not plan["candidates"]:
            return scan
        if self.artifact_store is None:
            raise _storage_unavailable()
        if plan["architecture"] is None:
            # An unreported inventory is unknown, not incompatible: never skip a
            # checkpoint or fall back to input because of it.
            raise _storage_unavailable(_INVENTORY_UNREPORTED)
        try:
            compatibility = expected_compatibility(plan["template"], plan["architecture"])
        except CheckpointManifestError:
            compatibility = {}
        rejected = []
        for candidate in plan["candidates"]:
            row = candidate["row"]
            if row["compatibility"] in rejected:
                # Same stored environment fails the same way; no new reason to emit.
                continue
            outcome, value = verify_candidate(self.artifact_store, plan, candidate, compatibility)
            if outcome == "CORRUPT":
                scan["marks"].append((row["checkpoint_id"], value))
            elif outcome == "INCOMPATIBLE":
                if row["job_id"] == plan["job_id"]:
                    # An inherited chunked checkpoint says nothing about this environment.
                    rejected.append(row["compatibility"])
                scan["incompatible"].append(value)
            else:
                scan["selected"] = (row, *value)
                break
        marked = {checkpoint_id for checkpoint_id, _ in scan["marks"]}
        # Claim rechecks that no mark or checkpoint appeared since this snapshot.
        scan["remaining"] = [
            c["row"]["checkpoint_id"]
            for c in plan["candidates"]
            if c["row"]["checkpoint_id"] not in marked
        ]
        return scan

    def _mark_corrupt(self, *, credential, authority, marks):
        """Commit one-way corruption marks and events under the live Attempt authority."""

        def operation(session):
            if self._mode(session) == "WRITE_FROZEN":
                _conflict("Writes are frozen")
            worker = self._worker_auth(
                session, worker_id=authority.worker_id, credential=credential
            )
            self._require_current_incarnation(
                session, worker, authority.worker_incarnation_id, require_reconciling=False
            )
            job, attempt, lease, allocation, _ = self._authority_rows(
                session, authority, worker_id=authority.worker_id
            )
            now = clock_timestamp(session)
            self._live_authority(job, attempt, lease, allocation, now)
            job = dict(job)
            for checkpoint_id, reason in marks:
                owned = session.execute(
                    select(s.checkpoints.c.checkpoint_id).where(
                        s.checkpoints.c.checkpoint_id == checkpoint_id,
                        restorable_scope(job["job_id"]),
                    )
                ).first()
                if owned is None:
                    _conflict("Checkpoint does not belong to this Job")
                inserted = session.execute(
                    pg_insert(s.checkpoint_corruptions)
                    .values(
                        checkpoint_id=checkpoint_id,
                        tenant_id=job["tenant_id"],
                        reason_code=reason,
                    )
                    .on_conflict_do_nothing(index_elements=["checkpoint_id"])
                    .returning(s.checkpoint_corruptions.c.checkpoint_id)
                ).first()
                if inserted is not None:
                    # Restore selection belongs to claim acknowledgment: event only,
                    # no Job version change.
                    _event(
                        session,
                        job,
                        authority.worker_id,
                        now,
                        "CHECKPOINT_CORRUPT",
                        reason,
                        keep_version=True,
                    )
                    job["event_sequence"] += 1

        run_transaction(self.session_factory, operation)

    def _prepare_restore(self, *, credential, authority, callback_id):
        plan = self._restore_plan(
            credential=credential, authority=authority, callback_id=callback_id
        )
        if plan is None:
            return None
        scan = self._scan_restore(plan)
        if scan["marks"]:
            self._mark_corrupt(credential=credential, authority=authority, marks=scan["marks"])
        scan["attempt_number"] = plan["attempt_number"]
        scan["restart_safe"] = plan["restart_safe"]
        scan["inherited"] = plan["inherited"]
        return scan

    @staticmethod
    def _restore_decision(session, job, authority, scan, now):
        """Recheck the scan inside claim and return the immutable restore_checkpoint."""
        if scan is None:
            raise _storage_unavailable("Checkpoint restore selection must be retried")
        current = list(
            session.execute(
                select(s.checkpoints.c.checkpoint_id)
                .where(restorable_scope(job["job_id"]), _not_corrupt())
                .order_by(*restore_order(job["job_id"]))
            ).scalars()
        )
        if current != scan["remaining"]:
            raise _storage_unavailable("Checkpoint restore selection must be retried")
        restore = None
        if scan["selected"] is not None:
            row, manifest, files = scan["selected"]
            locked = session.execute(
                select(s.artifacts.c.artifact_id)
                .where(
                    s.artifacts.c.tenant_id == job["tenant_id"],
                    s.artifacts.c.artifact_id.in_(
                        [row["manifest_artifact_id"], *(f["artifact_id"] for f in files)]
                    ),
                    s.artifacts.c.state == "COMMITTED",
                )
                .order_by(s.artifacts.c.artifact_id)
                .with_for_update(read=True)
            ).all()
            if len(locked) != len(files) + 1:
                raise _storage_unavailable("Checkpoint restore selection must be retried")
            restore = {
                "record": checkpoint_record(row),
                "manifest": manifest,
                "files": [ArtifactService._view(f) for f in files],
            }
        events = [("CHECKPOINT_INCOMPATIBLE", reason) for reason in scan["incompatible"]]
        if restore is not None:
            events.append(("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"))
        elif (scan["attempt_number"] > 1 or scan["inherited"]) and scan["restart_safe"]:
            events.append(("CHECKPOINT_FALLBACK_TO_INPUT", "CHECKPOINT_FALLBACK_TO_INPUT"))
        elif scan["attempt_number"] > 1 or scan["inherited"]:
            # The poll offer named a checkpoint, so the worker fails this Attempt
            # INCOMPATIBLE through NoContainerProof before any Docker create. Start
            # is also refused, so input is never replayed for a non-restart-safe Job.
            events.append(("CHECKPOINT_RESTORE_UNAVAILABLE", "CHECKPOINT_RESTORE_UNAVAILABLE"))
        for event_type, reason in events:
            # Claim acknowledgment appends events without a Job version change.
            _event(session, job, authority.worker_id, now, event_type, reason, keep_version=True)
            job["event_sequence"] += 1
        return restore
