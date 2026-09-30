"""Remediation B15-R05, worker side: an inherited restore is checked like an own one.

The server proves the source Job of an inherited checkpoint (B15-R05); the worker still
verifies what the server froze in the claim. Only the source session is taken from the
server's proof: a manifest, file or byte checksum that does not match is refused and the
Attempt fails INCOMPATIBLE before any container exists.
"""

import hashlib

import pytest

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.worker import dispatch
from tests.worker.test_checkpoint_flow_b14 import (
    ARCH,
    Api,
    Backend,
    Harness,
    _reseal,
    claim_context,
    sealed_restore,
)


def _inherited():
    """A committed checkpoint of the Job this manual retry was created from."""
    restore, raw = sealed_restore(claim_context())
    source_job, source_session = str(new_uuid7()), str(new_uuid7())
    restore["record"]["job_id"] = source_job
    restore["manifest"]["provenance"].update(job_id=source_job, session_id=source_session)
    _reseal(restore)
    return restore, raw


@pytest.mark.parametrize("defect", ["none", "manifest_checksum", "file_checksum"])
def test_inherited_restore_manifest_must_match_the_server_checksums(defect):
    restore, _ = _inherited()
    if defect == "manifest_checksum":
        restore["record"]["manifest_checksum"] = "sha256:" + "0" * 64
    elif defect == "file_checksum":
        restore["manifest"]["files"][0]["checksum"] = "sha256:" + "0" * 64
        _reseal(restore)
    context = claim_context(restore=restore)
    launch = dispatch.checkpoint_launch(context, image_capable=True)
    if defect == "none":
        dispatch.verify_restore_manifest(context, ARCH, launch)
    else:
        with pytest.raises(dispatch.RestoreUnavailable):
            dispatch.verify_restore_manifest(context, ARCH, launch)


def test_inherited_restore_bytes_that_differ_fail_without_a_container(tmp_path):
    restore, raw = _inherited()
    changed = raw[:-1] + b" "
    assert "sha256:" + hashlib.sha256(changed).hexdigest() != restore["files"][0]["checksum"]
    context = claim_context(restore=restore)
    api = Api(context, blobs={restore["files"][0]["artifact_id"]: changed})
    harness = Harness(tmp_path, context, api=api, backend=Backend(tmp_path / "fs" / "output"))
    harness.dispatch()
    assert [(f["failure_class"], f["reason_code"]) for f in api.failures] == [
        ("INCOMPATIBLE", "CHECKPOINT_RESTORE_UNAVAILABLE")
    ]
    assert api.failures[0]["observation"]["observation_type"] == "NO_CONTAINER"
    assert harness.backend.created == 0
