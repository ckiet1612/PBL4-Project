"""B19 6.A/R06: watermark cells per operation and store error wire codes, without a DB."""

import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexa.application import storage_pressure as sp
from nexa.application.errors import ApplicationError
from nexa.infrastructure.artifacts.store import ArtifactError, FilesystemArtifactStore
from nexa.observability import metrics_api

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = SimpleNamespace(storage_high_watermark_percent=85, storage_critical_watermark_percent=95)


def _wire_codes() -> set[str]:
    text = (ROOT / "docs/contracts/openapi.yaml").read_text()
    match = re.search(r"ErrorCode: \{ type: string, enum: \[([^\]]+)\]", text)
    assert match is not None
    return {code.strip() for code in match.group(1).split(",")}


def _store_codes() -> set[str]:
    text = (ROOT / "src/nexa/infrastructure/artifacts/store.py").read_text()
    return set(re.findall(r'ArtifactError\(\s*"([a-z_]+)"', text)) | {"storage_full"}


def _rejections(reason, operation):
    return metrics_api.STORAGE_REJECTIONS.labels(reason, operation)._value.get()


@pytest.mark.parametrize("code", sorted(_store_codes()))
@pytest.mark.parametrize("operation", [sp.USER_UPLOAD, sp.WORKER_UPLOAD])
def test_every_store_code_maps_to_a_published_error_code(code, operation):
    failure = sp.store_failure(ArtifactError(code, "x"), operation)
    assert failure.code in _wire_codes()
    assert failure.code in metrics_api.ERROR_CODES
    if failure.status == 503:
        assert failure.retry_after == 1


def test_enospc_is_storage_pressure_and_counted():
    before = _rejections("enospc", sp.WORKER_UPLOAD)
    failure = sp.store_failure(ArtifactError("storage_full", "full"), sp.WORKER_UPLOAD)
    assert (failure.code, failure.status, failure.retry_after) == ("storage_pressure", 503, 1)
    assert _rejections("enospc", sp.WORKER_UPLOAD) == before + 1
    unavailable = sp.store_failure(ArtifactError("storage_unavailable", "eio"), sp.USER_UPLOAD)
    assert (unavailable.code, unavailable.status) == ("dependency_unavailable", 503)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.mark.parametrize(
    ("code", "logged"),
    [
        ("storage_unavailable", True),
        ("not_found", True),
        ("storage_full", False),
        ("checksum_mismatch", False),
        ("size_mismatch", False),
    ],
)
def test_mapping_to_dependency_unavailable_logs_the_internal_code_once(code, logged):
    """B19-RV04: the operator sees the internal store code; the client never does."""
    logger = logging.getLogger(sp.__name__)
    handler = _Records()
    disabled, logger.disabled = logger.disabled, False
    logger.addHandler(handler)
    try:
        failure = sp.store_failure(ArtifactError(code, "/srv/secret/path"), sp.USER_UPLOAD)
    finally:
        logger.removeHandler(handler)
        logger.disabled = disabled
    assert (failure.code == "dependency_unavailable") is logged
    events = [r for r in handler.records if r.nexa_fields["event"] == "artifact_store_unavailable"]
    assert len(events) == (1 if logged else 0)
    if logged:
        (record,) = events
        assert record.levelno == logging.WARNING
        assert record.nexa_fields == {
            "event": "artifact_store_unavailable",
            "code": code,
            "operation": sp.USER_UPLOAD,
        }
        assert "/srv/secret/path" not in record.getMessage()
        assert code not in failure.message


@pytest.mark.parametrize(
    ("used", "operation", "rejected", "reason"),
    [
        (50, sp.ADMISSION, False, None),
        (85, sp.ADMISSION, True, "high_watermark"),
        (94, sp.USER_UPLOAD, True, "high_watermark"),
        (95, sp.USER_UPLOAD, True, "critical_watermark"),
        (50, sp.WORKER_UPLOAD, False, None),
        (94, sp.WORKER_UPLOAD, False, None),
        (95, sp.WORKER_UPLOAD, True, "critical_watermark"),
    ],
)
def test_watermark_table(used, operation, rejected, reason):
    reading = sp.StorageReading(total=100, free=100 - used)
    if not rejected:
        sp.enforce_watermark(reading, operation, SETTINGS)
        return
    before = _rejections(reason, operation)
    with pytest.raises(ApplicationError) as failure:
        sp.enforce_watermark(reading, operation, SETTINGS)
    assert (failure.value.code, failure.value.status) == ("storage_pressure", 503)
    assert failure.value.retry_after == 1
    assert _rejections(reason, operation) == before + 1


def test_expected_bytes_count_toward_the_watermark():
    reading = sp.StorageReading(total=100, free=20)
    sp.enforce_watermark(reading, sp.USER_UPLOAD, SETTINGS, extra_bytes=4)
    with pytest.raises(ApplicationError):
        sp.enforce_watermark(reading, sp.USER_UPLOAD, SETTINGS, extra_bytes=5)


def test_failed_statvfs_fails_closed_after_replay_and_is_never_cached(tmp_path):
    calls = []

    def statvfs(_path):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("statvfs failed")
        return SimpleNamespace(f_frsize=1, f_blocks=100, f_bavail=60)

    store = FilesystemArtifactStore(tmp_path / "a", max_file_bytes=1024, statvfs_fn=statvfs)
    reading = sp.read_storage(store)
    assert isinstance(reading, ApplicationError) and reading.code == "dependency_unavailable"
    before = _rejections("unavailable", sp.ADMISSION)
    with pytest.raises(ApplicationError):
        sp.enforce_watermark(reading, sp.ADMISSION, SETTINGS)
    assert _rejections("unavailable", sp.ADMISSION) == before + 1
    assert sp.read_storage(store) == sp.StorageReading(100, 60)
    # The good reading is cached for a second; fresh bypasses the cache.
    assert sp.read_storage(store) == sp.StorageReading(100, 60) and len(calls) == 2
    sp.read_storage(store, fresh=True)
    assert len(calls) == 3
