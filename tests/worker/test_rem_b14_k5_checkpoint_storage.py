"""Remediation B14-K5 on the worker: a checkpoint copy without space fails INTERNAL.

The B14 harness drives the production worker loop and the real trusted runner; only
Docker and HTTP are faked. When the runner cannot copy checkpoint bytes into its
bounded ``/output`` staging (ENOSPC/EDQUOT/EIO) it emits FAILED
INTERNAL/CHECKPOINT_STORAGE_FAILED; the worker forwards that safe reason once, cleans
up the exact container and publishes nothing. Before the fix the ``OSError`` escaped
the runner (exit 124, retryable INFRASTRUCTURE/RUNNER_UNAVAILABLE).
"""

import errno
import os

from nexa.worker.execution import container_exit_failure, terminal_frame_failure
from nexa.worker.protocol import STOP_EXIT_CODES
from nexa.workloads import chunk_manifest, trusted_runner
from tests.worker.test_checkpoint_flow_b14 import calls, started
from tests.worker.test_inference_flow_b16 import _started as inference_started
from tests.worker.test_inference_flow_b16 import _uploads, _write

STORAGE = ("INTERNAL", "CHECKPOINT_STORAGE_FAILED")


def _fail_writes(monkeypatch, name):
    write = trusted_runner._atomic_write

    def failing(path, payload, *, mode):
        if name(path.name):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        write(path, payload, mode=mode)

    monkeypatch.setattr(trusted_runner, "_atomic_write", failing)


def _failures(harness):
    return [(f["failure_class"], f["reason_code"]) for f in harness.api.failures]


def test_cpu_checkpoint_copy_without_space_fails_internal_once(tmp_path, monkeypatch):
    harness = started(tmp_path)
    _fail_writes(monkeypatch, lambda name: name.startswith("checkpoint-"))
    harness.advance(6)
    harness.cycle(8)

    assert len(calls(harness.api, "reserve_checkpoint")) == 1
    assert _failures(harness) == [STORAGE]
    assert [c["proof"]["container"]["container_id"] for c in harness.api.cleanups] == [
        harness.backend.container_id
    ]
    assert harness.api.published == {}
    assert calls(harness.api, "upload") == []


def test_inference_chunk_manifest_without_space_fails_internal(tmp_path, monkeypatch):
    harness = inference_started(tmp_path)
    manifest = f"checkpoint-1-{chunk_manifest.LOGICAL_NAME}"
    _fail_writes(monkeypatch, lambda name: name == manifest)
    _write(harness, 2, chunks=(0, 1))
    harness.advance(6)
    harness.cycle(12)

    assert _failures(harness) == [STORAGE]
    assert len(harness.api.cleanups) == 1
    assert harness.api.published == {}
    assert "CHUNK_OUTPUT_MANIFEST" not in _uploads(harness)


def test_storage_failure_frame_is_forwarded_and_never_retryable():
    frame = {
        "type": "FAILED",
        "payload": {
            "failure_class": "INTERNAL",
            "reason_code": "CHECKPOINT_STORAGE_FAILED",
            "exit_code": None,
            "oom_killed": False,
            "runtime_limit_reached": False,
        },
    }
    assert terminal_frame_failure(frame, {}) == STORAGE
    # Should the FAILED frame be lost, the runner's FAILURE exit status still names a
    # non-retryable internal failure, never RUNNER_UNAVAILABLE.
    for state in ({}, {"checkpoint_rejected": True}):
        failure_class, _ = container_exit_failure(
            {"exit_code": STOP_EXIT_CODES["FAILURE"], "oom_killed": False}, state
        )
        assert failure_class == "INTERNAL"
