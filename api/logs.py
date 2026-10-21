"""Logging: JSON lines on stderr for Azure (Log Analytics reads the console), readable lines locally.

The API never logs request bodies, subjects, ticket text or detected names. Request lines carry the
method, path, status, latency, request id, text length and whether an org id was given. Libraries that
could log request content at DEBUG (the Azure SDK, the HTTP clients) are kept at WARNING.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

# Extra fields a log record may carry (logger.info(..., extra={...})); everything else is dropped.
FIELDS = ("method", "path", "status", "latencyMs", "requestId", "textLength", "orgIdGiven", "caller", "operation")
QUIET = ("azure", "httpx", "httpx2", "httpcore", "httpcore2", "multipart", "uvicorn.access")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
               "level": record.levelname, "logger": record.name, "message": record.getMessage()}
        out.update({k: getattr(record, k) for k in FIELDS if hasattr(record, k)})
        if record.exc_info and record.exc_info[0]:
            out["exception"] = record.exc_info[0].__name__  # the type only: messages can hold request data
        return json.dumps(out, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        # The request line's message already says method, path and status
        extra = " ".join(f"{k}={getattr(record, k)}" for k in FIELDS
                         if hasattr(record, k) and k not in ("method", "path", "status"))
        saved, record.exc_info, record.exc_text = record.exc_info, None, None
        try:
            line = super().format(record)
        finally:
            record.exc_info = saved
        if saved and saved[0]:
            line += f" [{saved[0].__name__}]"
        return f"{line} {extra}" if extra else line


def configure(level: str = "INFO", fmt: str = "json") -> None:
    """Configure the root logger once, on stderr."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_ticket_api", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler._ticket_api = True
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    quiet()


def quiet() -> None:
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").disabled = True  # the request line comes from RequestContext
