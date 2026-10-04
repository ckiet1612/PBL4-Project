"""B19 JSON logs: shared formatter, context fields and redaction (PLAN §9, ACC-26/27)."""

import io
import json
import logging
import re

import pytest

from nexa.observability.logging import (
    JsonFormatter,
    RedactionFilter,
    bind_log_context,
    configure_logging,
    log_event,
    redact_text,
)

UUID = "0192f6a0-1234-7abc-8def-0123456789ab"
NEXA_SECRET = f"{UUID}.{'A' * 20}_{'b' * 22}"  # IdentifiedSecret format: <uuid>.<43 b64url>
CANARIES = {
    "bearer": f"Authorization: Bearer {NEXA_SECRET}",
    "nexa_token": f"token issued {NEXA_SECRET} ok",
    "dsn": "connect postgresql+psycopg://nexa:Sup3rS3cret@db:5432/nexa failed",
    "kv": "password=Sup3rS3cret csrf_token: abcCSRFdef cookie=nexa_session",
}
SECRETS = ("Sup3rS3cret", "abcCSRFdef", "nexa_session", NEXA_SECRET, "A" * 20)


def _capture(service="api", fmt="json", level="DEBUG"):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(service) if fmt == "json" else logging.Formatter())
    handler.addFilter(RedactionFilter())
    logger = logging.getLogger(f"nexa.test.b19.{service}.{fmt}")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(level)
    return logger, stream


def _lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_line_has_required_fields_in_utc_milliseconds():
    logger, stream = _capture()
    log_event(logger, logging.WARNING, "coordinator_tick_unavailable", reason="database")
    (line,) = _lines(stream)
    assert line["level"] == "WARNING"
    assert line["service"] == "api"
    assert line["logger"] == logger.name
    assert line["event"] == "coordinator_tick_unavailable"
    assert line["reason"] == "database"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", line["ts"])
    assert "message" in line


def test_extra_fields_become_json_fields_and_plain_codes_become_events():
    logger, stream = _capture()
    logger.warning("coordinator_maintenance_failed", extra={"step": "reap"})
    logger.info("Started server process")
    first, second = _lines(stream)
    assert first["event"] == "coordinator_maintenance_failed"
    assert first["step"] == "reap"
    assert second["event"] == "log"
    assert second["message"] == "Started server process"


def test_json_object_message_is_merged_and_keeps_its_event():
    logger, stream = _capture()
    logger.warning(json.dumps({"event": "worker_callback_rejected", "code": "stale_authority"}))
    (line,) = _lines(stream)
    assert line["event"] == "worker_callback_rejected"
    assert line["code"] == "stale_authority"


def test_context_ids_are_attached_and_reset():
    logger, stream = _capture()
    with bind_log_context(request_id="r-1", job_id="j-1"):
        with bind_log_context(attempt_id="a-1", worker_id="w-1", tenant_id="t-1"):
            log_event(logger, logging.INFO, "inner")
        log_event(logger, logging.INFO, "outer")
    log_event(logger, logging.INFO, "after")
    inner, outer, after = _lines(stream)
    assert {inner[k] for k in ("request_id", "job_id", "attempt_id", "worker_id", "tenant_id")} == {
        "r-1",
        "j-1",
        "a-1",
        "w-1",
        "t-1",
    }
    assert outer["request_id"] == "r-1" and "attempt_id" not in outer
    assert "request_id" not in after


@pytest.mark.parametrize("name", sorted(CANARIES))
@pytest.mark.parametrize("fmt", ["json", "text"])
def test_redaction_masks_secret_patterns_in_messages(name, fmt):
    logger, stream = _capture(fmt=fmt)
    logger.error(CANARIES[name])
    output = stream.getvalue()
    for secret in SECRETS:
        assert secret not in output
    assert "[REDACTED]" in output


def test_redaction_masks_sensitive_keys_and_nested_values():
    logger, stream = _capture()
    log_event(
        logger,
        logging.INFO,
        "probe",
        password="Sup3rS3cret",
        details={"database_url": "x", "items": [{"Authorization": "abcCSRFdef"}]},
        token_value="nexa_session",
        status=200,
    )
    output = stream.getvalue()
    for secret in SECRETS:
        assert secret not in output
    (line,) = _lines(stream)
    assert line["password"] == "[REDACTED]"
    assert line["status"] == 200


