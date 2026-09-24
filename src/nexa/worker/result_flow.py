"""Durable CPU result handshake; slow byte I/O never holds the authority lock."""

import hashlib

import rfc8785


def checksum(value):
    return "sha256:" + hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def descriptor_key(attempt_id, callback_id, sequence, descriptor):
    return hashlib.sha256(
        rfc8785.dumps(
            {
                "version": 1,
                "attempt_id": attempt_id,
                "purpose": "RESULT",
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
                        "active_reservations": {"checkpoint": None, "result": reservation},
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
        binding, raw = self._upload(attempt_id, sequence, payload["manifest"])
        manifest = json.loads(raw)
        if (
            manifest["result_id"] != payload["result_id"]
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
        callback = state.get("completion_callback_id")
        if callback is None:
            callback = str(new_uuid7())
            self._save(attempt_id, completion_callback_id=callback)
        ack = self.client.complete(
            attempt_id,
            callback,
            {
                "authority": asdict(authority),
                "result_manifest_artifact_id": binding["artifact_id"],
                "manifest": manifest,
            },
        )
        if ack.get("accepted") is not True:
            raise ValueError("completion was not accepted")
        self._save(attempt_id, completed=True, completion_ack=ack)

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
