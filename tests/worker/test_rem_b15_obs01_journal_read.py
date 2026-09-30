"""Remediation B15-OBS-01: journal reads never overlap a journal write of the same attempt.

The warnings were seen only with the worker state on a Docker Desktop bind mount (virtiofs),
where a reader may observe the record path missing while ``os.replace`` is still in flight.
The test opens exactly that window with a non-atomic replace and checks that a concurrent
``load``/``exists`` waits for the writer instead of reporting corruption. A record that is
really corrupt is reported with safe diagnostics only (cause, length, digest; no content).
"""

import asyncio
import hashlib
import logging
import os
import threading

import pytest

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.worker import journal as journal_module
from nexa.worker.agent import WorkerAgent
from nexa.worker.executor import DockerExecutor
from nexa.worker.journal import ExecutionJournal, JournalCorruption
from tests.worker.test_docker_config import start
from tests.worker.test_rem_b11_h01_fenced_claim import INSTALLATION_ID, LabelDocker
from tests.worker.test_rem_b15_r39_resolution_lock import GUARD_SECONDS, ObservedJournal

SECRET_MARKER = "input-secret-payload"


def _prepared(tmp_path, journal_class=ExecutionJournal):
    request = start()
    journal = journal_class(tmp_path / "journal")
    DockerExecutor(
        journal, LabelDocker(), image_ref="registry.invalid/cpu", installation_id=INSTALLATION_ID
    ).prepare(request)
    return request.context.authority.attempt_id, journal


@pytest.mark.parametrize("reader", ["load", "exists"])
def test_a_reader_waits_for_an_in_flight_replace(tmp_path, monkeypatch, reader) -> None:
    attempt_id, journal = _prepared(tmp_path, ObservedJournal)
    real_replace = os.replace
    window = threading.Event()
    reader_progress = threading.Condition()
    outcome: dict[str, object] = {}

    def read() -> None:
        result = _read(journal, reader, attempt_id)
        with reader_progress:
            outcome["read"] = result
            reader_progress.notify_all()

    reader_thread = threading.Thread(target=read)

    def non_atomic_replace(source, target):
        # The virtiofs-like window: the old record is gone, the new one not yet visible.
        os.unlink(target)
        window.set()
        with reader_progress:
            assert reader_progress.wait_for(
                lambda: "read" in outcome or "asked" in outcome, GUARD_SECONDS
            )
        real_replace(source, target)

    def note_lock(locked_attempt: str) -> None:
        if threading.current_thread() is reader_thread and locked_attempt == attempt_id:
            with reader_progress:
                outcome["asked"] = True
                reader_progress.notify_all()

    monkeypatch.setattr(journal_module.os, "replace", non_atomic_replace)
    writer = threading.Thread(
        target=lambda: journal.update_runner_state(attempt_id, lambda local: {**local, "k": 1})
    )
    writer.start()
    assert window.wait(GUARD_SECONDS)
    journal.on_lock = note_lock
    reader_thread.start()
    reader_thread.join(GUARD_SECONDS)
    writer.join(GUARD_SECONDS)
    assert not reader_thread.is_alive() and not writer.is_alive()
    monkeypatch.setattr(journal_module.os, "replace", real_replace)

    read = outcome["read"]
    assert not isinstance(read, BaseException), repr(read)
    if reader == "load":
        assert read.runner_state == {"k": 1}
    else:
        assert read is True


def _read(journal, reader, attempt_id):
    try:
        return journal.load(attempt_id) if reader == "load" else journal.exists(attempt_id)
    except BaseException as exc:  # recorded for the assertion, never swallowed
        return exc


def test_corruption_reports_only_cause_length_and_digest(tmp_path) -> None:
    attempt_id, journal = _prepared(tmp_path)
    raw = ('{"authority": "' + SECRET_MARKER).encode()
    journal.path_for(attempt_id).write_bytes(raw)

    with pytest.raises(JournalCorruption) as caught:
        journal.load(attempt_id)

    diagnostic = caught.value.diagnostic
    assert diagnostic == {
        "cause": "JSONDecodeError",
        "length": len(raw),
        "sha256_16": hashlib.sha256(raw).hexdigest()[:16],
    }
    assert SECRET_MARKER not in str(caught.value)
    assert SECRET_MARKER not in repr(caught.value)


def test_missing_record_is_reported_as_absent_bytes(tmp_path) -> None:
    attempt_id, journal = _prepared(tmp_path)
    journal.path_for(attempt_id).unlink()

    with pytest.raises(JournalCorruption) as caught:
        journal.load(attempt_id)

    assert caught.value.diagnostic == {
        "cause": "FileNotFoundError",
        "length": None,
        "sha256_16": None,
    }


def test_the_loop_warning_names_the_cause_without_record_content(
    tmp_path, caplog, monkeypatch
) -> None:
    # Alembic's fileConfig (migrations/env.py) disables loggers that already exist.
    monkeypatch.setattr(logging.getLogger("nexa.worker.agent"), "disabled", False)
    attempt_id, journal = _prepared(tmp_path)
    raw = ('{"authority": "' + SECRET_MARKER).encode()
    journal.path_for(attempt_id).write_bytes(raw)
    agent = WorkerAgent(
        worker_id=str(new_uuid7()),
        incarnation_id=str(new_uuid7()),
        installation_id=INSTALLATION_ID,
        client=object(),
        state=None,
        journal=journal,
        docker=None,
        provider=object(),
    )

    def _result_once() -> None:
        agent.stop()
        journal.load(attempt_id)

    with caplog.at_level(logging.WARNING, logger="nexa.worker.agent"):
        asyncio.run(agent._loop(_result_once, 0))

    [message] = [record.getMessage() for record in caplog.records]
    digest = hashlib.sha256(raw).hexdigest()[:16]
    assert message == (
        "worker_loop_failed operation=_result_once detail=JournalCorruption "
        f"cause=JSONDecodeError length={len(raw)} sha256_16={digest}"
    )
    assert SECRET_MARKER not in caplog.text
