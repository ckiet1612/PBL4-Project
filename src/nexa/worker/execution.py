"""CPU dispatch and result orchestration layered over B09/B10 lifecycle primitives."""

import hashlib
from contextlib import nullcontext, suppress
from dataclasses import asdict, replace

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.observability import metrics_worker
from nexa.workloads import adapter_launch

from .adapter_dispatch import (
    RECOGNIZED_DIRECTORY,
    ChunkOutputUnavailable,
    adapter_checkpoint,
    adapter_downloads,
    adapter_execution_request,
    adapter_of,
    adapter_recognized,
    recognized_downloads,
    verify_adapter_restore_files,
    verify_adapter_restore_manifest,
)
from .checkpoint_flow import CheckpointFlow, CheckpointProtocolError
from .client import WorkerApiError
from .dispatch import (
    RestoreUnavailable,
    checkpoint_launch,
    execution_request,
    verify_restore_manifest,
    verify_restore_state,
)
from .docker_client import DockerContainerNotFound
from .errors import ExecutorError, ExecutorErrorCode
from .models import Authority
from .protocol import (
    STOP_EXIT_CODES,
    FrameDecoder,
    SequenceState,
    canonical_envelope_effect,
    encode_frame,
    validate_ack,
)
from .result_flow import ChunkOutputConflict, ResultFlow
from .runner_control import RunnerControl, RunnerControlError

_CHECKPOINT_FRAMES = {"CHECKPOINT_FILES_READY", "CHECKPOINT_READY"}
# B16 inference chunk frames belong to the checkpoint cycle or the result by purpose.
_CHUNK_FRAMES = {"CHUNK_FILE_BATCH", "AUXILIARY_MANIFEST_READY"}
# A pause that commits no checkpoint and stops nothing ends as a runner failure.
PAUSE_DEADLINE_NS = 40 * 1_000_000_000
PAUSE_STOP_GRACE_NS = 5 * 1_000_000_000
# Runner FAILED reasons forwarded verbatim; any other runner text stays internal.
_FORWARDED_FAILURES = {
    ("INCOMPATIBLE", "CHECKPOINT_RESTORE_UNAVAILABLE"),
    # B16 adapter runners classify a rejected dataset or a non-finite training state.
    ("INVALID_INPUT", "INVALID_INPUT"),
    ("INTERNAL", "INVALID_RESULT"),
    # B16-R21: the workload wrote output for a recognized chunk, or a mounted
    # recognized chunk file is not the claimed artifact.
    ("INTERNAL", "CHUNK_OUTPUT_CONFLICT"),
    ("INTERNAL", "CHUNK_OUTPUT_UNAVAILABLE"),
    # B14-K5: no space or an I/O error while the runner copied checkpoint bytes.
    ("INTERNAL", "CHECKPOINT_STORAGE_FAILED"),
    # The runner saw its container cgroup ``oom_kill`` rise while the workload ran (B16-R26).
    ("OOM", "CONTAINER_OOM"),
}
# A runner that stops on its own deadline names the cause; any other stop is internal.
_STOP_FAILURES = {
    "RUNTIME_LIMIT": ("TIMEOUT", "RUNTIME_LIMIT_REACHED"),
    # The runner never started its workload within the startup limit (B15-R10).
    "STARTUP_LIMIT": ("TIMEOUT", "STARTUP_TIMEOUT"),
    # Renewals stopped reaching the API: lost authority is retryable infrastructure.
    "LEASE_DEADLINE": ("INFRASTRUCTURE", "RUNNER_UNAVAILABLE"),
}
_EXIT_STOP_REASONS = {code: reason for reason, code in STOP_EXIT_CODES.items()}


def _matches_descriptor(path, descriptor):
    if path.stat().st_size != descriptor["size_bytes"]:
        return False
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() == descriptor["checksum"]


def runner_stop_failure(stop_reason, state):
    """Classify a runner stop the same way from its STOPPED frame or its exit status."""
    if stop_reason == "FAILURE" and state.get("checkpoint_rejected"):
        # The runner rejected this worker's checkpoint control itself.
        return "INTERNAL", "CHECKPOINT_PROTOCOL_ERROR"
    return _STOP_FAILURES.get(stop_reason, ("INTERNAL", "WORKLOAD_EXIT_NONZERO"))


def terminal_frame_failure(envelope, state):
    """Classify a runner FAILED or STOPPED frame; unknown runner text stays internal."""
    if envelope["type"] == "STOPPED":
        return runner_stop_failure(envelope["payload"].get("reason"), state)
    payload = envelope["payload"]
    reason = (payload.get("failure_class"), payload.get("reason_code"))
    if reason not in _FORWARDED_FAILURES or (reason[0] == "OOM") != (
        payload.get("oom_killed") is True
    ):
        return "INTERNAL", "WORKLOAD_EXIT_NONZERO"
    return reason


