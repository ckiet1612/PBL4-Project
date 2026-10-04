"""Durable CPU result handshake; slow byte I/O never holds the authority lock."""

import hashlib

import rfc8785

from nexa.observability import metrics_worker
from nexa.workloads import chunk_manifest, inference_state

from .client import WorkerApiError

# The safe ErrorResponse reason of a publish whose chunk differs from its recognized row.
CHUNK_OUTPUT_CONFLICT = "CHUNK_OUTPUT_CONFLICT"


class ChunkOutputConflict(RuntimeError):
    """The server keeps a different recognized chunk; no replay can succeed (B16-R21)."""


def raise_chunk_conflict(exc):
    """Re-raise a publish rejection, as ChunkOutputConflict only for its safe reason.

    Any other ``409`` stays a replayed rejection, never a guessed failure.
    """
    if exc.status == 409 and exc.reason == CHUNK_OUTPUT_CONFLICT:
        raise ChunkOutputConflict("recognized chunk output conflicts") from None
    raise exc


def checksum(value):
    return "sha256:" + hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def descriptor_key(attempt_id, callback_id, sequence, descriptor, *, purpose="RESULT"):
    return hashlib.sha256(
        rfc8785.dumps(
            {
                "version": 1,
                "attempt_id": attempt_id,
                "purpose": purpose,
                "reservation_callback_id": callback_id,
                "source_message_sequence": sequence,
                "staging_name": descriptor["staging_name"],
                "descriptor_checksum": checksum(descriptor),
            }
        )
    ).hexdigest()


