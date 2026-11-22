"""Structured JSON logging with per-request correlation IDs."""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_STANDARD_RECORD_ATTRS = frozenset(
    {
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
        "thread",
        "threadName",
        "taskName",
        "service",
        "request_id",
    }
)


def get_request_id() -> str:
    """Return the active correlation request ID for the current async context."""
    return request_id_var.get()


class ServiceContextFilter(logging.Filter):
    """Inject service name and current request_id into every log record."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "service"):
            record.service = self.service_name
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        return True


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON with mandatory traceability fields."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "service": getattr(record, "service", self.service_name),
            "level": record.levelname,
            "request_id": getattr(record, "request_id", get_request_id()),
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = _safe_json_value(value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    """Human-readable log formatter for plain-text local debugging."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        service = getattr(record, "service", self.service_name)
        req_id = getattr(record, "request_id", get_request_id())
        base = f"{ts} [{record.levelname}] [{service}] [req={req_id}] {record.getMessage()}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def _safe_json_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _safe_json_value(v) for k, v in value.items()}
    return str(value)


def setup_logging(service_name: str, level: str = "INFO", json_logs: bool = True) -> logging.Logger:
    """Create or reconfigure a service-scoped logger without duplicating handlers."""
    logger = logging.getLogger(f"shopflow.{service_name}")
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric_level)
    logger.propagate = False

    logger.handlers.clear()
    logger.filters.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(numeric_level)
    handler.setFormatter(JSONFormatter(service_name) if json_logs else PlainFormatter(service_name))

    logger.addFilter(ServiceContextFilter(service_name))
    logger.addHandler(handler)
    return logger
