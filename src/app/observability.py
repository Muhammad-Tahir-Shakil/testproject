"""Structured JSON logging.

Logs Insights queries JSON natively, so one object per line turns the drift
questions in answers.md into queries that work on day one, with no metrics
pipeline:

    fields @timestamp, decision_state, top_confidence, decision_margin
    | filter event = "recommendation.generated"
    | stats count() by decision_state, bin(1h)

Extra fields pass through; customer identifiers and free text never do, so logs
stay inside the same PII boundary as the audit sink.
"""

import json
import logging
import os
import sys
from typing import Any


SERVICE_NAME = "vendor-dispatch"

# Attributes LogRecord always sets. Anything outside this set was supplied by
# the caller as `extra=` and is therefore a structured field worth emitting.
_RESERVED = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "taskName",
    "thread",
    "threadName",
}

# Never log these, whatever a caller passes. Mirrors events.SENSITIVE_KEYS so
# the two boundaries cannot drift apart silently.
_NEVER_LOG = {
    "address",
    "asset_label",
    "customer_email",
    "customer_name",
    "customer_phone",
    "details",
    "email",
    "phone",
    "site_name",
    "title",
}


class JsonFormatter(logging.Formatter):
    """Render each record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_") or key in _NEVER_LOG:
                continue
            if value is None:
                continue
            payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None) -> None:
    """Install the JSON formatter on the root handler.

    Lambda pre-installs a handler on the root logger, so reconfiguring that
    handler is what actually changes the output format; ``basicConfig`` alone
    is a no-op there.
    """

    resolved = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger()
    root.setLevel(resolved)
    if not root.handlers:
        root.addHandler(logging.StreamHandler(sys.stdout))
    for handler in root.handlers:
        handler.setFormatter(JsonFormatter())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
