"""CPU dispatch and result orchestration layered over B09/B10 lifecycle primitives."""

from contextlib import nullcontext, suppress
from dataclasses import asdict

from nexa.infrastructure.persistence.ids import new_uuid7

from .client import WorkerApiError
from .dispatch import execution_request
from .errors import ExecutorError, ExecutorErrorCode
from .models import Authority
from .protocol import (
    FrameDecoder,
    SequenceState,
    canonical_envelope_effect,
    encode_frame,
    validate_ack,
)
from .result_flow import ResultFlow
from .runner_control import RunnerControl, RunnerControlError


class WorkerExecutionMixin:
    def _pending_callback(self, operation, attempt_id, body):
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
            callback, operation=operation, payload={"attempt_id": attempt_id, "body": body}
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
        if self.executor is None:
            raise RuntimeError("dispatch requires configured executor")
        callback, pending = self._pending_callback(
            "claim", attempt_id, {"authority": asdict(authority)}
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
        request = execution_request(context, source, self.provider.discover().architecture)
        artifact = context["input_artifacts"][0]
        start_callback = None
        identity = None
        try:
            if self.monotonic_ns() >= first_claim_send + 30_000_000_000:
                raise TimeoutError("claim startup budget elapsed")
            if not source.exists():
                self.client.download_execution(authority, artifact, source)
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
                    raise RunnerControlError("start authority deadline was not accepted")
                self.journal.update_runner_state(
                    attempt_id, lambda state: {**state, "start_acknowledged": True}
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
                    connection, FrameDecoder(), frame["control_sequence"], 5.0
                )
            )
        if not ack["accepted"]:
            raise RunnerControlError("runner result control was not accepted")

    def _flush_result_controls(self, attempt_id):
        """Called under the attempt lock before renewal can allocate a sequence."""
        state = self.journal.load(attempt_id).runner_state or {}
        controls = state.get("result_controls", {})
        for key, entry in controls.items():
            if entry["acknowledged"]:
                continue
            self._control_exchange(attempt_id, entry["frame"])

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
                raise RunnerControlError("authority control is pending")
            self._flush_result_controls(attempt_id)
            record = self.journal.load(attempt_id)
            state = record.runner_state or {}
            controls = state.get("result_controls", {})
            if key in controls:
                frame = controls[key]["frame"]
                if frame["type"] != type_ or frame["payload"] != payload:
                    raise ValueError("result control replay payload changed")
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
        for attempt_id in tuple(self._adopted):
            record = self.journal.load(attempt_id)
            state = record.runner_state or {}
            if state.get("result_flow", {}).get("completed"):
                self._discard_completed_renewals(attempt_id)
                if record.container is not None and self._stop_orphan(record.container):
                    self._adopted.pop(attempt_id, None)
                    self._containers.pop(record.container.container_id, None)
                continue
            envelope = state.get("pending_execution_message")
            if envelope is None:
                continue
            if envelope["type"] in {"FAILED", "STOPPED"}:
                self._execution_failed(
                    attempt_id, failure_class="INTERNAL", reason_code="WORKLOAD_EXIT_NONZERO"
                )
                continue
            try:
                flow.process(attempt_id, envelope)
            except ValueError:
                # Invalid result bytes/protocol must not leave a live allocation
                # waiting forever for a message that can never be acknowledged.
                self._execution_failed(
                    attempt_id, failure_class="INTERNAL", reason_code="INVALID_RESULT"
                )
                continue

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

    def _execution_failed(
        self,
        attempt_id,
        *,
        request=None,
        failure_class="INFRASTRUCTURE",
        reason_code="STARTUP_FAILED",
    ):
        """Failure linearization precedes identity-only proof-based cleanup."""
        record = self.journal.load(attempt_id) if self.journal.exists(attempt_id) else None
        if record is not None and (record.runner_state or {}).get("result_flow", {}).get(
            "completed"
        ):
            stopped = self._stop_orphan(record.container) if record.container else False
            if stopped:
                self._adopted.pop(attempt_id, None)
            return stopped
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
                observation = {
                    "observation_type": "CONTAINER",
                    "container": {
                        "container_id": identity.container_id,
                        "runtime_identity_digest": identity.runtime_identity_digest,
                    },
                    "observed_at": datetime.now(UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    "exit_code": None,
                    "oom_killed": False,
                    "runtime_limit_reached": False,
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
