"""Logging setup with correlation IDs.

Every log line carries two IDs when they're known:
- request_id: one per HTTP request (taken from an incoming X-Request-ID header or
  generated) and returned to the caller in the X-Request-ID response header;
- message_id: the stable ID of the email being processed.

They live in context variables, so any code (the pipeline, the ERP service, MCP tools)
gets them in its log lines without passing them around. To investigate a bug, grep the
logs for the request_id from the response, or for the email's message_id.

Raw email bodies are never logged; they're stored in the emails table by message_id.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
message_id_var: ContextVar[str] = ContextVar("message_id", default="-")

# Chatty third-party loggers, kept at WARNING unless LOG_LEVEL=DEBUG.
_NOISY = ("httpx", "httpx2", "httpcore", "anthropic", "openai", "mcp", "fastmcp", "sse_starlette")

# Attributes every LogRecord has; anything else came from `extra=` and is logged as a field.
# (color_message is uvicorn's ANSI-colored duplicate of the message.)
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "color_message"}


class ContextFilter(logging.Filter):
    """Adds request_id and message_id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.message_id = message_id_var.get()
        return True


# Shorter names for display. uvicorn logs its startup/shutdown messages on a logger
# called "uvicorn.error", which reads like an error but isn't.
_DISPLAY_NAMES = {"uvicorn.error": "uvicorn"}

# Attributes TextFormatter adds to records for its own use (not logged as extra fields).
_FORMATTER_ATTRS = {"ids", "display_name", "extras"}


class TextFormatter(logging.Formatter):
    """Human-readable: time level logger [req=… msg=…] message key=value...

    The [req=… msg=…] block is left out for lines outside any request or email
    (startup, shutdown), where both IDs would be empty.
    """

    def __init__(self):
        super().__init__(
            "%(asctime)s %(levelname)-7s %(display_name)s%(ids)s %(message)s%(extras)s"
        )

    def format(self, record: logging.LogRecord) -> str:
        request_id = getattr(record, "request_id", "-")
        message_id = getattr(record, "message_id", "-")
        record.ids = (
            "" if request_id == message_id == "-" else f" [req={request_id} msg={message_id}]"
        )
        record.display_name = _DISPLAY_NAMES.get(record.name, record.name)
        extras = _extra_fields(record)
        # Extra fields go on the message line, before any traceback.
        record.extras = "".join(f" {k}={v}" for k, v in extras.items())
        return super().format(record)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "message_id": getattr(record, "message_id", "-"),
            **_extra_fields(record),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Configure the root logger once per process (safe to call again)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())

    third_party_level = logging.DEBUG if level.upper() == "DEBUG" else logging.WARNING
    for name in _NOISY:
        logging.getLogger(name).setLevel(third_party_level)
    # Our middleware logs every request; uvicorn's access log would duplicate it.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True


def _extra_fields(record: logging.LogRecord) -> dict:
    return {
        k: v
        for k, v in vars(record).items()
        if k not in _STANDARD_ATTRS
        and k not in _FORMATTER_ATTRS
        and k not in ("request_id", "message_id")
    }