def container_exit_failure(exited, state=None):
    """Classify a workload container that stopped without a runner terminal frame."""
    if exited["oom_killed"]:
        return "OOM", "CONTAINER_OOM"
    stop_reason = _EXIT_STOP_REASONS.get(exited["exit_code"])
    if stop_reason is not None:
        # No worker kept the STOPPED frame; the exit status names its reason (B15-R14).
        return runner_stop_failure(stop_reason, state or {})
    if exited["exit_code"] == 0:
        # The runner exits 0 only after a terminal frame this worker never saw.
        return "INTERNAL", "RUNNER_PROTOCOL_ERROR"
    return "INFRASTRUCTURE", "RUNNER_UNAVAILABLE"


class RunnerControlRejected(RunnerControlError):
    """The runner answered a control frame with a definite rejection."""

    def __init__(self, code):
        super().__init__(f"runner rejected control: {code}")
        self.code = code


class AuthorityControlPending(RunnerControlError):
    """A deadline operation owns the next control sequence; retry later."""


class WorkerExecutionMixin:
    def _pending_callback(self, operation, attempt_id, body, **details):
        # ``details`` are local facts journaled with the first send; a replay
        # keeps the original values.
        for callback, record in self.state.operations.items():
            if (
                record["operation"] == operation
                and record["payload"].get("attempt_id") == attempt_id
            ):
                if record["payload"]["body"] != body:
                    raise ValueError("pending callback immutable payload changed")
                return callback, record
        callback = str(new_uuid7())
        record = self.state.begin(
            callback,
            operation=operation,
            payload={"attempt_id": attempt_id, "body": body, **details},
        )
        return callback, record

    def _dispatch_offer(self, offer):
        authority = Authority(**offer["authority"])
        attempt_id = authority.attempt_id
        if (
            authority.worker_id != self.worker_id
            or authority.worker_incarnation_id != self.incarnation_id
        ):
            raise ValueError("dispatch offer authority mismatch")
        if attempt_id in self._adopted:
            return
        if (
            self.journal is not None
            and self.journal.exists(attempt_id)
            and (self.journal.load(attempt_id).runner_state or {}).get("cleanup_verified")
            is not None
        ):
            # Released for good: its claims were discarded with the verified cleanup
            # and a new one could only be rejected (REM-R06).
            return
        if self.executor is None:
            raise RuntimeError("dispatch requires configured executor")
        callback, pending = self._pending_callback(
            "claim",
            attempt_id,
            {"authority": asdict(authority)},
            offered_checkpoint=isinstance(offer.get("checkpoint"), dict),
        )
        first_claim_send = self.state.first_send(callback)
        claim = pending["acknowledgment"]
        if claim is None:
            claim = self.client.claim(attempt_id, callback, pending["payload"]["body"])
            if claim.get("accepted") is not True or claim.get("callback_id") != callback:
                raise ValueError("claim callback acknowledgment mismatch")
            self.state.acknowledge(callback, claim)
        context = claim["execution_context"]
        if context["authority"] != asdict(authority):
            raise ValueError("claimed execution authority mismatch")
        directory = self.executor.staging_root / "downloads" / attempt_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink() or directory.parent.is_symlink():
            raise ValueError("input staging directory is a symlink")
        source = directory / "input.json"
        architecture = self.provider.discover().architecture
        adapter = adapter_of(context)
        # Nothing is journaled before prepare, so a pre-launch failure can
        # tombstone against the launch-free request.
        if adapter is None:
            request = execution_request(
                {**context, "restore_checkpoint": None},
                source,
                architecture,
                limits=self.executor.limits,
            )
            downloads = [(context["input_artifacts"][0], source)]
        else:
            request = adapter_execution_request(
                {**context, "restore_checkpoint": None},
                directory,
                architecture,
                limits=self.executor.limits,
            )
            downloads = adapter_downloads(context, directory)
        start_callback = None
        identity = None
        try:
            if self.monotonic_ns() >= first_claim_send + 30_000_000_000:
                # The budget does not name a runner that Docker shows already exited.
                exited = self._observe_container_exit(attempt_id)
                if exited is not None:
                    self._fail_exited_startup(attempt_id, request, exited)
                    return
                raise TimeoutError("claim startup budget elapsed")
            try:
                if (
                    context["restore_checkpoint"] is None
                    and pending["payload"].get("offered_checkpoint") is True
                    and context["template_snapshot"].get("restart_safe") is not True
                ):
                    # The Job has checkpoint state but the claim found none
                    # restorable; input replay is not allowed for this template.
                    raise RestoreUnavailable("claim froze no restore for a checkpointed Job")
                if adapter is None:
                    launch, restore_file = self._checkpoint_launch(
                        authority, context, directory, architecture
                    )
                else:
                    launch = self._adapter_checkpoint_launch(
                        authority, context, adapter, directory, architecture
                    )
            except ChunkOutputUnavailable:
                # A recognized chunk the claim cannot hand over must not be
                # recomputed under this Attempt's source (B16-R21).
                if self._execution_failed(
                    attempt_id,
                    request=request,
                    failure_class="INTERNAL",
                    reason_code="CHUNK_OUTPUT_UNAVAILABLE",
                ):
                    self._retire_fenced_startup(attempt_id)
                return
            except RestoreUnavailable:
                # Never fall back to a from-zero run: the claim froze a restore.
                if self._execution_failed(
                    attempt_id,
                    request=request,
                    failure_class="INCOMPATIBLE",
                    reason_code="CHECKPOINT_RESTORE_UNAVAILABLE",
                ):
                    self._retire_fenced_startup(attempt_id)
                return
            if adapter is None:
                request = execution_request(
                    context,
                    source,
                    architecture,
                    checkpoint=launch,
                    restore_file=restore_file,
                    restore_source=directory / "restore-state.json",
                    limits=self.executor.limits,
                )
            else:
                checkpoint, restore, restore_files, recognized = launch
                request = adapter_execution_request(
                    context,
                    directory,
                    architecture,
                    checkpoint=checkpoint,
                    restore=restore,
                    restore_files=restore_files,
                    recognized=recognized,
                    limits=self.executor.limits,
                )
            for artifact, target in downloads:
                if target.exists() and not _matches_descriptor(target, artifact):
                    # A worker killed mid-download leaves a partial private file.
                    target.unlink()
                if not target.exists():
                    self.client.download_execution(authority, artifact, target)
            # Executor performs descriptor verification on every prepare/start replay.
            prepared = self.executor.prepare(request)
            identity = self.executor.start(prepared)
            self._containers[identity.container_id] = identity
            record = self.journal.load(attempt_id)
            body = {
                "authority": asdict(authority),
                "startup_nonce": record.startup_nonce,
                "executor_operation_sequence": record.operation_sequence,
                "container": {
                    "container_id": identity.container_id,
                    "runtime_identity_digest": identity.runtime_identity_digest,
                },
            }
            start_callback, start_pending = self._pending_callback("start", attempt_id, body)
            first_send = self.state.first_send(start_callback)
            acknowledgment = start_pending["acknowledgment"]
            with self.journal.lock(attempt_id):
                if acknowledgment is None:
                    acknowledgment = self.client.start(attempt_id, start_callback, body)
                    if (
                        acknowledgment.get("accepted") is not True
                        or acknowledgment.get("callback_id") != start_callback
                    ):
                        raise ValueError("start callback acknowledgment mismatch")
                    sequence = self._next_control_sequence(self.journal.load(attempt_id))
                    self.state.acknowledge(
                        start_callback, acknowledgment, control_sequence=sequence
                    )
                else:
                    sequence = start_pending["control_sequence"]
                if not self._apply_deadline(
                    attempt_id, identity, start_callback, first_send, acknowledgment, sequence
                ):
                    # Only a Docker-proven exit of the bound container ends the
                    # startup early; a runner still starting keeps the replay until
                    # the startup budget ends as STARTUP_TIMEOUT (B15-R11).
                    exited = self._observe_container_exit(attempt_id)
                    if exited is None:
                        raise RunnerControlError("start authority deadline was not accepted")
                    self._fail_exited_startup(attempt_id, request, exited)
                    return
                # A CHECKPOINT_FOR_PAUSE Attempt exists only to checkpoint and stop.
                paused = (
                    {"control_desired": "PAUSED", "checkpoint_for_pause": True}
                    if context.get("execution_intent") == "CHECKPOINT_FOR_PAUSE"
                    else {}
                )
                self.journal.update_runner_state(
                    attempt_id, lambda state: {**state, "start_acknowledged": True, **paused}
                )
                self._adopted[attempt_id] = authority
                self.state.finish(start_callback)
                self.state.finish(callback)
        except ExecutorError as exc:
            if exc.code == ExecutorErrorCode.INVALID_STATE and str(exc).startswith(
                "input verification failed:"
            ):
                self._execution_failed(
                    attempt_id,
                    request=request,
                    failure_class="INVALID_INPUT",
                    reason_code="INPUT_CHECKSUM_MISMATCH",
                )
            # Unknown Docker create/start outcomes require reconciliation; they
            # cannot be converted into a no-container cleanup proof here.
            raise
        except WorkerApiError as exc:
            if exc.status == 409 and identity is not None and start_callback is not None:
                try:
                    released = self._execution_failed(
                        attempt_id,
                        request=request,
                        failure_class="TIMEOUT",
                        reason_code="STARTUP_TIMEOUT",
                    )
                    if released:
                        self._retire_fenced_startup(attempt_id)
                finally:
                    # A definite rejection cannot authorize this runner. Even
                    # when failure reporting is unavailable, stop only its
                    # locally bound identity and retain pending resolution.
                    with suppress(ExecutorError):
                        current = self.journal.load(attempt_id)
                        if current.container == identity and current.state != "TOMBSTONED":
                            self.executor.cleanup(identity)
            raise
        except (ValueError, TimeoutError) as exc:
            self._execution_failed(
                attempt_id,
                request=request,
                failure_class="TIMEOUT" if isinstance(exc, TimeoutError) else "INTERNAL",
                reason_code="STARTUP_TIMEOUT"
                if isinstance(exc, TimeoutError)
                else "INVALID_RESULT",
            )
            raise

    def _checkpoint_launch(self, authority, context, directory, architecture):
        """Return (launch, restore file view); both are checked before any Docker work."""
        image_capable = context["template_snapshot"].get(
            "checkpointable"
        ) is True and self.executor.checkpoint_supported(context["image_digest"])
        launch = checkpoint_launch(context, image_capable=image_capable)
        if context["restore_checkpoint"] is None:
            return launch, None
        restore_file, cursor = verify_restore_manifest(context, architecture, launch)
        target = directory / "restore-state.json"
        try:
            if target.exists() and not _matches_descriptor(target, restore_file):
                # A worker killed mid-download leaves a partial private file.
                target.unlink()
            if not target.exists():
                self.client.download_execution(authority, restore_file, target)
            raw = target.read_bytes()
        except ValueError as exc:
            raise RestoreUnavailable("restore state bytes are unavailable") from exc
        verify_restore_state(raw, context=context, cursor=cursor)
        return replace(launch, restore=cursor), restore_file

    def _adapter_checkpoint_launch(self, authority, context, adapter, directory, architecture):
        """Return (checkpoint, restore, ordered restore files, recognized chunks).

        All are checked before any Docker work.
        """
        image_capable = context["template_snapshot"].get(
            "checkpointable"
        ) is True and self.executor.checkpoint_supported(
            context["image_digest"], adapter.checkpoint_format
        )
        checkpoint = adapter_checkpoint(
            context, adapter, image_capable=image_capable, architecture=architecture
        )
        if context["restore_checkpoint"] is None:
            recognized = adapter_recognized(context, adapter, None)
            self._download_recognized(authority, context, directory, recognized, None)
            return checkpoint, None, (), recognized
        restore, ordered = verify_adapter_restore_manifest(context, adapter, checkpoint[0])
        recognized = adapter_recognized(context, adapter, restore)
        target_dir = directory / "restore"
        target_dir.mkdir(exist_ok=True, mode=0o700)
        if target_dir.is_symlink():
            raise RestoreUnavailable("restore staging directory is a symlink")
        contents = {}
        try:
            for name, view in ordered:
                target = target_dir / name
                if target.exists() and not _matches_descriptor(target, view):
                    # A worker killed mid-download leaves a partial private file.
                    target.unlink()
                if not target.exists():
                    self.client.download_execution(authority, view, target)
                contents[name] = target.read_bytes()
        except ValueError as exc:
            raise RestoreUnavailable("restore state bytes are unavailable") from exc
        threads = adapter_launch.threads_for(context["allocation"]["resources"]["cpu_millis"])
        verify_adapter_restore_files(contents, context=context, restore=restore, threads=threads)
        self._download_recognized(authority, context, directory, recognized, restore)
        return checkpoint, restore, ordered, recognized

    def _download_recognized(self, authority, context, directory, recognized, restore):
        """Download the recognized chunk files the workload carries forward (B16-R21).

        Bytes that differ from the claimed artifact fail the Attempt before any container:
        the workload may neither recompute the chunk nor carry a different one.
        """
        if not recognized:
            return
        target_dir = directory / RECOGNIZED_DIRECTORY
        target_dir.mkdir(exist_ok=True, mode=0o700)
        if target_dir.is_symlink():
            raise ChunkOutputUnavailable("recognized staging directory is a symlink")
        try:
            for view, target in recognized_downloads(context, directory, recognized, restore):
                if target.exists() and not _matches_descriptor(target, view):
                    # A worker killed mid-download leaves a partial private file.
                    target.unlink()
                if not target.exists():
                    self.client.download_execution(authority, view, target)
                if not _matches_descriptor(target, view):
                    raise ValueError("recognized chunk bytes mismatch")
        except ValueError as exc:
            raise ChunkOutputUnavailable("recognized chunk bytes are unavailable") from exc

    def _fail_exited_startup(self, attempt_id, request, exited):
        """Report a runner whose container exited before it accepted its deadline.

        A terminal frame the runner handed the deadline connection names the cause;
        otherwise the exit status does (B15-R14). An unresolved report is replayed.
        """
        state = self.journal.load(attempt_id).runner_state or {}
        failure = container_exit_failure(exited, state)
        for key in ("pending_execution_message", "pending_terminal_message"):
            envelope = state.get(key)
            if envelope is not None and envelope["type"] in {"FAILED", "STOPPED"}:
                failure = terminal_frame_failure(envelope, state)
                break
        failure_class, reason_code = failure
        if not self._execution_failed(
            attempt_id,
            request=request,
            failure_class=failure_class,
            reason_code=reason_code,
            exited=exited,
        ):
            raise RunnerControlError("exited runner failure is not yet resolved")
        self._retire_fenced_startup(attempt_id)

    def _retire_fenced_startup(self, attempt_id):
        # Called only after the server has revoked the attempt and accepted
        # exact stopped-container cleanup. A denied start has no 200 response
        # to acknowledge, so it needs an explicit durable discard.
        for callback_id, pending in self.state.operations.items():
            if pending["payload"].get("attempt_id") != attempt_id:
                continue
            if pending["operation"] == "start":
                if pending["acknowledgment"] is None:
                    self.state.discard_rejected_start(
                        callback_id,
                        attempt_id=attempt_id,
                        authority_revoked=True,
                        cleanup_verified=True,
                    )
                else:
                    self.state.finish(callback_id)
            elif pending["operation"] == "claim" and pending["acknowledgment"] is not None:
                self.state.finish(callback_id)

    def _control_exchange(self, attempt_id, frame):
        record = self.journal.load(attempt_id)
        if record.container is None:
            raise RunnerControlError("runner identity is absent")
        channel = self.channel_factory(record.container.container_id)
        manager = channel if hasattr(channel, "__enter__") else nullcontext(channel)
        with manager as connection:
            connection.settimeout(5.0)
            connection.sendall(encode_frame(frame))
            ack = validate_ack(
                RunnerControl._receive_ack(
                    connection,
                    FrameDecoder(),
                    frame["control_sequence"],
                    5.0,
                    keep_terminal=lambda message: self._record_runner_message(attempt_id, message),
                )
            )
        if not ack["accepted"]:
            raise RunnerControlRejected(ack.get("code"))

    def _flush_result_controls(self, attempt_id):
        """Called under the attempt lock before renewal can allocate a sequence."""
        state = self.journal.load(attempt_id).runner_state or {}
        controls = state.get("result_controls", {})
        for key, entry in controls.items():
            if entry["acknowledged"] or entry.get("rejected"):
                continue
            try:
                self._control_exchange(attempt_id, entry["frame"])
            except RunnerControlRejected:

                def reject(local, key=key):
                    current = dict(local.get("result_controls", {}))
                    current[key] = {**current[key], "rejected": True}
                    return {**local, "result_controls": current}

                # A rejected sequence was never committed by the runner; it is
                # not resent, and the runner stops after any rejection.
                self.journal.update_runner_state(attempt_id, reject)
                raise

            def commit(local, key=key, entry=entry):
                current = dict(local.get("result_controls", {}))
                current[key] = {**current[key], "acknowledged": True}
                return {
                    **local,
                    "result_controls": current,
                    "last_control_sequence": entry["frame"]["control_sequence"],
                }

            self.journal.update_runner_state(attempt_id, commit)

    def _send_result_control(self, attempt_id, key, type_, payload):
        with self.journal.lock(attempt_id):
            # A deadline operation owns its sequence until the B10 replay path resolves it.
            if any(
                record["operation"] in {"renew", "adopt", "start"}
                and record["payload"].get("attempt_id") == attempt_id
                for record in self.state.operations.values()
            ):
                raise AuthorityControlPending("authority control is pending")
            self._flush_result_controls(attempt_id)
            record = self.journal.load(attempt_id)
            state = record.runner_state or {}
            controls = state.get("result_controls", {})
            if key in controls:
                frame = controls[key]["frame"]
                if frame["type"] != type_ or frame["payload"] != payload:
                    raise ValueError("result control replay payload changed")
                if controls[key].get("rejected"):
                    raise RunnerControlRejected("INVALID")
                return
            frame = {
                "schema_version": 1,
                "control_sequence": self._next_control_sequence(record),
                "type": type_,
                "payload": payload,
            }
            self.journal.update_runner_state(
                attempt_id,
                lambda local: {
                    **local,
                    "result_controls": {
                        **local.get("result_controls", {}),
                        key: {"frame": frame, "acknowledged": False},
                    },
                },
            )
            self._flush_result_controls(attempt_id)

    def _read_result_output(self, attempt_id, descriptor):
        record = self.journal.load(attempt_id)
        if record.container is None:
            raise ValueError("result source container missing")
        observed = self._inspect_identity(record.container.container_id)
        if (
            observed.container_id != record.container.container_id
            or observed.runtime_identity_digest != record.container.runtime_identity_digest
        ):
            raise ValueError("result container identity changed")
        return self.docker.read_output(record.container.container_id, descriptor)

    def _result_once(self):
        if self.journal is None:
            return
        flow = ResultFlow(
            self.journal, self.client, self._read_result_output, self._send_result_control
        )
        checkpoints = CheckpointFlow(
            self.journal,
            self.client,
            self._read_result_output,
            self._send_result_control,
            monotonic_ns=self.monotonic_ns,
            next_due=self._checkpoint_due,
            pause_retry=self._pause_retry,
        )
        first_error = None
        for attempt_id in tuple(self._adopted):
            try:
                self._result_attempt(attempt_id, flow, checkpoints)
            except (OSError, TimeoutError, WorkerApiError, RunnerControlError, RuntimeError) as exc:
                if self._observe_container_exit(attempt_id) is not None:
                    continue
                # One unreachable attempt must not stall another attempt's cycle.
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _observe_container_exit(self, attempt_id):
        """Persist a Docker-proven stop of the bound workload container.

        Returns None while the container runs or cannot be inspected, so a
        transient relay failure never ends a healthy attempt.
        """
        if self.executor is None or not self.journal.exists(attempt_id):
            return None
        with self.journal.lock(attempt_id):
            record = self.journal.load(attempt_id)
            state = record.runner_state or {}
            if state.get("container_exit") is not None:
                return state["container_exit"]
            if record.container is None:
                return None
            try:
                observation = self.executor.inspect(record.container)
            except (ExecutorError, DockerContainerNotFound):
                return None
            if observation.running:
                return None
            code = observation.exit_code
            exited = {
                "exit_code": code if isinstance(code, int) and -1 <= code <= 255 else None,
                "oom_killed": observation.oom_killed,
                "observed_at": observation.observed_at,
            }
            self.journal.update_runner_state(
                attempt_id, lambda local: {**local, "container_exit": exited}
            )
            return exited

    def _result_attempt(self, attempt_id, flow, checkpoints):
        record = self.journal.load(attempt_id)
        state = record.runner_state or {}
        if state.get("result_flow", {}).get("completed"):
            self._discard_completed_renewals(attempt_id)
            if record.container is not None and self._stop_orphan(record.container):
                self._adopted.pop(attempt_id, None)
                self._containers.pop(record.container.container_id, None)
            return
        if state.get("cleanup_verified") is not None:
            # A stale snapshot of the adopted set: the attempt was released (REM-R06).
            self._adopted.pop(attempt_id, None)
            self._pause_deadline.pop(attempt_id, None)
            self._pause_retry.pop(attempt_id, None)
            return
        envelope = state.get("pending_execution_message")
        exited = state.get("container_exit")
        kept = state.get("pending_terminal_message")
        if (
            exited is not None
            and kept is not None
            and (envelope is None or envelope["type"] not in {"FAILED", "STOPPED"})
        ):
            # The runner is gone; the terminal frame it handed a connection
            # names the cause, so frames before it no longer matter (B15-R16).
            envelope = kept
        pausing = state.get("control_desired") == "PAUSED"
        # The committed pause checkpoint moved the Attempt to STOPPING: the server
        # accepts no failure any more (SM:75), only the cleanup that pauses (SM:23).
        stopping = (
            pausing and (state.get("checkpoint_flow") or {}).get("pause_checkpoint") is not None
        )
        if pausing and (state.get("checkpoint_flow") or {}).get("pause_rejected"):
            # A deterministic manifest defect of a CHECKPOINT_FOR_PAUSE Attempt:
            # no retry can produce the pause checkpoint (B15-R28).
            if self._execution_failed(
                attempt_id, failure_class="INTERNAL", reason_code="CHECKPOINT_PROTOCOL_ERROR"
            ):
                self._pause_deadline.pop(attempt_id, None)
            return
        if pausing:
            now = self.monotonic_ns()
            deadline = self._pause_deadline.setdefault(attempt_id, now + PAUSE_DEADLINE_NS)
            if stopping and (
                now >= deadline
                or exited is not None
                or (envelope is not None and envelope["type"] in {"FAILED", "STOPPED"})
            ):
                # Whatever ended the workload, a forced stop proves it (B15-R21).
                self._paused(attempt_id, record)
                return
            if now >= deadline:
                # No committed checkpoint or no confirmed stop: the pause
                # cannot finish, so the runner is treated as unavailable.
                if self._execution_failed(
                    attempt_id, failure_class="INFRASTRUCTURE", reason_code="RUNNER_UNAVAILABLE"
                ):
                    self._pause_deadline.pop(attempt_id, None)
                return
        if (
            exited is not None
            and state.get("result_flow", {}).get("completion_callback_id") is None
            and (envelope is None or envelope["type"] not in {"FAILED", "STOPPED"})
        ):
            # No runner is left to answer; a sent completion is replayed instead.
            failure_class, reason_code = container_exit_failure(exited, state)
            self._execution_failed(
                attempt_id, failure_class=failure_class, reason_code=reason_code, exited=exited
            )
            return
        if envelope is None and pausing:
            self._pause_step(attempt_id, state, checkpoints)
            return
        if envelope is None:
            deferred = state.get("deferred_result_prepare")
            if deferred is not None and not checkpoints.cycle_open(attempt_id):
                if self._process_result(attempt_id, flow, deferred):
                    self.journal.update_runner_state(
                        attempt_id, lambda local: {**local, "deferred_result_prepare": None}
                    )
                return
            self._checkpoint_step(attempt_id, lambda: checkpoints.tick(attempt_id))
            return
        if envelope["type"] in {"FAILED", "STOPPED"}:
            stop_reason = (
                envelope["payload"].get("reason") if envelope["type"] == "STOPPED" else None
            )
            if stop_reason == "PAUSE" and state.get("pause_stop") is not None:
                self._paused(attempt_id, record)
                return
            failure_class, reason_code = terminal_frame_failure(envelope, state)
            self._execution_failed(attempt_id, failure_class=failure_class, reason_code=reason_code)
            return
        if envelope["type"] in _CHECKPOINT_FRAMES or (
            envelope["type"] in _CHUNK_FRAMES and envelope["payload"].get("purpose") == "CHECKPOINT"
        ):
            # A runner that rejected this cycle's control is stopping (B15-R14):
            # its later checkpoint frames drive nothing, and holding one pending
            # would keep its STOPPED frame, which names the cause, unread.
            if not state.get("checkpoint_rejected") and not self._checkpoint_step(
                attempt_id, lambda: checkpoints.process(attempt_id, envelope)
            ):
                return
        elif envelope["type"] == "RESULT_PREPARE" and (
            pausing or checkpoints.cycle_open(attempt_id)
        ):
            # The server refuses a result reservation while CHECKPOINTING and
            # the runner still owes this cycle's frames after RESULT_PREPARE;
            # a pause reserves no result unless the server aborts it.
            self.journal.update_runner_state(
                attempt_id, lambda local: {**local, "deferred_result_prepare": dict(envelope)}
            )
        elif not self._process_result(attempt_id, flow, envelope):
            return

        def commit(local, envelope=envelope):
            sequences = SequenceState.from_snapshot(
                local.get("message_sequences", {"highest": 0, "payload_hashes": {}})
            )
            effect = canonical_envelope_effect(envelope)
            if sequences.classify(envelope["message_sequence"], effect) == "ACCEPTED":
                sequences.commit(envelope["message_sequence"], effect)
            return {
                **local,
                "message_sequences": sequences.snapshot(),
                "pending_execution_message": None,
            }

        self.journal.update_runner_state(attempt_id, commit)

    def _process_result(self, attempt_id, flow, envelope):
        try:
            flow.process(attempt_id, envelope)
        except ChunkOutputConflict:
            # The server keeps a different recognized chunk (B16-R21): a replay
            # repeats the same rejection, so the Attempt fails at once.
            self._execution_failed(
                attempt_id, failure_class="INTERNAL", reason_code="CHUNK_OUTPUT_CONFLICT"
            )
            return False
        except ValueError:
            # Invalid result bytes/protocol must not leave a live allocation
            # waiting forever for a message that can never be acknowledged.
            self._execution_failed(
                attempt_id, failure_class="INTERNAL", reason_code="INVALID_RESULT"
            )
            return False
        return True

    def _pause_step(self, attempt_id, state, checkpoints):
        """Checkpoint for pause, then ask the runner to stop the workload for PAUSE."""
        flow = state.get("checkpoint_flow") or {}
        if flow.get("pause_checkpoint") is None:
            self._checkpoint_step(attempt_id, lambda: checkpoints.tick(attempt_id, pause=True))
            return
        stop = state.get("pause_stop")
        if stop is None:
            # Frozen once: a replay must resend the byte-identical control.
            stop = {
                "reason": "PAUSE",
                "grace_deadline_monotonic_ns": self.monotonic_ns() + PAUSE_STOP_GRACE_NS,
            }
            self.journal.update_runner_state(
                attempt_id, lambda local: {**local, "pause_stop": stop}
            )
        self._checkpoint_step(
            attempt_id,
            lambda: self._send_result_control(attempt_id, "stop:PAUSE", "REQUEST_STOP", stop),
        )

    def _paused(self, attempt_id, record):
        """The workload stopped for PAUSE: prove the container stopped, then release."""
        if record.container is not None and self._stop_orphan(record.container):
            self._adopted.pop(attempt_id, None)
            self._containers.pop(record.container.container_id, None)
            self._pause_deadline.pop(attempt_id, None)
            self._pause_retry.pop(attempt_id, None)
            metrics_worker.execution("PAUSED")

    def _checkpoint_step(self, attempt_id, step):
        """Run one checkpoint step; a definite protocol defect fails the attempt closed."""
        try:
            step()
        except AuthorityControlPending:
            return False
        except RunnerControlRejected as exc:
            if exc.code != "INVALID":
                raise
            # A stopping runner rejects every control (B14-R10); its STOPPED
            # frame, still unread, names the cause. FAILURE means this control.
            self.journal.update_runner_state(
                attempt_id, lambda local: {**local, "checkpoint_rejected": True}
            )
            return False
        except CheckpointProtocolError:
            # The failure callback also ends the server reservation (ABANDONED).
            self._execution_failed(
                attempt_id, failure_class="INTERNAL", reason_code="CHECKPOINT_PROTOCOL_ERROR"
            )
            return False
        except ChunkOutputConflict:
            # B16-R21: nothing committed and a replay repeats the rejection.
            self._execution_failed(
                attempt_id, failure_class="INTERNAL", reason_code="CHUNK_OUTPUT_CONFLICT"
            )
            return False
        return True

    def _execution_failed(
        self,
        attempt_id,
        *,
        request=None,
        failure_class="INFRASTRUCTURE",
        reason_code="STARTUP_FAILED",
        exited=None,
    ):
        """Failure linearization precedes identity-only proof-based cleanup.

        The reconciliation replay may send this attempt's pending failure or
        cleanup concurrently; the whole read/decide/send runs under the attempt
        journal lock (B15-R39).
        """
        with self.journal.lock(attempt_id):
            return self._execution_failed_locked(
                attempt_id,
                request=request,
                failure_class=failure_class,
                reason_code=reason_code,
                exited=exited,
            )

    def _execution_failed_locked(self, attempt_id, *, request, failure_class, reason_code, exited):
        record = self.journal.load(attempt_id) if self.journal.exists(attempt_id) else None
        if record is not None and (record.runner_state or {}).get("result_flow", {}).get(
            "completed"
        ):
            stopped = self._stop_orphan(record.container) if record.container else False
            if stopped:
                self._adopted.pop(attempt_id, None)
            return stopped
        if record is not None and (record.runner_state or {}).get("cleanup_verified") is not None:
            # The verified cleanup released the lease: the server accepts no failure
            # for it, and a pending one would hold the worker out of READY (REM-R06).
            self._adopted.pop(attempt_id, None)
            return True
        identity = record.container if record else None
        failure = (record.runner_state or {}).get("failure_resolution") if record else None
        if failure is None:
            if identity is None:
                if request is None:
                    return False
                proof = self.executor.tombstone_unclaimed(request, reason=reason_code)
                observation = {
                    "observation_type": "NO_CONTAINER",
                    "proof": {
                        key: getattr(proof, key)
                        for key in (
                            "proof_type",
                            "startup_nonce",
                            "executor_operation_sequence",
                            "tombstone_sequence",
                            "observed_at",
                            "inspection_checksum",
                        )
                    },
                }
                authority = request.context.authority
            else:
                from datetime import UTC, datetime

                authority = record.authority
                # Without a Docker-proven exit the runner is still presumed alive; only
                # its forwarded cgroup OOM report marks the observation as an OOM kill.
                exited = exited or {
                    "exit_code": None,
                    "oom_killed": failure_class == "OOM",
                    "observed_at": datetime.now(UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                }
                observation = {
                    "observation_type": "CONTAINER",
                    "container": {
                        "container_id": identity.container_id,
                        "runtime_identity_digest": identity.runtime_identity_digest,
                    },
                    "observed_at": exited["observed_at"],
                    "exit_code": exited["exit_code"],
                    "oom_killed": exited["oom_killed"],
                    # Only the runner's own runtime-limit stop reports this
                    # failure; the server rejects a contradicting observation.
                    "runtime_limit_reached": (failure_class, reason_code)
                    == ("TIMEOUT", "RUNTIME_LIMIT_REACHED"),
                }
            body = {
                "authority": asdict(authority),
                "failure_class": failure_class,
                "reason_code": reason_code,
                "observation": observation,
            }
            callback, _ = self._pending_callback("failure", attempt_id, body)
            failure = {"callback_id": callback, "body": body, "acknowledged": False}
            self.journal.update_runner_state(
                attempt_id, lambda local: {**local, "failure_resolution": failure}
            )
        callback, body = failure["callback_id"], failure["body"]
        authority = Authority(**body["authority"])
        observation = body["observation"]
        if not failure["acknowledged"]:
            if callback not in self.state.operations:
                self.state.begin(
                    callback,
                    operation="failure",
                    payload={"attempt_id": attempt_id, "body": body},
                )
            if not self._send_resolution(callback):
                return False
            self.journal.update_runner_state(
                attempt_id,
                lambda local: {
                    **local,
                    "failure_resolution": {**local["failure_resolution"], "acknowledged": True},
                },
            )
        self._adopted.pop(attempt_id, None)
        if identity is not None:
            return self._stop_orphan(identity)
        cleanup = {
            "worker_id": self.worker_id,
            "worker_incarnation_id": self.incarnation_id,
            "attempt_id": attempt_id,
            "allocation_id": authority.allocation_id,
            "job_fence": authority.job_fence,
            "proof": observation["proof"],
        }
        callback, _ = self._pending_callback("cleanup", attempt_id, cleanup)
        return self._send_resolution(callback)
