"""Worker half of the B14 CPU checkpoint cycle.

Each server identity and byte binding is journaled before the next effect, so a
crash at any step replays the same checkpoint instead of reserving another. The
workload keeps running; checkpoint bytes are never logged.
"""

import hashlib
import json
from dataclasses import asdict

import rfc8785

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.workloads import adapter_launch, chunk_manifest, inference_state, training_state
from nexa.workloads.cpu_state import STATE_MAX_BYTES, CpuStateError, decode_state

from . import inference_flow
from .client import WorkerApiError
from .docker_client import ADAPTER_OUTPUT_MAX_BYTES
from .models import CpuCheckpointLaunch
from .result_flow import checksum, descriptor_key, raise_chunk_conflict

CHECKPOINT_DEADLINE_NS = 60 * 1_000_000_000
# A pause reserve refused while the Attempt is not RUNNING is retried this soon.
PAUSE_RETRY_NS = 1_000_000_000
_CHUNK_FRAMES = frozenset({"CHUNK_FILE_BATCH", "AUXILIARY_MANIFEST_READY"})
MANIFEST_MAX_BYTES = 64 * 1024
_PROVENANCE_CONTEXT = {
    "tenant_id": "tenant_id",
    "job_id": "job_id",
    "session_id": "logical_session_id",
    "input_checksum": "input_checksum",
    "template_id": "template_id",
    "template_version": "template_version",
    "adapter_id": "adapter_id",
    "adapter_version": "adapter_version",
    "image_digest": "image_digest",
}


class CheckpointProtocolError(ValueError):
    """A runner frame or server answer that can never complete this cycle."""


def _checkpoint_file_limit(adapter, logical_name):
    if adapter.chunked:
        return inference_state.MAX_DOCUMENT_BYTES
    if logical_name == training_state.STATE_FILE:
        return training_state.MAX_STATE_BYTES
    return ADAPTER_OUTPUT_MAX_BYTES


def _adapter_checkpoint(binding):
    """The v3 adapter launch of a checkpointing attempt, or None."""
    launch = binding.get("adapter_launch")
    if launch is None or launch["spec"]["checkpoint"] is None:
        return None
    return launch


def _checkpointing(binding):
    return binding.get("checkpoint") is not None or _adapter_checkpoint(binding) is not None


def _interval_seconds(binding):
    launch = _adapter_checkpoint(binding)
    source = launch if launch is not None else binding["checkpoint"]
    return int(source["interval_seconds"])


def reconcile_adoption(runner_state, transferred):
    """Return runner state matching the server's transferred reservations, or None.

    The server snapshot is authoritative for what is still RESERVED; the local
    journal must explain every difference by a step it durably recorded.
    """
    state = dict(runner_state or {})
    local = state.get("active_reservations") or {"checkpoint": None, "result": None}
    held_result, server_result = local.get("result"), transferred.get("result")
    if held_result != server_result:
        result_flow = state.get("result_flow") or {}
        if (
            held_result is not None
            or result_flow.get("reservation_callback_id") != server_result.get("callback_id")
            or result_flow.get("reservation") not in (None, server_result)
        ):
            return None
        # The result reserve committed before the worker journaled its answer;
        # the journaled callback id is the only request that could have made it.
        state["result_flow"] = {**result_flow, "reservation": server_result}
    flow = dict(state.get("checkpoint_flow") or {})
    cycle = flow.get("cycle")
    held, server = local.get("checkpoint"), transferred.get("checkpoint")
    if held == server:
        if held is None:
            if cycle is not None:
                # A reserve that never committed; its callback is bound to the old grant.
                flow["cycle"] = None
        elif not _cycle_holds(cycle, held):
            return None
        else:
            # The old publish callback may not replay under the new Authority.
            flow["cycle"] = {**cycle, "publish_callback_id": None}
    elif held is None:
        if (
            cycle is None
            or cycle.get("reservation") is not None
            or cycle.get("publish_callback_id") is not None
            or cycle.get("reserve_callback_id") != server.get("callback_id")
        ):
            return None
        flow["cycle"] = {**cycle, "reservation": server}
    elif server is None:
        if not _cycle_holds(cycle, held) or cycle.get("publish_callback_id") is None:
            return None
        # Publish resolved before adoption: COMMITTED or REJECTED, never reusable.
        flow["cycle"] = None
        flow["last_outcome"] = {
            "checkpoint_id": held["checkpoint_id"],
            "checkpoint_sequence": held["sequence"],
            "outcome": "RESOLVED_BEFORE_ADOPTION",
        }
    else:
        return None
    return {
        **state,
        "checkpoint_flow": flow,
        "active_reservations": {**local, "checkpoint": server, "result": server_result},
    }


