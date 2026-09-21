import pytest

from nexa.worker.journal import ExecutionJournal, JournalCorruption, JournalWriteError
from nexa.worker.models import Authority, ResourceVector

ATTEMPT = "018f0d60-7b6a-7a23-9d82-1aa39c4f30b7"
ALLOC = "018f0d60-7b6a-7a24-9d82-1aa39c4f30b7"
NONCE = "018f0d60-7b6a-7a26-9d82-1aa39c4f30b7"
AUTHORITY = Authority(
    worker_id="018f0d60-7b6a-7a21-9d82-1aa39c4f30b7",
    worker_incarnation_id="018f0d60-7b6a-7a22-9d82-1aa39c4f30b7",
    attempt_id=ATTEMPT,
    allocation_id=ALLOC,
    lease_id="018f0d60-7b6a-7a25-9d82-1aa39c4f30b7",
    job_fence=1,
)
BINDING = {
    "architecture": "linux/amd64",
    "adapter_id": "cpu.iterative",
    "adapter_version": "1.0.0",
    "framework": "NEXA_CPU",
    "framework_version": "1.0.0",
    "scratch_bytes": 64 * 1024**2,
    "log_bytes": 1024**2,
    "runtime_limit_seconds": 30,
    "input_mounts": [],
}


def test_journal_persists_monotonic_state_and_reload(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path)
    record = journal.prepare(
        attempt_id=ATTEMPT,
        allocation_id=ALLOC,
        startup_nonce=NONCE,
        authority=AUTHORITY,
        resources=ResourceVector(1000, 128 * 1024**2, 0),
        image_digest="sha256:" + "a" * 64,
        execution_binding=BINDING,
    )
    assert record.state == "PREPARED"
    in_flight = journal.begin_create(ATTEMPT, expected_sequence=record.operation_sequence)
    assert in_flight.state == "CREATE_IN_FLIGHT"
    reloaded = ExecutionJournal(tmp_path).load(ATTEMPT)
    assert reloaded.operation_sequence == in_flight.operation_sequence
    assert reloaded.startup_nonce == NONCE


def test_journal_tombstone_rejects_old_sequence(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path)
    record = journal.prepare(
        attempt_id=ATTEMPT,
        allocation_id=ALLOC,
        startup_nonce=NONCE,
        authority=AUTHORITY,
        resources=ResourceVector(1000, 128 * 1024**2, 0),
        image_digest="sha256:" + "a" * 64,
        execution_binding=BINDING,
    )
    tombstone = journal.tombstone(
        ATTEMPT, expected_sequence=record.operation_sequence, reason="PRE_CREATE_STOP"
    )
    assert tombstone.state == "TOMBSTONED"
    with pytest.raises(JournalWriteError, match="tombstoned"):
        journal.begin_create(ATTEMPT, expected_sequence=record.operation_sequence)


def test_journal_corruption_fails_closed(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path)
    path = journal.path_for(ATTEMPT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(JournalCorruption):
        journal.load(ATTEMPT)


def test_journal_does_not_tombstone_create_in_flight(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path)
    record = journal.prepare(
        attempt_id=ATTEMPT,
        allocation_id=ALLOC,
        startup_nonce=NONCE,
        authority=AUTHORITY,
        resources=ResourceVector(1000, 128 * 1024**2, 0),
        image_digest="sha256:" + "a" * 64,
        execution_binding=BINDING,
    )
    in_flight = journal.begin_create(ATTEMPT, expected_sequence=record.operation_sequence)
    with pytest.raises(JournalWriteError, match="in flight"):
        journal.tombstone(
            ATTEMPT,
            expected_sequence=in_flight.operation_sequence,
            reason="PRE_CREATE_STOP",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("architecture", "linux/arm64"),
        ("adapter_version", "1.0.1"),
        ("framework_version", "1.0.1"),
        ("scratch_bytes", 32 * 1024**2),
        ("log_bytes", 512 * 1024),
        ("runtime_limit_seconds", 29),
        (
            "input_mounts",
            [
                {
                    "artifact_id": "018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
                    "source_path": "/var/lib/nexa/staging/changed",
                    "target_path": "/input/input.json",
                    "content_checksum": "sha256:" + "c" * 64,
                    "size_bytes": 1,
                    "read_only": True,
                }
            ],
        ),
    ],
)
def test_journal_rejects_every_changed_immutable_binding(tmp_path, field, value) -> None:
    journal = ExecutionJournal(tmp_path)
    journal.prepare(
        attempt_id=ATTEMPT,
        allocation_id=ALLOC,
        startup_nonce=NONCE,
        authority=AUTHORITY,
        resources=ResourceVector(1000, 128 * 1024**2, 0),
        image_digest="sha256:" + "a" * 64,
        execution_binding=BINDING,
    )
    changed = {**BINDING, field: value}
    with pytest.raises(JournalWriteError, match="immutable"):
        journal.prepare(
            attempt_id=ATTEMPT,
            allocation_id=ALLOC,
            startup_nonce=NONCE,
            authority=AUTHORITY,
            resources=ResourceVector(1000, 128 * 1024**2, 0),
            image_digest="sha256:" + "a" * 64,
            execution_binding=changed,
        )
