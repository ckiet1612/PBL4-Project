"""CPU recognition accepts the runner's bytes and rejects mismatched bindings."""

from copy import deepcopy
from hashlib import sha256
from uuid import UUID

import pytest
import rfc8785

from nexa.application.errors import ApplicationError
from nexa.application.result_validation import validate_cpu_result_manifest
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.workloads.test_runner import result_provenance


def _case():
    result_id = new_uuid7()
    output_id = new_uuid7()
    provenance = result_provenance()
    document = {
        "kind": "RESULT",
        "schema_version": 1,
        "result_id": str(result_id),
        "created_at": "2026-09-23T01:02:03.000Z",
        "provenance": provenance,
        "status": "SUCCEEDED",
        "files": [
            {
                "artifact_id": str(output_id),
                "logical_name": "result.json",
                "media_type": "application/vnd.nexa.cpu-iterative-result+json",
                "size_bytes": 11,
                "checksum": "sha256:" + sha256(b'{"value":1}').hexdigest(),
            }
        ],
        "metrics": {},
    }
    document["manifest_checksum"] = "sha256:" + sha256(rfc8785.dumps(document)).hexdigest()
    raw = rfc8785.dumps(document)
    artifact = {
        "kind": "RESULT_MANIFEST",
        "media_type": "application/json",
        "size_bytes": len(raw),
        "checksum": "sha256:" + sha256(raw).hexdigest(),
    }
    return document, raw, artifact, result_id, provenance


def test_runner_manifest_canonical_bytes_bind_one_cpu_output():
    document, raw, artifact, result_id, provenance = _case()
    assert (
        validate_cpu_result_manifest(
            document,
            raw=raw,
            artifact=artifact,
            expected_result_id=result_id,
            provenance=provenance,
        )
        == document["files"][0]
    )


@pytest.mark.parametrize("changed", ["provenance", "kind", "output_kind", "bytes", "checksum"])
def test_manifest_rejects_changed_origin_and_bytes(changed):
    document, raw, artifact, result_id, provenance = _case()
    document = deepcopy(document)
    artifact = dict(artifact)
    if changed == "provenance":
        document["provenance"]["attempt_id"] = str(new_uuid7())
    elif changed == "kind":
        document["kind"] = "INPUT"
    elif changed == "output_kind":
        document["files"][0]["media_type"] = "application/json"
    elif changed == "bytes":
        raw += b" "
    elif changed == "checksum":
        artifact["checksum"] = "sha256:" + "0" * 64
    with pytest.raises(ApplicationError) as error:
        validate_cpu_result_manifest(
            document,
            raw=raw,
            artifact=artifact,
            expected_result_id=UUID(str(result_id)),
            provenance=provenance,
        )
    assert error.value.status == 422