def test_redaction_covers_exception_text_and_traceback():
    logger, stream = _capture()
    try:
        raise RuntimeError(f"dsn postgresql://nexa:Sup3rS3cret@db/nexa token {NEXA_SECRET}")
    except RuntimeError:
        logger.exception("unhandled_exception")
    output = stream.getvalue()
    for secret in SECRETS:
        assert secret not in output
    (line,) = _lines(stream)
    assert "RuntimeError" in line["exception"]


@pytest.mark.parametrize(
    "text",
    [
        'password="hunter two words"',
        "password='hunter two words'",
        '{"password": "hunter two words", "status": 200}',
        "{'db_password': 'hunter two words'}",
        "password=hunter;two&words",
        'password="esc\\"aped hunter two words"',
        "dsn postgresql://nexa:hunter/two/words@db:5432/nexa",
    ],
)
def test_redaction_masks_the_whole_secret_value(text):
    redacted = redact_text(text)
    for part in ("hunter", "two", "words"):
        assert part not in redacted, redacted
    assert "[REDACTED]" in redacted


def test_redaction_keeps_the_text_after_a_quoted_secret():
    assert redact_text('password="a b" status=200') == 'password="[REDACTED]" status=200'


def test_redact_text_keeps_ordinary_ids_and_codes():
    text = f"job {UUID} attempt_id={UUID} code=storage_pressure"
    assert redact_text(text) == text


def test_configure_logging_is_idempotent_and_quiets_noisy_loggers(capsys):
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    try:
        configure_logging("coordinator", level="INFO", fmt="json")
        configure_logging("coordinator", level="INFO", fmt="json")
        ours = [h for h in root.handlers if getattr(h, "_nexa_handler", False)]
        assert len(ours) == 1
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert logging.getLogger("uvicorn").level == logging.INFO
        assert logging.getLogger("uvicorn.error").level == logging.INFO
        access = logging.getLogger("uvicorn.access")
        assert not access.hasHandlers()
        logging.getLogger("nexa.coordinator").warning(
            "coordinator_tick_unavailable", extra={"secret": "Sup3rS3cret"}
        )
        err = capsys.readouterr().err.strip().splitlines()[-1]
        line = json.loads(err)
        assert line["service"] == "coordinator"
        assert line["secret"] == "[REDACTED]"
    finally:
        root.handlers[:] = before
        root.setLevel(level)


def test_configure_logging_keeps_foreign_root_handlers():
    root = logging.getLogger()
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    before, level = list(root.handlers), root.level
    try:
        configure_logging("api", level="WARNING", fmt="text")
        assert foreign in root.handlers
    finally:
        root.handlers[:] = before
        root.setLevel(level)


@pytest.mark.parametrize(
    "text",
    [
        "a" * 65536,
        "a." * 32768,
        "a-" * 32768,
        "password" * 8192,
        "a_password" * 6554,
        "x://a:" * 10923,
        "/a://b:" * 9362,
    ],
)
def test_redact_text_is_linear_on_long_inputs(text):
    # B19-RV01: a 64 KiB value (e.g. an HTTP method token) must not stall the caller.
    import time

    started = time.perf_counter()
    redact_text(text)
    assert time.perf_counter() - started < 0.05


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Basic dXNlcjpwYXNz",
        "authorization=Digest dXNlcjpwYXNz",
        '{"Authorization": "Basic dXNlcjpwYXNz"}',
        "secret: [1, dXNlcjpwYXNz]",
        "secret: [[1], [dXNlcjpwYXNz]] tail",
        "token={a: 1, b: dXNlcjpwYXNz}",
        'password="dXNlcjpwYXNz',
    ],
)
def test_redaction_masks_auth_schemes_and_bracketed_values(text):
    # B19-RV10
    redacted = redact_text(text)
    assert "dXNlcjpwYXNz" not in redacted, redacted
    assert "[REDACTED]" in redacted


def test_redaction_keeps_the_auth_scheme_and_following_text():
    assert redact_text("Authorization: Basic dXNlcjpwYXNz next=1") == (
        "Authorization: Basic [REDACTED] next=1"
    )
    assert redact_text("secret: [1, 2] status=200") == "secret: [REDACTED] status=200"


def test_redaction_does_not_reredact_an_already_masked_bearer():
    assert redact_text(f"Authorization: Bearer {NEXA_SECRET}") == "Authorization: Bearer [REDACTED]"
