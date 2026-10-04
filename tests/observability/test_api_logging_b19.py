"""B19 API access line and unhandled-exception line, without a database."""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from nexa.api.app import create_app
from nexa.config import load_settings
from nexa.observability.logging import JsonFormatter, RedactionFilter
from tests.test_config import SAFE_ENV

SECRET = "postgresql://nexa:Sup3rS3cret@db/nexa"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[dict] = []
        self.setFormatter(JsonFormatter("api"))
        self.addFilter(RedactionFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


def _client():
    app = create_app(load_settings(SAFE_ENV))

    @app.get("/v1/_b19/boom/{item_id}")
    def boom(item_id: str):
        raise RuntimeError(f"cannot reach {SECRET}")

    @app.get("/v1/_b19/ok")
    def ok():
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _restore_api_logger():
    # Later tests (B15-R33) read this logger's records; leave its level as found.
    logger = logging.getLogger("nexa.api.app")
    level, disabled = logger.level, logger.disabled
    yield
    logger.setLevel(level)
    logger.disabled = disabled


def _capture():
    logger = logging.getLogger("nexa.api.app")
    handler = _Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # An earlier in-process Alembic run (PG fixtures) disables existing loggers.
    logger.disabled = False
    return logger, handler


def test_access_line_uses_route_template_and_drops_query_and_headers():
    logger, handler = _capture()
    try:
        response = _client().get(
            "/v1/_b19/ok?token=leak-me", headers={"Authorization": "Bearer leak-me-too"}
        )
    finally:
        logger.removeHandler(handler)
    assert response.status_code == 200
    (line,) = [line for line in handler.lines if line["event"] == "http_request"]
    assert line["route"] == "/v1/_b19/ok"
    assert line["method"] == "GET"
    assert line["status"] == 200
    assert line["request_id"] == response.headers["X-Request-Id"]
    assert isinstance(line["duration_ms"], float)
    assert "leak-me" not in json.dumps(handler.lines)


def test_unmatched_route_is_not_echoed():
    logger, handler = _capture()
    try:
        _client().get("/v1/no/such/7f1c?x=1")
    finally:
        logger.removeHandler(handler)
    (line,) = [line for line in handler.lines if line["event"] == "http_request"]
    assert line["route"] == "unmatched"
    assert line["status"] == 404


def test_unhandled_exception_logs_request_id_route_and_redacted_traceback():
    logger, handler = _capture()
    try:
        response = _client().get("/v1/_b19/boom/abc")
    finally:
        logger.removeHandler(handler)
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    events = {line["event"]: line for line in handler.lines}
    crash = events["unhandled_exception"]
    assert crash["route"] == "/v1/_b19/boom/{item_id}"
    assert crash["request_id"] == response.json()["request_id"]
    assert "RuntimeError" in crash["exception"]
    assert events["http_request"]["status"] == 500
    assert "Sup3rS3cret" not in json.dumps(handler.lines)


@pytest.mark.parametrize("method", ["BREW", "X" * 16000])
def test_access_line_logs_the_closed_method_label_not_the_raw_method(method):
    # B19-RV01: the raw token never reaches the log; the label is shared with the metrics.
    logger, handler = _capture()
    try:
        _client().request(method, "/v1/_b19/ok")
    finally:
        logger.removeHandler(handler)
    (line,) = [line for line in handler.lines if line["event"] == "http_request"]
    assert line["method"] == "OTHER"
    assert method not in json.dumps(handler.lines)
