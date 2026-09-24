"""Closed CPU result manifest checks before a fenced Result can be recognized."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime
from uuid import UUID

import rfc8785

from nexa.application.errors import ApplicationError

_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$")
_MANIFEST_FIELDS = {
    "kind",
    "schema_version",
    "result_id",
    "created_at",
    "provenance",
    "status",
    "files",
    "metrics",
    "manifest_checksum",
}
_FILE_FIELDS = {"artifact_id", "logical_name", "media_type", "size_bytes", "checksum"}


def _invalid(message: str) -> None:
    raise ApplicationError(code="validation_failed", status=422, message=message)


def validate_cpu_result_manifest(
    manifest: dict,
    *,
    raw: bytes,
    artifact: dict,
    expected_result_id: UUID,
    provenance: dict,
) -> dict:
    """Return the sole CPU file binding after checking canonical bytes and origin."""
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        _invalid("Result manifest fields are invalid")
    if (
        manifest["kind"] != "RESULT"
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["status"] != "SUCCEEDED"
        or manifest["result_id"] != str(expected_result_id)
        or manifest["provenance"] != provenance
    ):
        _invalid("Result identity or provenance is invalid")
    timestamp = manifest["created_at"]
    if not isinstance(timestamp, str) or not _TIMESTAMP.fullmatch(timestamp):
        _invalid("Result timestamp is invalid")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        _invalid("Result timestamp is invalid")
    metrics = manifest["metrics"]
    if (
        not isinstance(metrics, dict)
        or len(metrics) > 128
        or any(
            not isinstance(key, str)
            or not isinstance(value, (str, bool, int, float, type(None)))
            or (isinstance(value, float) and not math.isfinite(value))
            for key, value in metrics.items()
        )
    ):
        _invalid("Result metrics are invalid")
    files = manifest["files"]
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        _invalid("CPU result requires exactly one file binding")
    output = files[0]
    if (
        set(output) != _FILE_FIELDS
        or output["logical_name"] != "result.json"
        or output["media_type"] != "application/vnd.nexa.cpu-iterative-result+json"
        or type(output["size_bytes"]) is not int
        or not 0 <= output["size_bytes"] <= 10 * 1024**3
        or not isinstance(output["checksum"], str)
        or not _CHECKSUM.fullmatch(output["checksum"])
    ):
        _invalid("CPU result file binding is invalid")
    try:
        if UUID(output["artifact_id"]).version != 7:
            _invalid("Result file artifact ID is invalid")
        canonical_without_checksum = rfc8785.dumps(
            {key: value for key, value in manifest.items() if key != "manifest_checksum"}
        )
        canonical = rfc8785.dumps(manifest)
    except (TypeError, ValueError, KeyError, rfc8785.FloatDomainError, rfc8785.IntegerDomainError):
        _invalid("Result manifest is not canonical JSON")
    if (
        manifest["manifest_checksum"]
        != "sha256:" + hashlib.sha256(canonical_without_checksum).hexdigest()
        or raw != canonical
        or artifact["size_bytes"] != len(raw)
        or artifact["checksum"] != "sha256:" + hashlib.sha256(raw).hexdigest()
        or artifact["kind"] != "RESULT_MANIFEST"
        or artifact["media_type"] != "application/json"
    ):
        _invalid("Result manifest bytes or checksum mismatch")
    return output
