import threading

from nexa.worker.journal import ExecutionJournal
from nexa.worker.models import Authority, ResourceVector

OLD = Authority(
    worker_id="018f0d60-7b6a-7a21-9d82-1aa39c4f30b7",
    worker_incarnation_id="018f0d60-7b6a-7a22-9d82-1aa39c4f30b7",
    attempt_id="018f0d60-7b6a-7a23-9d82-1aa39c4f30b7",
    allocation_id="018f0d60-7b6a-7a24-9d82-1aa39c4f30b7",
    lease_id="018f0d60-7b6a-7a25-9d82-1aa39c4f30b7",
    job_fence=1,
)
NEW = Authority(
    worker_id=OLD.worker_id,
    worker_incarnation_id="018f0d60-7b6a-7a26-9d82-1aa39c4f30b7",
    attempt_id=OLD.attempt_id,
    allocation_id=OLD.allocation_id,
    lease_id=OLD.lease_id,
    job_fence=OLD.job_fence,
)


def test_rebind_authority_preserves_execution_and_runner_state(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path)
    record = journal.prepare(
        attempt_id=OLD.attempt_id,
        allocation_id=OLD.allocation_id,
        startup_nonce="018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
        authority=OLD,
        resources=ResourceVector(1000, 128 * 1024**2, 0),
        image_digest="sha256:" + "a" * 64,
        execution_binding={"control_sequence": 4, "payload_hash": "sha256:" + "b" * 64},
    )
    journal.append_runner_state(record.attempt_id, {"pending_control": 4})

    rebound = journal.rebind_authority(
        record.attempt_id,
        prior_authority=OLD,
        current_authority=NEW,
        transferred_reservations={"checkpoint": "same", "result": "same"},
    )

    assert rebound.authority == NEW
    assert rebound.execution_binding == record.execution_binding
    assert rebound.operation_sequence == record.operation_sequence
    assert rebound.runner_state == {"pending_control": 4}
    assert journal.load(record.attempt_id).authority == NEW


def test_rebind_authority_holds_attempt_lock_through_durable_write(tmp_path, monkeypatch) -> None:
    journal = ExecutionJournal(tmp_path)
    record = journal.prepare(
        attempt_id=OLD.attempt_id,
        allocation_id=OLD.allocation_id,
        startup_nonce="018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
        authority=OLD,
        resources=ResourceVector(1000, 128 * 1024**2, 0),
        image_digest="sha256:" + "a" * 64,
        execution_binding={},
    )
    write = journal._write

    def write_while_locked(value):
        assert journal._thread_depth.get(value.attempt_id) == 1
        return write(value)

    monkeypatch.setattr(journal, "_write", write_while_locked)

    journal.rebind_authority(
        record.attempt_id,
        prior_authority=OLD,
        current_authority=NEW,
    )


def test_runner_state_updates_are_atomic_under_attempt_lock(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path)
    record = journal.prepare(
        attempt_id=OLD.attempt_id,
        allocation_id=OLD.allocation_id,
        startup_nonce="018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
        authority=OLD,
        resources=ResourceVector(1000, 128 * 1024**2, 0),
        image_digest="sha256:" + "a" * 64,
        execution_binding={},
    )
    barrier = threading.Barrier(2)

    def update(key: str) -> None:
        barrier.wait()
        journal.update_runner_state(record.attempt_id, lambda state: {**state, key: True})

    threads = [threading.Thread(target=update, args=(key,)) for key in ("deadline", "progress")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert journal.load(record.attempt_id).runner_state == {"deadline": True, "progress": True}
