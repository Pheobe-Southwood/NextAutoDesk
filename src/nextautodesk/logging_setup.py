"""Structured (JSON) logging configuration.

Every log line written by NextAutoDesk is a single JSON object so it can be
ingested by ``jq``, Loki, journald or any log pipeline without parsing rules.
The stable field set is:

``ts`` / ``level`` / ``logger`` / ``message`` / ``request_id`` / ``task_id`` /
``action`` / ``duration_ms`` / ``status``

``request_id`` and ``task_id`` are resolved from :mod:`contextvars` when a log
record does not provide them explicitly, so any code running inside an HTTP
request is automatically correlated with the access log. Extra keyword
arguments passed with ``logger.info(..., extra={...})`` are merged into the same
object (used for ``method``/``path``/``client`` in access logs).

``setup_logging`` is idempotent: calling it twice with the same settings is a
no-op, and calling it with new settings reconfigures the handlers in place
(used by configuration hot-reload).
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import sys
import threading
from datetime import UTC, datetime
from typing import Any

from .config import AppConfig

#: Current HTTP request id (set by the request middleware).
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nextautodesk_request_id", default=None
)
#: Current task id (set by the task runner / task routes).
task_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nextautodesk_task_id", default=None
)
#: Current action name (set by action routes / task runner).
action_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nextautodesk_action", default=None
)

LOGGER = logging.getLogger(__name__)

#: Attribute names that every ``LogRecord`` carries; they are never treated as extras.
_RESERVED_RECORD_ATTRS: frozenset[str] = frozenset(
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
        "message",
        "module",
        "msecs",
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
)

#: Marker attribute identifying handlers installed by this module.
_HANDLER_MARK = "_nextautodesk_handler"

_HANDLER_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"signature": None}


def current_request_id() -> str | None:
    """Request id of the current execution context (if any)."""

    return request_id_var.get()


def current_task_id() -> str | None:
    """Task id of the current execution context (if any)."""

    return task_id_var.get()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


def _utc_iso(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class JsonFormatter(logging.Formatter):
    """Render ``LogRecord`` objects as one-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _utc_iso(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": _first(
                getattr(record, "request_id", None), request_id_var.get()
            ),
            "task_id": _first(getattr(record, "task_id", None), task_id_var.get()),
            "action": _first(getattr(record, "action", None), action_var.get()),
            "duration_ms": getattr(record, "duration_ms", None),
            "status": getattr(record, "status", None),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_") or key in payload:
                continue
            payload[key] = _jsonable(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable formatter for local development (``logging.format=console``)."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = _utc_iso(record.created)
        request_id = _first(getattr(record, "request_id", None), request_id_var.get())
        task_id = _first(getattr(record, "task_id", None), task_id_var.get())
        action = _first(getattr(record, "action", None), action_var.get())
        parts = [
            timestamp,
            f"{record.levelname:<8}",
            f"{record.name}:",
            record.getMessage(),
        ]
        if request_id:
            parts.append(f"request_id={request_id}")
        if task_id:
            parts.append(f"task_id={task_id}")
        if action:
            parts.append(f"action={action}")
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_") or key in {
                "request_id",
                "task_id",
                "action",
                "duration_ms",
                "status",
            }:
                continue
            parts.append(f"{key}={value}")
        line = " ".join(parts)
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _remove_installed_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover - defensive cleanup
                LOGGER.debug("failed to close logging handler", exc_info=True)


def setup_logging(config: AppConfig) -> logging.Logger:
    """Configure process-wide structured logging (idempotent).

    A rotating file handler writes ``<logs_dir>/nextautodesk.log`` and a stderr
    handler keeps container logs working without a mounted volume. Uvicorn's own
    loggers are pointed at the root handlers so its startup/shutdown messages use
    the same format and are not duplicated.
    """

    level_name = config.logging.level.upper()
    level = getattr(logging, level_name, logging.INFO)
    log_format = config.logging.format
    log_path = config.storage.logs_path / "nextautodesk.log"
    signature = (
        level_name,
        log_format,
        str(log_path),
        config.logging.max_bytes,
        config.logging.backup_count,
    )

    with _HANDLER_LOCK:
        if _STATE["signature"] == signature:
            return LOGGER
        _remove_installed_handlers()
        root = logging.getLogger()
        root.setLevel(level)
        formatter: logging.Formatter = (
            JsonFormatter() if log_format == "json" else ConsoleFormatter()
        )

        stderr_handler = logging.StreamHandler(stream=sys.stderr)
        stderr_handler.setLevel(level)
        stderr_handler.setFormatter(formatter)
        setattr(stderr_handler, _HANDLER_MARK, True)
        if log_format == "json":
            # Keep uvicorn tracebacks readable inside the JSON "exc_info" field.
            stderr_handler.set_name("nextautodesk-stderr")
        root.addHandler(stderr_handler)

        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                str(log_path),
                maxBytes=config.logging.max_bytes,
                backupCount=config.logging.backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            setattr(file_handler, _HANDLER_MARK, True)
            file_handler.set_name("nextautodesk-file")
            root.addHandler(file_handler)
        except OSError as exc:  # pragma: no cover - unusual permission problem
            sys.stderr.write(
                f"nextautodesk: cannot open log file {log_path}: {exc}; "
                f"logging to stderr only\n"
            )

        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            uv_logger = logging.getLogger(name)
            uv_logger.handlers.clear()
            uv_logger.setLevel(level)
            uv_logger.propagate = True

        _STATE["signature"] = signature

    LOGGER.debug("structured logging configured (level=%s format=%s)", level_name, log_format)
    return LOGGER


def reset_logging() -> None:
    """Remove handlers installed by :func:`setup_logging` (test / shutdown helper)."""

    with _HANDLER_LOCK:
        _remove_installed_handlers()
        _STATE["signature"] = None


__all__ = [
    "ConsoleFormatter",
    "JsonFormatter",
    "action_var",
    "current_request_id",
    "current_task_id",
    "request_id_var",
    "reset_logging",
    "setup_logging",
    "task_id_var",
]