class ResultFlow:
    def __init__(self, journal, client, read_output, control):
        self.journal = journal
        self.client = client
        self.read_output = read_output
        self.control = control

    def _load(self, attempt_id):
        return (self.journal.load(attempt_id).runner_state or {}).get("result_flow", {})

    def _save(self, attempt_id, **changes):
        self.journal.update_runner_state(
            attempt_id,
            lambda state: {**state, "result_flow": {**state.get("result_flow", {}), **changes}},
        )

    def process(self, attempt_id, envelope):
        """Effects are durable before caller commits/ACKs the message sequence."""
        import json
        from dataclasses import asdict

        from nexa.infrastructure.persistence.ids import new_uuid7

        payload = envelope["payload"]
        sequence = envelope["message_sequence"]
        state = self._load(attempt_id)
        authority = self.journal.load(attempt_id).authority
        if envelope["type"] == "RESULT_PREPARE":
            if not state:
                self._save(
                    attempt_id,
                    completion_token=payload["completion_token"],
                    reservation_callback_id=str(new_uuid7()),
                    source_sequence=sequence,
                )
                state = self._load(attempt_id)
            if (
                state["completion_token"] != payload["completion_token"]
                or state["source_sequence"] != sequence
            ):
                raise ValueError("result prepare mapping conflict")
            reservation = state.get("reservation")
            if reservation is None:
                reservation = self.client.reserve_result(
                    attempt_id, state["reservation_callback_id"], {"authority": asdict(authority)}
                )
                if (
                    reservation["callback_id"] != state["reservation_callback_id"]
                    or reservation["attempt_id"] != attempt_id
                ):
                    raise ValueError("result reservation identity mismatch")
                self._save(attempt_id, reservation=reservation)
                self.journal.update_runner_state(
                    attempt_id,
                    lambda local: {
                        **local,
                        "active_reservations": {
                            **(local.get("active_reservations") or {"checkpoint": None}),
                            "result": reservation,
                        },
                    },
                )
            self.control(
                attempt_id,
                f"prepare:{sequence}",
                "PREPARE_RESULT",
                {
                    "completion_token": payload["completion_token"],
                    "reservation_callback_id": state["reservation_callback_id"],
                    "result_id": reservation["result_id"],
                },
            )
            return
        if not state or not state.get("reservation"):
            raise ValueError("result frame has no committed reservation")
        if envelope["type"] in ("CHUNK_FILE_BATCH", "AUXILIARY_MANIFEST_READY"):
            if (
                payload.get("purpose") != "RESULT"
                or payload.get("reservation_callback_id") != state["reservation_callback_id"]
                or payload.get("reserved_id") != state["reservation"]["result_id"]
            ):
                raise ValueError("result chunk frame identity mismatch")
            if envelope["type"] == "CHUNK_FILE_BATCH":
                self._chunk_batch(attempt_id, envelope, state)
            else:
                self._chunk_manifest_ready(attempt_id, envelope, state)
            return
        for field, expected in (
            ("completion_token", state["completion_token"]),
            ("reservation_callback_id", state["reservation_callback_id"]),
            ("result_id", state["reservation"]["result_id"]),
        ):
            if payload[field] != expected:
                raise ValueError("result frame identity mismatch")
        common = {
            key: payload[key]
            for key in ("completion_token", "reservation_callback_id", "result_id")
        }
        spec = _chunked_spec(self.journal.load(attempt_id))
        if envelope["type"] == "RESULT_FILE_BATCH" and spec is not None:
            self._summary_batch(attempt_id, envelope, state, spec)
            return
        if envelope["type"] == "RESULT_FILE_BATCH":
            # B09 CPU has precisely one output batch. No checkpoint/chunk capability implied.
            if payload["batch_index"] != 0 or payload["batch_count"] != 1:
                raise ValueError("unsupported CPU result batch count")
            bindings = [
                self._upload(attempt_id, sequence, desc)[0] for desc in payload["artifacts"]
            ]
            self.control(
                attempt_id,
                f"bind:{sequence}",
                "BIND_ARTIFACT_BATCH",
                {
                    "purpose": "RESULT",
                    "reservation_callback_id": payload["reservation_callback_id"],
                    "reserved_id": payload["result_id"],
                    "source_message_sequence": sequence,
                    "bindings": bindings,
                },
            )
            self.control(
                attempt_id,
                f"finalize:{sequence}",
                "FINALIZE_RESULT_MANIFEST",
                {**common, "binding_set_checksum": checksum(bindings)},
            )
            return
        if envelope["type"] != "RESULT_READY":
            raise ValueError("unsupported result frame")
        sent = state.get("completion_request")
        if sent is not None:
            # A sent completion replays its exact request; the stopped container's
            # output need not be readable again.
            self._complete(attempt_id, authority, state["completion_callback_id"], sent)
            return
        binding, raw = self._upload(attempt_id, sequence, payload["manifest"])
        manifest = json.loads(raw)
        aux = self._load(attempt_id).get("chunk_manifest_binding")
        if spec is not None and aux is None:
            raise ValueError("chunked result has no bound chunk-output manifest")
        if (
            manifest.get("chunk_output_manifest")
            != (None if spec is None else _inference().manifest_reference(aux))
            or (spec is None and "chunk_output_manifest" in manifest)
            or manifest["result_id"] != payload["result_id"]
            or rfc8785.dumps(manifest) != raw
            or manifest["manifest_checksum"]
            != checksum(
                {key: value for key, value in manifest.items() if key != "manifest_checksum"}
            )
        ):
            raise ValueError("result manifest bytes are not canonical or reserved")
        state = self._load(attempt_id)
        if state.get("completed"):
            return
        request = {"result_manifest_artifact_id": binding["artifact_id"], "manifest": manifest}
        callback = state.get("completion_callback_id")
        if callback is None:
            callback = str(new_uuid7())
        self._save(attempt_id, completion_callback_id=callback, completion_request=request)
        self._complete(attempt_id, authority, callback, request)

    def _bind(self, attempt_id, key, purpose, sequence, bindings):
        state = self._load(attempt_id)
        self.control(
            attempt_id,
            key,
            "BIND_ARTIFACT_BATCH",
            {
                "purpose": purpose,
                "reservation_callback_id": state["reservation_callback_id"],
                "reserved_id": state["reservation"]["result_id"],
                "source_message_sequence": sequence,
                "bindings": bindings,
            },
        )

    def _chunk_batch(self, attempt_id, envelope, state):
        inference = _inference()
        spec = _chunked_spec(self.journal.load(attempt_id))
        if spec is None or state.get("summary_binding") is not None:
            raise ValueError("chunk batch outside a chunked result")
        bindings = inference.chunk_batch(
            self.journal,
            self.client,
            self.read_output,
            attempt_id,
            envelope,
            spec=spec,
            reserved_id=state["reservation"]["result_id"],
            callback_id=state["reservation_callback_id"],
            error=ValueError,
        )
        sequence = envelope["message_sequence"]
        self._bind(attempt_id, f"chunk-bind:{sequence}", "CHUNK_OUTPUT", sequence, bindings)

    def _summary_batch(self, attempt_id, envelope, state, spec):
        """Inference summary: bound once every chunk is; finalize waits for the manifest."""
        inference = _inference()
        payload = envelope["payload"]
        sequence = envelope["message_sequence"]
        if (
            payload["batch_index"] != 0
            or payload["batch_count"] != 1
            or not isinstance(payload["artifacts"], list)
            or len(payload["artifacts"]) != 1
        ):
            raise ValueError("unsupported inference result batch")
        descriptor = inference.closed_descriptor(
            payload["artifacts"][0],
            name="summary.json",
            logical_name="summary.json",
            kind="RESULT_FILE",
            media_type="application/json",
            limit=inference_state.MAX_DOCUMENT_BYTES,
            error=ValueError,
        )
        binding, raw = self._upload(attempt_id, sequence, descriptor)
        record = self.journal.load(attempt_id)
        summary = inference.check_document(raw, record, spec, summary=True, error=ValueError)
        inference.require_all_chunks(
            record,
            spec,
            state["reservation"]["result_id"],
            summary["chunk_count"],
            error=ValueError,
        )
        self._save(
            attempt_id,
            summary_binding=binding,
            chunk_total=summary["chunk_count"],
            item_count=summary["item_count"],
        )
        self._bind(attempt_id, f"bind:{sequence}", "RESULT", sequence, [binding])

    def _chunk_manifest_ready(self, attempt_id, envelope, state):
        inference = _inference()
        record = self.journal.load(attempt_id)
        spec = _chunked_spec(record)
        summary = state.get("summary_binding")
        if spec is None or summary is None:
            raise ValueError("chunk-output manifest precedes the summary")
        sequence = envelope["message_sequence"]
        descriptor = inference.closed_descriptor(
            envelope["payload"].get("artifact"),
            name=f"result-{chunk_manifest.LOGICAL_NAME}",
            logical_name=chunk_manifest.LOGICAL_NAME,
            kind="CHUNK_OUTPUT_MANIFEST",
            media_type=chunk_manifest.MEDIA_TYPE,
            limit=chunk_manifest.MAX_MANIFEST_BYTES,
            error=ValueError,
        )
        binding, raw = self._upload(attempt_id, sequence, descriptor)
        inference.check_chunk_manifest(
            raw,
            record,
            spec,
            total=state["chunk_total"],
            item_count=state["item_count"],
            error=ValueError,
        )
        self._save(attempt_id, chunk_manifest_binding=binding)
        self._bind(attempt_id, f"aux-bind:{sequence}", "CHUNK_OUTPUT", sequence, [binding])
        self.control(
            attempt_id,
            f"finalize:{sequence}",
            "FINALIZE_RESULT_MANIFEST",
            {
                "completion_token": state["completion_token"],
                "reservation_callback_id": state["reservation_callback_id"],
                "result_id": state["reservation"]["result_id"],
                "binding_set_checksum": inference.binding_set_checksum(
                    self.journal.load(attempt_id),
                    state["reservation"]["result_id"],
                    [summary],
                    binding,
                ),
            },
        )

    def _complete(self, attempt_id, authority, callback, request):
        from dataclasses import asdict

        try:
            ack = self.client.complete(
                attempt_id, callback, {"authority": asdict(authority), **request}
            )
        except WorkerApiError as exc:
            raise_chunk_conflict(exc)
        if ack.get("accepted") is not True:
            raise ValueError("completion was not accepted")
        self._save(attempt_id, completed=True, completion_ack=ack)
        metrics_worker.execution("SUCCEEDED")

    def _upload(self, attempt_id, sequence, descriptor):
        state = self._load(attempt_id)
        key = descriptor_key(attempt_id, state["reservation_callback_id"], sequence, descriptor)
        # Validate bytes even after an earlier local binding was persisted.
        content = self.read_output(attempt_id, descriptor)
        if (
            len(content) != descriptor["size_bytes"]
            or "sha256:" + hashlib.sha256(content).hexdigest() != descriptor["checksum"]
        ):
            raise ValueError("closed result bytes mismatch")
        bindings = state.get("bindings", {})
        binding = bindings.get(key)
        if binding is None:
            artifact = self.client.upload_artifact(
                self.journal.load(attempt_id).authority, descriptor, key, content
            )
            for field in ("kind", "media_type", "size_bytes", "checksum"):
                if artifact[field] != descriptor[field]:
                    raise ValueError("committed result binding mismatch")
            binding = {**descriptor, "artifact_id": artifact["artifact_id"]}
            self._save(attempt_id, bindings={**bindings, key: binding})
        return binding, content


# Imported lazily: inference_flow imports this module's checksum helpers.
def _inference():
    from . import inference_flow

    return inference_flow


def _chunked_spec(record):
    return _inference().adapter_spec(record.execution_binding or {})