def _cycle_holds(cycle, reservation):
    return (
        cycle is not None
        and cycle.get("reservation") == reservation
        and cycle.get("reserve_callback_id") == reservation.get("callback_id")
    )


class CheckpointFlow:
    def __init__(
        self, journal, client, read_output, control, *, monotonic_ns, next_due, pause_retry=None
    ):
        self.journal = journal
        self.client = client
        self.read_output = read_output
        self.control = control
        self.monotonic_ns = monotonic_ns
        # In-memory monotonic schedule; a restarted worker waits one full interval.
        self.next_due = next_due
        # In-memory backoff after a refused pause reserve; a restart retries at once.
        self.pause_retry = {} if pause_retry is None else pause_retry

    @staticmethod
    def _flow(record):
        return (record.runner_state or {}).get("checkpoint_flow") or {}

    def _update_flow(self, attempt_id, change):
        self.journal.update_runner_state(
            attempt_id,
            lambda local: {
                **local,
                "checkpoint_flow": change(dict(local.get("checkpoint_flow") or {})),
            },
        )

    def _save_cycle(self, attempt_id, **changes):
        def change(flow):
            return {**flow, "cycle": {**flow["cycle"], **changes}}

        self._update_flow(attempt_id, change)

    def cycle_open(self, attempt_id):
        return self._flow(self.journal.load(attempt_id)).get("cycle") is not None

    def _interval_ns(self, record):
        return _interval_seconds(record.execution_binding) * 1_000_000_000

    def tick(self, attempt_id, *, pause=False):
        """Resume an open cycle, or start one when the interval is due.

        With ``pause`` the server desires PAUSED: an open cycle becomes the pause
        checkpoint, otherwise one opens at once, even for a disabled schedule or
        a finished workload, until one commits or the server aborts the pause.
        """
        record = self.journal.load(attempt_id)
        if not _checkpointing(record.execution_binding):
            return
        state = record.runner_state or {}
        flow = self._flow(record)
        cycle = flow.get("cycle")
        if cycle is not None:
            self._resume(attempt_id, cycle)
            return
        progress = state.get("latest_progress")
        if pause:
            retry = self.pause_retry.get(attempt_id)
            if (
                progress is None
                or state.get("result_flow")
                or state.get("failure_resolution") is not None
                or flow.get("pause_checkpoint") is not None
                or flow.get("pause_aborted")
                or flow.get("pause_rejected")
                or (retry is not None and self.monotonic_ns() < retry)
            ):
                return
            self._open(attempt_id, "PAUSE")
            return
        if (
            flow.get("disabled")
            or state.get("result_flow")
            or state.get("deferred_result_prepare") is not None
            or state.get("failure_resolution") is not None
            or progress is None
            or progress.get("fraction", 0) >= 1
        ):
            return
        now = self.monotonic_ns()
        due = self.next_due.get(attempt_id)
        if due is None:
            self.next_due[attempt_id] = now + self._interval_ns(record)
            return
        if now < due:
            return
        self._open(attempt_id, "INTERVAL")

    def _open(self, attempt_id, reason):
        cycle = {
            "reason": reason,
            "reserve_callback_id": str(new_uuid7()),
            "reservation": None,
            "request": None,
            "files_sequence": None,
            "bindings": {},
            "cursor": None,
            "manifest_sequence": None,
            "manifest_binding": None,
            "publish_callback_id": None,
        }
        self._update_flow(attempt_id, lambda value: {**value, "cycle": cycle})
        self._resume(attempt_id, cycle)

    def _resume(self, attempt_id, cycle):
        record = self.journal.load(attempt_id)
        reservation = cycle["reservation"]
        if reservation is None:
            try:
                reservation = self.client.reserve_checkpoint(
                    attempt_id,
                    cycle["reserve_callback_id"],
                    {"authority": asdict(record.authority)},
                )
            except WorkerApiError as exc:
                if exc.status != 409:
                    raise
                if cycle.get("reason") == "PAUSE":
                    self._pause_refused(attempt_id)
                    return
                # Nothing is reserved under this callback; retry next interval.
                self._update_flow(attempt_id, lambda value: {**value, "cycle": None})
                self.next_due[attempt_id] = self.monotonic_ns() + self._interval_ns(record)
                return
            except ValueError as exc:
                # B14-R08: a 2xx answer that is not a JSON object ends this Attempt.
                raise CheckpointProtocolError("checkpoint reservation answer is invalid") from exc
            sequence = reservation.get("sequence")
            if (
                reservation.get("callback_id") != cycle["reserve_callback_id"]
                or reservation.get("attempt_id") != attempt_id
                or reservation.get("job_id") != record.execution_binding["context"]["job_id"]
                or not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence < 1
                or not isinstance(reservation.get("checkpoint_id"), str)
            ):
                raise CheckpointProtocolError("checkpoint reservation identity mismatch")

            def persist(local, reservation=reservation):
                flow = dict(local.get("checkpoint_flow") or {})
                flow["cycle"] = {**flow["cycle"], "reservation": reservation}
                active = local.get("active_reservations") or {"result": None}
                return {
                    **local,
                    "checkpoint_flow": flow,
                    "active_reservations": {**active, "checkpoint": reservation},
                }

            self.journal.update_runner_state(attempt_id, persist)
        request = cycle.get("request")
        if request is None:
            request = {
                "reason": cycle.get("reason", "INTERVAL"),
                "reservation_callback_id": cycle["reserve_callback_id"],
                "checkpoint_id": reservation["checkpoint_id"],
                "checkpoint_sequence": reservation["sequence"],
                # Frozen once: a replay must resend the byte-identical control.
                "checkpoint_deadline_monotonic_ns": self.monotonic_ns() + CHECKPOINT_DEADLINE_NS,
            }
            self._save_cycle(attempt_id, request=request)
        self.control(
            attempt_id,
            f"checkpoint-request:{cycle['reserve_callback_id']}",
            "REQUEST_CHECKPOINT",
            request,
        )

    def _pause_refused(self, attempt_id):
        """Close a pause cycle whose reserve the server refused with 409.

        A publish while PAUSING moves the Attempt to STOPPING, after which every
        reserve conflicts; the last committed checkpoint of this Attempt is then
        the pause checkpoint. The server re-checks that at cleanup.
        """

        def change(flow):
            flow = {**flow, "cycle": None}
            last = flow.get("last_outcome")
            if last is not None and last["outcome"] == "COMMITTED":
                flow["pause_checkpoint"] = last["checkpoint_id"]
            return flow

        self._update_flow(attempt_id, change)
        self.pause_retry[attempt_id] = self.monotonic_ns() + PAUSE_RETRY_NS

    def process(self, attempt_id, envelope):
        """Handle a checkpoint frame; the caller commits its sequence afterwards."""
        payload = envelope["payload"]
        flow = self._flow(self.journal.load(attempt_id))
        cycle = flow.get("cycle")
        last = flow.get("last_outcome")
        chunk_frame = envelope["type"] in _CHUNK_FRAMES
        checkpoint_id = payload.get("reserved_id" if chunk_frame else "checkpoint_id")
        if cycle is None or cycle.get("reservation") is None:
            if last is not None and checkpoint_id == last["checkpoint_id"]:
                return
            raise CheckpointProtocolError("checkpoint frame has no reservation")
        reservation = cycle["reservation"]
        identity = (
            (("purpose", "CHECKPOINT"),)
            if chunk_frame
            else (("checkpoint_sequence", reservation["sequence"]),)
        )
        for field, expected in (
            ("reservation_callback_id", cycle["reserve_callback_id"]),
            *identity,
        ):
            if payload.get(field) != expected:
                raise CheckpointProtocolError("checkpoint frame identity mismatch")
        if checkpoint_id != reservation["checkpoint_id"]:
            raise CheckpointProtocolError("checkpoint frame identity mismatch")
        if envelope["type"] == "CHUNK_FILE_BATCH":
            self._chunk_batch(attempt_id, envelope, cycle)
        elif envelope["type"] == "AUXILIARY_MANIFEST_READY":
            self._chunk_manifest_ready(attempt_id, envelope, cycle)
        elif envelope["type"] == "CHECKPOINT_FILES_READY":
            self._files_ready(attempt_id, envelope, cycle)
        elif envelope["type"] == "CHECKPOINT_READY":
            self._ready(attempt_id, envelope, cycle)
        else:
            raise CheckpointProtocolError("unsupported checkpoint frame")

    def _read(self, attempt_id, descriptor):
        try:
            content = self.read_output(attempt_id, descriptor)
        except CheckpointProtocolError:
            raise
        except ValueError as exc:
            # The closed bytes contradict the runner's descriptor or container identity.
            raise CheckpointProtocolError("closed checkpoint output is invalid") from exc
        if (
            len(content) != descriptor["size_bytes"]
            or "sha256:" + hashlib.sha256(content).hexdigest() != descriptor["checksum"]
        ):
            raise CheckpointProtocolError("closed checkpoint bytes mismatch")
        return content

    def _upload(self, attempt_id, cycle, sequence, descriptor, content):
        key = descriptor_key(
            attempt_id, cycle["reserve_callback_id"], sequence, descriptor, purpose="CHECKPOINT"
        )
        bindings = self._flow(self.journal.load(attempt_id))["cycle"]["bindings"]
        binding = bindings.get(key)
        if binding is None:
            try:
                artifact = self.client.upload_artifact(
                    self.journal.load(attempt_id).authority, descriptor, key, content
                )
            except ValueError as exc:
                # A committed answer that contradicts the descriptor never binds.
                raise CheckpointProtocolError("checkpoint upload answer is invalid") from exc
            for field in ("kind", "media_type", "size_bytes", "checksum"):
                if artifact[field] != descriptor[field]:
                    raise CheckpointProtocolError("committed checkpoint binding mismatch")
            binding = {**descriptor, "artifact_id": artifact["artifact_id"]}
            self._save_cycle(attempt_id, bindings={**bindings, key: binding})
        return binding

    @staticmethod
    def _descriptor(descriptor, *, name, logical_name, kind, limit, media_type="application/json"):
        if (
            not isinstance(descriptor, dict)
            or set(descriptor)
            != {"staging_name", "logical_name", "kind", "media_type", "size_bytes", "checksum"}
            or descriptor["staging_name"] != name
            or descriptor["logical_name"] != logical_name
            or descriptor["kind"] != kind
            or descriptor["media_type"] != media_type
            or not isinstance(descriptor["size_bytes"], int)
            or isinstance(descriptor["size_bytes"], bool)
            or not 0 < descriptor["size_bytes"] <= limit
        ):
            raise CheckpointProtocolError("checkpoint descriptor is not closed")
        return descriptor

    def _files_ready(self, attempt_id, envelope, cycle):
        record = self.journal.load(attempt_id)
        launch = _adapter_checkpoint(record.execution_binding)
        if launch is not None:
            self._adapter_files_ready(attempt_id, envelope, cycle, record, launch)
            return
        payload = envelope["payload"]
        sequence = envelope["message_sequence"]
        checkpoint_sequence = cycle["reservation"]["sequence"]
        if (
            payload.get("batch_index") != 0
            or payload.get("batch_count") != 1
            or not isinstance(payload.get("artifacts"), list)
            or len(payload["artifacts"]) != 1
        ):
            raise CheckpointProtocolError("unsupported CPU checkpoint batch")
        if cycle["files_sequence"] not in (None, sequence):
            raise CheckpointProtocolError("checkpoint files arrived twice")
        descriptor = self._descriptor(
            payload["artifacts"][0],
            name=f"checkpoint-{checkpoint_sequence}-state.json",
            logical_name="state.json",
            kind="CHECKPOINT_FILE",
            limit=STATE_MAX_BYTES,
        )
        if cycle["files_sequence"] is None:
            self._save_cycle(attempt_id, files_sequence=sequence)
        record = self.journal.load(attempt_id)
        workload = record.execution_binding["cpu_workload"]
        content = self._read(attempt_id, descriptor)
        try:
            state = decode_state(
                content,
                iterations=workload["iterations"],
                modulus=workload["modulus"],
                input_checksum=record.execution_binding["context"]["input_checksum"],
                spec_checksum=workload["spec_checksum"],
            )
        except CpuStateError as exc:
            raise CheckpointProtocolError("checkpoint state is invalid") from exc
        restore = record.execution_binding["checkpoint"].get("restore")
        if restore is not None and state.step < restore["step"]:
            raise CheckpointProtocolError("checkpoint state regressed below the restore")
        cursor = {"step": state.step, "accumulator": state.accumulator}
        if cycle.get("cursor") not in (None, cursor):
            raise CheckpointProtocolError("checkpoint cursor changed on replay")
        binding = self._upload(attempt_id, cycle, sequence, descriptor, content)
        self._save_cycle(attempt_id, cursor=cursor)
        self._bind_and_finalize(attempt_id, cycle, sequence, [binding])

    def _adapter_files_ready(self, attempt_id, envelope, cycle, record, launch):
        """Training checkpoint: every file is read and the state validated before upload."""
        payload = envelope["payload"]
        sequence = envelope["message_sequence"]
        checkpoint_sequence = cycle["reservation"]["sequence"]
        spec = launch["spec"]
        adapter = adapter_launch.adapter_for(spec)
        rules = adapter.checkpoint_files
        if (
            payload.get("batch_index") != 0
            or payload.get("batch_count") != 1
            or not isinstance(payload.get("artifacts"), list)
            or len(payload["artifacts"]) != len(rules)
        ):
            raise CheckpointProtocolError("unsupported adapter checkpoint batch")
        if cycle["files_sequence"] not in (None, sequence):
            raise CheckpointProtocolError("checkpoint files arrived twice")
        descriptors = [
            self._descriptor(
                item,
                name=f"checkpoint-{checkpoint_sequence}-{rule.logical_name}",
                logical_name=rule.logical_name,
                kind="CHECKPOINT_FILE",
                media_type=rule.media_type,
                limit=_checkpoint_file_limit(adapter, rule.logical_name),
            )
            for item, rule in zip(payload["artifacts"], rules, strict=True)
        ]
        if cycle["files_sequence"] is None:
            self._save_cycle(attempt_id, files_sequence=sequence)
        contents = {d["logical_name"]: self._read(attempt_id, d) for d in descriptors}
        if adapter.chunked:
            self._inference_files_ready(
                attempt_id, cycle, record, spec, sequence, descriptors[0], contents
            )
            return
        try:
            document = training_state.validate_checkpoint_files(contents)
            adapter_launch.check_training_state(
                document,
                parameters=spec["parameters"],
                threads=spec["threads"],
                input_checksum=spec["provenance"]["input_checksum"],
                spec_checksum=spec["spec_checksum"],
            )
            cursor = adapter_launch.validate_training_cursor(
                training_state.runtime_cursor(document), spec["parameters"]
            )
        except (training_state.TrainingStateError, adapter_launch.LaunchSpecError) as exc:
            raise CheckpointProtocolError("checkpoint state is invalid") from exc
        restore = spec["restore"]
        if restore is not None and cursor["step"] < restore["cursor"]["step"]:
            raise CheckpointProtocolError("checkpoint state regressed below the restore")
        if cycle.get("cursor") not in (None, cursor):
            raise CheckpointProtocolError("checkpoint cursor changed on replay")
        bindings = [
            self._upload(attempt_id, cycle, sequence, d, contents[d["logical_name"]])
            for d in descriptors
        ]
        self._save_cycle(attempt_id, cursor=cursor)
        self._bind_and_finalize(attempt_id, cycle, sequence, bindings)

    def _inference_files_ready(
        self, attempt_id, cycle, record, spec, sequence, descriptor, contents
    ):
        """Inference state: every chunk before its cursor was bound in this reservation."""
        document = inference_flow.check_document(
            contents[descriptor["logical_name"]],
            record,
            spec,
            summary=False,
            error=CheckpointProtocolError,
        )
        try:
            cursor = adapter_launch.validate_inference_cursor(
                inference_state.runtime_cursor(document)
            )
        except adapter_launch.LaunchSpecError as exc:
            raise CheckpointProtocolError("checkpoint state is invalid") from exc
        restore = spec["restore"]
        if restore is not None and cursor["step"] < restore["cursor"]["step"]:
            raise CheckpointProtocolError("checkpoint state regressed below the restore")
        if cycle.get("cursor") not in (None, cursor):
            raise CheckpointProtocolError("checkpoint cursor changed on replay")
        inference_flow.require_all_chunks(
            record,
            spec,
            cycle["reservation"]["checkpoint_id"],
            cursor["step"],
            error=CheckpointProtocolError,
        )
        binding = self._upload(
            attempt_id, cycle, sequence, descriptor, contents[descriptor["logical_name"]]
        )
        self._save_cycle(
            attempt_id,
            cursor=cursor,
            item_count=document["item_count"],
            state_binding=binding,
        )
        # The binding set closes with the chunk-output manifest (AUXILIARY_MANIFEST_READY).
        self._bind(attempt_id, cycle, sequence, [binding], purpose="CHECKPOINT")

    def _chunk_batch(self, attempt_id, envelope, cycle):
        record = self.journal.load(attempt_id)
        spec = inference_flow.adapter_spec(record.execution_binding)
        if spec is None or cycle.get("cursor") is not None:
            raise CheckpointProtocolError("chunk batch outside a chunked checkpoint cycle")
        bindings = inference_flow.chunk_batch(
            self.journal,
            self.client,
            self._read,
            attempt_id,
            envelope,
            spec=spec,
            reserved_id=cycle["reservation"]["checkpoint_id"],
            callback_id=cycle["reserve_callback_id"],
            error=CheckpointProtocolError,
        )
        self._bind(
            attempt_id,
            cycle,
            envelope["message_sequence"],
            bindings,
            purpose="CHUNK_OUTPUT",
            key="checkpoint-chunk-bind",
        )

    def _chunk_manifest_ready(self, attempt_id, envelope, cycle):
        """The runner's chunk-output manifest must list exactly the journaled chunks."""
        record = self.journal.load(attempt_id)
        spec = inference_flow.adapter_spec(record.execution_binding)
        state_binding = cycle.get("state_binding")
        if spec is None or state_binding is None:
            raise CheckpointProtocolError("chunk-output manifest precedes the state file")
        sequence = envelope["message_sequence"]
        if cycle.get("aux_sequence") not in (None, sequence):
            raise CheckpointProtocolError("chunk-output manifest arrived twice")
        descriptor = self._descriptor(
            envelope["payload"].get("artifact"),
            name=f"checkpoint-{cycle['reservation']['sequence']}-{chunk_manifest.LOGICAL_NAME}",
            logical_name=chunk_manifest.LOGICAL_NAME,
            kind="CHUNK_OUTPUT_MANIFEST",
            media_type=chunk_manifest.MEDIA_TYPE,
            limit=chunk_manifest.MAX_MANIFEST_BYTES,
        )
        if cycle.get("aux_sequence") is None:
            self._save_cycle(attempt_id, aux_sequence=sequence)
        raw = self._read(attempt_id, descriptor)
        inference_flow.check_chunk_manifest(
            raw,
            record,
            spec,
            total=cycle["cursor"]["step"],
            item_count=cycle["item_count"],
            error=CheckpointProtocolError,
        )
        binding = self._upload(attempt_id, cycle, sequence, descriptor, raw)
        self._save_cycle(attempt_id, aux_binding=binding)
        self._bind(
            attempt_id,
            cycle,
            sequence,
            [binding],
            purpose="CHUNK_OUTPUT",
            key="checkpoint-aux-bind",
        )
        self._finalize(
            attempt_id,
            cycle,
            sequence,
            inference_flow.binding_set_checksum(
                self.journal.load(attempt_id),
                cycle["reservation"]["checkpoint_id"],
                [state_binding],
                binding,
            ),
        )

    def _bind(self, attempt_id, cycle, sequence, bindings, *, purpose, key="checkpoint-bind"):
        self.control(
            attempt_id,
            f"{key}:{sequence}",
            "BIND_ARTIFACT_BATCH",
            {
                "purpose": purpose,
                "reservation_callback_id": cycle["reserve_callback_id"],
                "reserved_id": cycle["reservation"]["checkpoint_id"],
                "source_message_sequence": sequence,
                "bindings": bindings,
            },
        )

    def _bind_and_finalize(self, attempt_id, cycle, sequence, bindings):
        self._bind(attempt_id, cycle, sequence, bindings, purpose="CHECKPOINT")
        self._finalize(attempt_id, cycle, sequence, checksum(bindings))

    def _finalize(self, attempt_id, cycle, sequence, binding_set_checksum):
        checkpoint_sequence = cycle["reservation"]["sequence"]
        self.control(
            attempt_id,
            f"checkpoint-finalize:{sequence}",
            "FINALIZE_CHECKPOINT_MANIFEST",
            {
                "reservation_callback_id": cycle["reserve_callback_id"],
                "checkpoint_id": cycle["reservation"]["checkpoint_id"],
                "checkpoint_sequence": checkpoint_sequence,
                "binding_set_checksum": binding_set_checksum,
            },
        )

    def _expected_manifest(self, record, cycle):
        binding = record.execution_binding
        files = [
            {key: value for key, value in item.items() if key not in {"staging_name", "kind"}}
            for item in cycle["bindings"].values()
            if item["kind"] == "CHECKPOINT_FILE"
        ]
        launch = _adapter_checkpoint(binding)
        if launch is not None:
            spec = launch["spec"]
            adapter = adapter_launch.adapter_for(spec)
            # Journaled bindings are keyed by digest; the manifest lists files in rule order.
            order = {
                rule.logical_name: index for index, rule in enumerate(adapter.checkpoint_files)
            }
            if sorted(item["logical_name"] for item in files) != sorted(order):
                raise CheckpointProtocolError("checkpoint files do not match the adapter")
            files.sort(key=lambda item: order[item["logical_name"]])
            return (
                spec["provenance"],
                spec["checkpoint"]["compatibility"],
                cycle["cursor"],
                files,
                list(adapter.state_components),
            )
        context = binding["context"]
        launch = binding["checkpoint"]
        provenance = {key: context[field] for key, field in _PROVENANCE_CONTEXT.items()}
        provenance.update(
            attempt_id=record.attempt_id,
            job_fence=context["authority"]["job_fence"],
            spec_checksum=binding["cpu_workload"]["spec_checksum"],
        )
        compatibility = CpuCheckpointLaunch(
            framework_version=launch["framework_version"],
            restart_safe=launch["restart_safe"],
            interval_seconds=launch["interval_seconds"],
        ).compatibility(context["architecture"])
        step = cycle["cursor"]["step"]
        return (
            provenance,
            compatibility,
            {
                "step": step,
                "epoch": 0,
                "item_cursor": step,
                "accumulator": cycle["cursor"]["accumulator"],
            },
            files,
            ["ACCUMULATOR"],
        )

    def _ready(self, attempt_id, envelope, cycle):
        sequence = envelope["message_sequence"]
        reservation = cycle["reservation"]
        if cycle["files_sequence"] is None or cycle.get("cursor") is None:
            raise CheckpointProtocolError("checkpoint manifest precedes its files")
        chunked = inference_flow.adapter_spec(self.journal.load(attempt_id).execution_binding)
        if chunked is not None and cycle.get("aux_binding") is None:
            raise CheckpointProtocolError("checkpoint manifest precedes its chunk-output manifest")
        if cycle["manifest_sequence"] not in (None, sequence):
            raise CheckpointProtocolError("checkpoint manifest arrived twice")
        descriptor = self._descriptor(
            envelope["payload"].get("manifest"),
            name=f"checkpoint-{reservation['sequence']}-manifest.json",
            logical_name="checkpoint.manifest.json",
            kind="CHECKPOINT_MANIFEST",
            limit=MANIFEST_MAX_BYTES,
        )
        record = self.journal.load(attempt_id)
        raw = self._read(attempt_id, descriptor)
        try:
            manifest = json.loads(raw)
            canonical = isinstance(manifest, dict) and rfc8785.dumps(manifest) == raw
        except (UnicodeDecodeError, ValueError, rfc8785.CanonicalizationError):
            canonical = False
        if not canonical:
            raise CheckpointProtocolError("checkpoint manifest is not canonical JSON")
        provenance, compatibility, cursor, files, components = self._expected_manifest(
            record, cycle
        )
        body = {key: value for key, value in manifest.items() if key != "manifest_checksum"}
        if (
            manifest.get("manifest_checksum") != checksum(body)
            or manifest.get("kind") != "CHECKPOINT"
            or manifest.get("schema_version") != 1
            or manifest.get("checkpoint_id") != reservation["checkpoint_id"]
            or manifest.get("checkpoint_sequence") != reservation["sequence"]
            or manifest.get("provenance") != provenance
            or manifest.get("compatibility") != compatibility
            or manifest.get("cursor") != cursor
            or manifest.get("state_components") != components
            or manifest.get("files") != files
            or manifest.get("chunk_output_manifest")
            != (
                None if chunked is None else inference_flow.manifest_reference(cycle["aux_binding"])
            )
            or (chunked is None and "chunk_output_manifest" in manifest)
        ):
            raise CheckpointProtocolError("checkpoint manifest does not match the cycle")
        if cycle["manifest_sequence"] is None:
            self._save_cycle(attempt_id, manifest_sequence=sequence)
        binding = self._upload(attempt_id, cycle, sequence, descriptor, raw)
        if cycle.get("manifest_binding") not in (None, binding):
            raise CheckpointProtocolError("checkpoint manifest binding changed")
        self._save_cycle(attempt_id, manifest_binding=binding)
        callback = self._flow(self.journal.load(attempt_id))["cycle"]["publish_callback_id"]
        if callback is None:
            callback = str(new_uuid7())
            self._save_cycle(attempt_id, publish_callback_id=callback)
        record = self.journal.load(attempt_id)
        try:
            published = self.client.publish_checkpoint(
                attempt_id,
                callback,
                {
                    "authority": asdict(record.authority),
                    "manifest_artifact_id": binding["artifact_id"],
                    "manifest": manifest,
                },
            )
        except WorkerApiError as exc:
            if exc.status != 422:
                raise_chunk_conflict(exc)
            # A deterministic manifest defect: the server ended this identity
            # and returned the attempt to RUNNING. Further cycles would repeat it.
            self._close(attempt_id, record, reservation, outcome="REJECTED", disable=True)
            return
        except ValueError as exc:
            raise CheckpointProtocolError("checkpoint publish answer is invalid") from exc
        if (
            published.get("checkpoint_id") != reservation["checkpoint_id"]
            or published.get("attempt_id") != attempt_id
            or published.get("sequence") != reservation["sequence"]
            or published.get("manifest_artifact_id") != binding["artifact_id"]
            or published.get("state") != "COMMITTED"
        ):
            raise CheckpointProtocolError("published checkpoint record mismatch")
        self._close(attempt_id, record, reservation, outcome="COMMITTED", disable=False)

    def _close(self, attempt_id, record, reservation, *, outcome, disable):
        def change(local):
            flow = dict(local.get("checkpoint_flow") or {})
            flow["cycle"] = None
            flow["last_outcome"] = {
                "checkpoint_id": reservation["checkpoint_id"],
                "checkpoint_sequence": reservation["sequence"],
                "outcome": outcome,
            }
            if disable:
                flow["disabled"] = True
            if local.get("control_desired") == "PAUSED":
                # Publish while PAUSING either stops the Attempt (COMMITTED) or
                # aborts the pause and returns it to RUNNING (REJECTED). A
                # CHECKPOINT_FOR_PAUSE Attempt cannot run on, so the server keeps
                # it PAUSING and the worker fails it instead (B15-R28).
                if outcome == "COMMITTED":
                    flow["pause_checkpoint"] = reservation["checkpoint_id"]
                elif local.get("checkpoint_for_pause"):
                    flow["pause_rejected"] = True
                else:
                    flow["pause_aborted"] = True
            active = local.get("active_reservations") or {"result": None}
            return {
                **local,
                "checkpoint_flow": flow,
                "active_reservations": {**active, "checkpoint": None},
            }

        self.journal.update_runner_state(attempt_id, change)
        self.next_due[attempt_id] = self.monotonic_ns() + self._interval_ns(record)


__all__ = ["CheckpointFlow", "CheckpointProtocolError", "reconcile_adoption"]
