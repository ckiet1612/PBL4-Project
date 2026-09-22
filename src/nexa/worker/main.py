"""Local worker entrypoint. Unresolved server or Docker state prevents READY."""

import argparse
import asyncio
import logging
import os
import platform
import signal
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from nexa.infrastructure.persistence.ids import new_uuid7

from .agent import WorkerAgent
from .client import WorkerApiClient, WorkerTransportError
from .credentials import (
    CredentialRecoveryRequired,
    CredentialStore,
    _atomic_private_json,
    _fsync_directory,
)
from .docker_client import DockerCli
from .executor import DockerExecutor
from .journal import ExecutionJournal
from .probes import live_provider
from .singleton import LocalWorkerLock, WorkerAlreadyRunning
from .state import PendingOperationStore

LOG = logging.getLogger("nexa.worker")


def _cleanup_complete(response: dict) -> bool:
    return response.get("verified") is True or response.get("allocation_state") == "RELEASED"


def _replay_pending(
    config: "WorkerConfig", client: WorkerApiClient, state: PendingOperationStore
) -> None:
    for callback_id, record in list(state.operations.items()):
        if record["operation"] in {"adopt", "renew", "runner_deadline"}:
            continue
        acknowledgment = record["acknowledgment"]
        if acknowledgment is not None and (
            record["operation"] != "cleanup" or _cleanup_complete(acknowledgment)
        ):
            state.finish(callback_id)
            continue
        state.first_send(callback_id)
        operation = record["operation"]
        payload = record["payload"]
        if operation == "create_incarnation":
            response = client.create_incarnation(
                config.worker_id, nonce=payload["nonce"], key=callback_id
            )
        elif operation == "heartbeat":
            response = client.heartbeat(config.worker_id, callback_id, payload)
        elif operation in {"adopt", "renew", "failure", "cleanup"}:
            attempt_id = payload["attempt_id"]
            body = payload["body"]
            method = getattr(
                client,
                {"adopt": "adopt", "renew": "renew", "failure": "fail", "cleanup": "cleanup"}[
                    operation
                ],
            )
            response = method(attempt_id, callback_id, body)
        else:
            raise RuntimeError(f"pending {operation} requires an unavailable milestone endpoint")
        state.acknowledge(callback_id, response)
        if operation != "cleanup" or _cleanup_complete(response):
            state.finish(callback_id)


def _bootstrap_secret(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > 513:
        raise ValueError("bootstrap secret is invalid")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        secret = raw.decode("ascii", "strict")
    except UnicodeDecodeError:
        raise ValueError("bootstrap secret is invalid") from None
    if not 32 <= len(secret) <= 512 or any(ord(value) < 33 or ord(value) > 126 for value in secret):
        raise ValueError("bootstrap secret is invalid")
    return secret


@dataclass(frozen=True)
class WorkerConfig:
    api_url: str
    installation_id: str
    worker_id: str
    fingerprint: str
    state_root: Path
    bootstrap_secret_file: Path

    @classmethod
    def from_environment(cls) -> "WorkerConfig":
        keys = (
            "NEXA_WORKER_API_URL",
            "NEXA_INSTALLATION_ID",
            "NEXA_LOCAL_WORKER_ID",
            "NEXA_LOCAL_WORKER_FINGERPRINT",
            "NEXA_WORKER_STATE_ROOT",
            "NEXA_BOOTSTRAP_SECRET_FILE",
        )
        values = [os.environ[key] for key in keys]
        config = cls(values[0], values[1], values[2], values[3], Path(values[4]), Path(values[5]))
        parsed = urlsplit(config.api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
            raise ValueError("worker API URL is invalid")
        if not config.state_root.is_absolute() or not config.bootstrap_secret_file.is_absolute():
            raise ValueError("worker state and secret paths must be absolute")
        for identity in (config.installation_id, config.worker_id):
            if UUID(identity).version != 7:
                raise ValueError("worker identifiers must be UUIDv7")
        if not config.fingerprint.startswith("sha256:") or len(config.fingerprint) != 71:
            raise ValueError("worker fingerprint is invalid")
        return config


def _credential(config: WorkerConfig, *, bootstrap: bool) -> str:
    store = CredentialStore(config.state_root / "credential.json")
    existing = store.load(worker_id=config.worker_id, installation_id=config.installation_id)
    if existing is not None:
        return existing
    if not bootstrap:
        raise CredentialRecoveryRequired("worker credential missing; explicit bootstrap required")
    intent = config.state_root / "bootstrap-intent.json"
    if intent.exists():
        raise CredentialRecoveryRequired(
            "bootstrap response uncertain; administrator recovery required"
        )
    secret = _bootstrap_secret(config.bootstrap_secret_file)
    key = str(new_uuid7())
    _atomic_private_json(intent, {"idempotency_key": key})
    with_client = WorkerApiClient(config.api_url)
    try:
        response = with_client.bootstrap(
            installation_id=config.installation_id,
            fingerprint=config.fingerprint,
            secret=secret,
            idempotency_key=key,
        )
        if response.get("worker_id") != config.worker_id:
            raise ValueError("bootstrap returned a different worker")
        store.save(
            worker_id=config.worker_id,
            installation_id=config.installation_id,
            credential=response["credential"],
        )
        intent.unlink()
        _fsync_directory(intent.parent)
        return response["credential"]
    finally:
        with_client.close()


def run(config: WorkerConfig, *, bootstrap: bool = False) -> int:
    if platform.system() != "Linux":
        raise RuntimeError("worker and trusted runner require the same Linux monotonic clock")
    with LocalWorkerLock(config.state_root / "worker.lock"):
        credential = _credential(config, bootstrap=bootstrap)
        state = PendingOperationStore(config.state_root / "agent-state.json")
        client = WorkerApiClient(config.api_url, credential)
        try:
            _replay_pending(config, client, state)
            nonce = str(new_uuid7())
            key = str(new_uuid7())
            state.begin(key, operation="create_incarnation", payload={"nonce": nonce})
            state.first_send(key)
            incarnation = client.create_incarnation(config.worker_id, nonce=nonce, key=key)
            if (
                incarnation.get("process_start_nonce") != nonce
                or incarnation.get("worker_id") != config.worker_id
            ):
                raise RuntimeError("incarnation acknowledgment does not match this process")
            incarnation_id = incarnation["worker_incarnation_id"]
            state.acknowledge(key, incarnation)
            state.finish(key)
            provider = live_provider()
            journal = ExecutionJournal(config.state_root / "journal")
            docker = DockerCli()
            agent = WorkerAgent(
                worker_id=config.worker_id,
                incarnation_id=incarnation_id,
                installation_id=config.installation_id,
                client=client,
                state=state,
                journal=journal,
                docker=docker,
                executor=DockerExecutor(
                    journal,
                    docker,
                    image_ref=os.environ["NEXA_CPU_IMAGE_REF"],
                    installation_id=config.installation_id,
                ),
                provider=provider,
            )

            def terminate(_signum: int, _frame: object) -> None:
                agent.stop()

            signal.signal(signal.SIGTERM, terminate)
            signal.signal(signal.SIGINT, terminate)
            asyncio.run(agent.run())
            return 0
        finally:
            client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Nexa local worker")
    parser.add_argument("--bootstrap", action="store_true", help="one-time explicit bootstrap")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        return run(WorkerConfig.from_environment(), bootstrap=args.bootstrap)
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        WorkerAlreadyRunning,
        WorkerTransportError,
    ) as exc:
        LOG.error("worker unavailable: %s", exc)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
