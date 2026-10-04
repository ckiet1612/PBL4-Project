"""Shared JSON logging for the API, coordinator and worker (PLAN §9, B19).

Stdlib only. Every line carries `ts`, `level`, `service`, `logger`, `event` and
`message`, the bound context IDs and any `extra` fields. Redaction runs on the handler,
so message text, structured fields, exception text and tracebacks are masked before
anything is written.
"""

import json
import logging
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

REDACTED = "[REDACTED]"
CONTEXT_FIELDS = ("request_id", "job_id", "attempt_id", "worker_id", "tenant_id")
_CONTEXT: ContextVar[tuple[tuple[str, str], ...]] = ContextVar("nexa_log_context", default=())

_SECRET_WORDS = (
    r"password|passwd|token|secret|cookie|csrf|authorization|credential|database_url|dsn"
)
_SENSITIVE_KEY = re.compile(_SECRET_WORDS, re.IGNORECASE)


def _mask_pair(match: re.Match[str]) -> str:
    quote = match["quote"] or ""
    return f"{match['key']}{match['sep']}{quote}{match['scheme'] or ''}{REDACTED}{quote}"


# Every pattern is anchored by a look-behind and uses possessive quantifiers, so the
# work stays linear in the text length: a hostile 64 KiB token (e.g. an HTTP method)
# cannot stall the event loop (B19-RV01).
_PATTERNS = (
    # Bearer header values, whatever the token format.
    (re.compile(r"(?i)\bbearer\s++[A-Za-z0-9._~+/=-]++"), f"Bearer {REDACTED}"),
    # Nexa opaque secrets (session cookie, CLI token, worker credential): <uuid>.<43 b64url>.
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
            r"\.[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"
        ),
        REDACTED,
    ),
    # URL credentials, e.g. postgresql://user:pass@host; the password may contain "/".
    (
        re.compile(r"(?i)(?<![\w+.-])([a-z][a-z0-9+.-]*+://)[^\s:/@]++:(?:(?!://)[^\s@])*+@"),
        rf"\1{REDACTED}@",
    ),
    # key=value / key: value pairs whose key names a secret. A quoted value is masked up
    # to its closing quote (or the end of the text); a [...] or {...} value up to its
    # closing bracket, two levels deep, else to the end of the line; a bare value up to
    # whitespace, ",", "}" or "]", keeping an HTTP auth scheme such as "Basic".
    (
        re.compile(
            rf"(?i)(?<![\w.-])(?=[\w.-]*?(?:{_SECRET_WORDS}))(?P<key>[\w.-]++)"
            r"(?P<sep>[\"']?\s*+[:=]\s*+)"
            r"(?:(?P<quote>[\"'])(?:\\[\s\S]?|(?!(?P=quote))[^\\])*+(?:(?P=quote)|\Z)"
            r"|(?!Bearer\b|\[REDACTED\])"
            r"(?:\[(?:[^\[\]]|\[[^\[\]]*+\])*+(?:\]|[^\n]*+)"
            r"|\{(?:[^{}]|\{[^{}]*+\})*+(?:\}|[^\n]*+)"
            r"|(?P<scheme>(?:basic|digest|negotiate)\s++)?[^\s,}\]]++))"
        ),
        _mask_pair,
    ),
)
_EVENT_CODE = re.compile(r"[a-z][a-z0-9]*(?:[._][a-z0-9]+)+")
_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
    | {"message", "asctime", "taskName", "nexa_fields", "color_message"}
)
_NOISY = {"httpx": logging.WARNING, "httpcore": logging.WARNING}


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(k): redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [redact_value(item) for item in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return redact_text(str(value))


def _merged(fields: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    merged = dict(_CONTEXT.get())
    merged.update({k: str(v) for k, v in fields.items() if v is not None})
    return tuple(merged.items())


@contextmanager
def bind_log_context(**fields: object) -> Iterator[None]:
    """Attach context IDs (request/job/attempt/worker/tenant) to lines logged inside."""
    unknown = set(fields) - set(CONTEXT_FIELDS)
    if unknown:
        raise ValueError("unsupported log context field")
    token = _CONTEXT.set(_merged(fields))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def set_log_context(**fields: object) -> None:
    """Bind context for the rest of the current context (e.g. a worker process)."""
    if set(fields) - set(CONTEXT_FIELDS):
        raise ValueError("unsupported log context field")
    _CONTEXT.set(_merged(fields))


def log_event(logger: logging.Logger, level: int, event: str, /, **fields: Any) -> None:
    """Log one event code with structured fields; `message` defaults to the event."""
    message = fields.pop("message", event)
    exc_info = fields.pop("exc_info", None)
    logger.log(level, message, exc_info=exc_info, extra={"nexa_fields": {"event": event, **fields}})


class RedactionFilter(logging.Filter):
    """Mask secrets in the message, args, extra fields and exception of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string must not leak args
            message = str(record.msg)
        record.msg = redact_text(message)
        record.args = None
        fields = getattr(record, "nexa_fields", None)
        if isinstance(fields, Mapping):
            record.nexa_fields = redact_value(fields)
        for name, value in list(vars(record).items()):
            if name not in _RECORD_ATTRS:
                setattr(record, name, redact_value(value, name))
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_text = redact_text(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
        }
        line.update(dict(_CONTEXT.get()))
        merged: dict[str, Any] = {}
        if message.startswith("{"):
            try:
                parsed = json.loads(message)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                merged = parsed
        line.update(merged)
        for name, value in vars(record).items():
            if name not in _RECORD_ATTRS:
                line[name] = value
        fields = getattr(record, "nexa_fields", None)
        if isinstance(fields, Mapping):
            line.update(fields)
        if "event" not in line:
            line["event"] = message if _EVENT_CODE.fullmatch(message) else "log"
        line["message"] = message
        if record.exc_text or record.exc_info:
            line["exception"] = record.exc_text or self.formatException(record.exc_info)
        return json.dumps(line, default=str, separators=(",", ":"), ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__(f"%(asctime)s %(levelname)s {service} %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        fields = getattr(record, "nexa_fields", None)
        if isinstance(fields, Mapping):
            text += " " + json.dumps(fields, default=str, separators=(",", ":"))
        return text


class _StderrHandler(logging.StreamHandler):
    """Write to the current `sys.stderr`, so a replaced stream is never left dangling."""

    @property  # type: ignore[override]
    def stream(self):  # noqa: ANN201
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:  # noqa: ANN001
        pass


def configure_logging(service: str, *, level: str = "INFO", fmt: str = "json") -> None:
    """Install the Nexa stderr handler on the root logger; idempotent per process.

    Foreign root handlers (pytest capture, embedding applications) are kept. uvicorn's
    own handlers are removed so its lines use this handler at `level`, and its access
    logger is disabled because `http_request` replaces it without query strings.
    """
    root = logging.getLogger()
    root.handlers[:] = [h for h in root.handlers if not getattr(h, "_nexa_handler", False)]
    handler = _StderrHandler()
    handler._nexa_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter(service) if fmt == "json" else _TextFormatter(service))
    handler.addFilter(RedactionFilter())
    root.addHandler(handler)
    root.setLevel(level)
    for name, noisy_level in _NOISY.items():
        logging.getLogger(name).setLevel(noisy_level)
    for name in ("uvicorn", "uvicorn.error"):
        # uvicorn's dictConfig pins these at INFO before the app imports (B19-RV07).
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True
        logger.setLevel(level)
    access = logging.getLogger("uvicorn.access")
    access.handlers[:] = []
    access.propagate = False
    access.disabled = True
