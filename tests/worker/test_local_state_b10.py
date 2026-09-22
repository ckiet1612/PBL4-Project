import json
import multiprocessing
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from nexa.worker.credentials import CredentialRecoveryRequired, CredentialStore
from nexa.worker.singleton import LocalWorkerLock, WorkerAlreadyRunning
from nexa.worker.state import PendingOperationStore


def _compete(path: str, result) -> None:
    try:
        with LocalWorkerLock(Path(path)):
            result.put("acquired")
    except WorkerAlreadyRunning:
        result.put("rejected")


def test_second_process_cannot_take_stable_lock_and_fd_is_not_inherited(tmp_path) -> None:
    path = tmp_path / "worker.lock"
    with LocalWorkerLock(path) as lock:
        inode = path.stat().st_ino
        assert lock.fd is not None and not os.get_inheritable(lock.fd)
        result = multiprocessing.Queue()
        contender = multiprocessing.Process(target=_compete, args=(str(path), result))
        contender.start()
        contender.join(timeout=5)
        assert contender.exitcode == 0
        assert result.get(timeout=1) == "rejected"
        subprocess.run([sys.executable, "-c", "pass"], check=True, close_fds=False)
    assert path.stat().st_ino == inode
    with LocalWorkerLock(path):
        pass


def test_private_credential_and_pending_callback_survive_restart(tmp_path) -> None:
    credentials = CredentialStore(tmp_path / "credential.json")
    credentials.save(worker_id="worker", installation_id="installation", credential="secret")
    assert credentials.path.stat().st_mode & 0o777 == 0o600
    assert credentials.load(worker_id="worker", installation_id="installation") == "secret"
    with pytest.raises(CredentialRecoveryRequired):
        credentials.save(worker_id="worker", installation_id="installation", credential="new")
    with pytest.raises(CredentialRecoveryRequired):
        credentials.load(worker_id="other", installation_id="installation")

    path = tmp_path / "pending.json"
    store = PendingOperationStore(path, boot_id="first")
    store.begin("callback", operation="renew", payload={"authority": "exact"})
    sent_at = store.first_send("callback")
    recovered = PendingOperationStore(path, boot_id="first")
    assert recovered.first_send("callback") == sent_at
    with pytest.raises(ValueError, match="unacknowledged"):
        recovered.finish("callback")
    recovered.acknowledge("callback", {"accepted": True})
    recovered.finish("callback")
    assert PendingOperationStore(path, boot_id="first").operations == {}
    with pytest.raises(ValueError, match="callback identity"):
        store.begin("callback", operation="renew", payload={"authority": "different"})
    store.begin("pending", operation="renew", payload={"authority": "exact"})
    with pytest.raises(ValueError, match="clock domain"):
        PendingOperationStore(path, boot_id="rebooted")


def test_pending_state_rejects_invalid_records_and_oversized_writes(tmp_path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text(
        json.dumps(
            {
                "version": 1,
                "boot_id": "boot",
                "operations": {
                    "callback": {
                        "operation": "renew",
                        "payload": [],
                        "first_send_monotonic_ns": False,
                        "acknowledgment": None,
                        "control_sequence": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    invalid.chmod(0o600)
    with pytest.raises(ValueError, match="pending operation"):
        PendingOperationStore(invalid, boot_id="boot")

    oversized = tmp_path / "oversized.json"
    store = PendingOperationStore(oversized, boot_id="boot")
    with pytest.raises(ValueError, match="exceeded bound"):
        store.begin(
            "callback",
            operation="renew",
            payload={"body": "x" * 1_048_576},
        )
    assert store.operations == {}
    assert not oversized.exists()


def test_concurrent_pending_callbacks_are_serialized_and_survive_restart(tmp_path) -> None:
    path = tmp_path / "concurrent.json"
    store = PendingOperationStore(path, boot_id="boot")
    barrier = threading.Barrier(16)

    def begin(index: int) -> None:
        barrier.wait()
        store.begin(f"callback-{index}", operation="renew", payload={"index": index})

    threads = [threading.Thread(target=begin, args=(index,)) for index in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert set(store.operations) == {f"callback-{index}" for index in range(16)}
    recovered = PendingOperationStore(path, boot_id="boot")
    assert set(recovered.operations) == set(store.operations)

    barrier = threading.Barrier(32)

    def finish(index: int) -> None:
        barrier.wait()
        callback_id = f"callback-{index}"
        store.first_send(callback_id)
        store.acknowledge(callback_id, {"accepted": True})
        store.finish(callback_id)

    def add(index: int) -> None:
        barrier.wait()
        store.begin(f"callback-{index}", operation="renew", payload={"index": index})

    finishers = [threading.Thread(target=finish, args=(index,)) for index in range(16)]
    adders = [threading.Thread(target=add, args=(index,)) for index in range(16, 32)]
    for thread in [*finishers, *adders]:
        thread.start()
    for thread in [*finishers, *adders]:
        thread.join()

    expected = {f"callback-{index}" for index in range(16, 32)}
    assert set(store.operations) == expected
    assert set(PendingOperationStore(path, boot_id="boot").operations) == expected
