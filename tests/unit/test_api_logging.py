"""Structured logging configuration tests."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from test_api_support import make_config

from nextautodesk.logging_setup import (
    ConsoleFormatter,
    JsonFormatter,
    action_var,
    request_id_var,
    reset_logging,
    setup_logging,
    task_id_var,
)


def _flagged_handlers() -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_nextautodesk_handler", False)
    ]


def _read_log_lines(log_path: Path) -> list[dict]:
    assert log_path.is_file(), f"log file not created: {log_path}"
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    return [json.loads(line) for line in lines]


def test_json_logging_writes_rotating_file_and_stderr(tmp_path: Path) -> None:
    config = make_config(tmp_path, log_format="json")
    reset_logging()
    try:
        logger = setup_logging(config)
        logger.warning(
            "unit-test event",
            extra={
                "request_id": "rid-1",
                "task_id": "tid-1",
                "action": "mouse.move",
                "duration_ms": 1.5,
                "status": "ok",
            },
        )
        handlers = _flagged_handlers()
        assert len(handlers) == 2
        assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers)

        log_path = config.storage.logs_path / "nextautodesk.log"
        entries = _read_log_lines(log_path)
        event = next(item for item in entries if item["message"] == "unit-test event")
        assert event["level"] == "WARNING"
        assert event["logger"].startswith("nextautodesk")
        assert event["request_id"] == "rid-1"
        assert event["task_id"] == "tid-1"
        assert event["action"] == "mouse.move"
        assert event["duration_ms"] == 1.5
        assert event["status"] == "ok"
        assert event["ts"].endswith("Z")
    finally:
        reset_logging()


def test_contextvars_are_used_as_fallbacks(tmp_path: Path) -> None:
    config = make_config(tmp_path, log_format="json")
    reset_logging()
    request_token = request_id_var.set("ctx-request")
    task_token = task_id_var.set("ctx-task")
    action_token = action_var.set("keyboard.type")
    try:
        logger = setup_logging(config)
        logger.warning("contextual")
        entries = _read_log_lines(config.storage.logs_path / "nextautodesk.log")
        event = next(item for item in entries if item["message"] == "contextual")
        assert event["request_id"] == "ctx-request"
        assert event["task_id"] == "ctx-task"
        assert event["action"] == "keyboard.type"
    finally:
        request_id_var.reset(request_token)
        task_id_var.reset(task_token)
        action_var.reset(action_token)
        reset_logging()


def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    config = make_config(tmp_path, log_format="json")
    reset_logging()
    try:
        setup_logging(config)
        first = _flagged_handlers()
        setup_logging(config)
        second = _flagged_handlers()
        assert len(second) == len(first) == 2
        assert {id(h) for h in second} == {id(h) for h in first}
    finally:
        reset_logging()


def test_setup_logging_reconfigures_on_new_settings(tmp_path: Path) -> None:
    config = make_config(tmp_path, log_format="json")
    reset_logging()
    try:
        setup_logging(config)
        stricter = config.model_copy(
            update={"logging": config.logging.model_copy(update={"level": "ERROR"})}
        )
        setup_logging(stricter)
        assert len(_flagged_handlers()) == 2
        assert logging.getLogger().level == logging.ERROR
    finally:
        reset_logging()


def test_console_formatter_contains_correlation_ids() -> None:
    record = logging.LogRecord(
        name="nextautodesk.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    record.request_id = "r-1"
    record.task_id = "t-1"
    line = ConsoleFormatter().format(record)
    assert "hello world" in line
    assert "request_id=r-1" in line
    assert "task_id=t-1" in line


def test_json_formatter_handles_exceptions_and_non_serialisable_extras() -> None:
    record = logging.LogRecord(
        name="nextautodesk.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=(),
        exc_info=None,
    )
    record.custom = {1, 2, 3}
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "boom"
    assert payload["level"] == "ERROR"
    assert payload["custom"] == [1, 2, 3]


def test_uvicorn_loggers_propagate_to_root(tmp_path: Path) -> None:
    config = make_config(tmp_path, log_format="json")
    reset_logging()
    try:
        setup_logging(config)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            assert logger.handlers == []
            assert logger.propagate is True
    finally:
        reset_logging()
