"""Unified error model for the HTTP API and internal layers.

Every failure that reaches the HTTP layer is normalised to:

.. code-block:: json

    {
      "error": {
        "code": "TASK_NOT_FOUND",
        "message": "human readable message",
        "details": {...},
        "request_id": "..."
      }
    }

The ``code`` values are stable identifiers callers may branch on.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # Authentication / authorisation
    AUTH_MISSING = "AUTH_MISSING"
    AUTH_INVALID = "AUTH_INVALID"

    # Request validation
    VALIDATION_ERROR = "VALIDATION_ERROR"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    UNKNOWN_ACTION = "UNKNOWN_ACTION"
    UNKNOWN_OPERATION = "UNKNOWN_OPERATION"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    # Framework-level HTTP failures (raised by Starlette/FastAPI, not by domain code)
    BAD_REQUEST = "BAD_REQUEST"
    FORBIDDEN = "FORBIDDEN"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CONFLICT = "CONFLICT"
    TOO_MANY_REQUESTS = "TOO_MANY_REQUESTS"

    # Strategy layer
    STRATEGY_UNAVAILABLE = "STRATEGY_UNAVAILABLE"
    STRATEGY_NOT_SUPPORTED = "STRATEGY_NOT_SUPPORTED"
    STRATEGY_EXECUTION_FAILED = "STRATEGY_EXECUTION_FAILED"

    # Desktop / driver layer
    DESKTOP_UNAVAILABLE = "DESKTOP_UNAVAILABLE"
    DRIVER_UNAVAILABLE = "DRIVER_UNAVAILABLE"
    DRIVER_ERROR = "DRIVER_ERROR"
    ACTION_FAILED = "ACTION_FAILED"
    ACTION_TIMEOUT = "ACTION_TIMEOUT"
    WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
    WINDOW_ACTION_FAILED = "WINDOW_ACTION_FAILED"
    SCREENSHOT_FAILED = "SCREENSHOT_FAILED"
    SCREENSHOT_NOT_FOUND = "SCREENSHOT_NOT_FOUND"

    # Perception layer
    PERCEPTION_UNAVAILABLE = "PERCEPTION_UNAVAILABLE"
    LOCATOR_UNAVAILABLE = "LOCATOR_UNAVAILABLE"
    LOCATOR_NOT_SUPPORTED = "LOCATOR_NOT_SUPPORTED"
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    PERCEPTION_FAILED = "PERCEPTION_FAILED"

    # Task layer
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_NOT_CANCELLABLE = "TASK_NOT_CANCELLABLE"
    TASK_CANCELLED = "TASK_CANCELLED"
    TASK_TIMEOUT = "TASK_TIMEOUT"
    TASK_FAILED = "TASK_FAILED"
    QUEUE_FULL = "QUEUE_FULL"
    EXECUTOR_UNAVAILABLE = "EXECUTOR_UNAVAILABLE"

    # Infrastructure
    CONFIG_INVALID = "CONFIG_INVALID"
    STORAGE_ERROR = "STORAGE_ERROR"
    RELOAD_FAILED = "RELOAD_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    NOT_FOUND = "NOT_FOUND"


class NextAutoDeskError(Exception):
    """Base class for all domain errors.

    ``http_status`` is the status code used by the API layer when no more
    specific mapping is provided.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.request_id = request_id

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
        }
        if self.request_id:
            payload["request_id"] = self.request_id
        return payload


class ConfigError(NextAutoDeskError):
    code = ErrorCode.CONFIG_INVALID
    http_status = 500


class ReloadError(NextAutoDeskError):
    code = ErrorCode.RELOAD_FAILED
    http_status = 500


class AuthError(NextAutoDeskError):
    code = ErrorCode.AUTH_INVALID
    http_status = 401


class ValidationFailed(NextAutoDeskError):
    code = ErrorCode.VALIDATION_ERROR
    http_status = 422


class InvalidArgument(NextAutoDeskError):
    code = ErrorCode.INVALID_ARGUMENT
    http_status = 400


class UnknownAction(NextAutoDeskError):
    code = ErrorCode.UNKNOWN_ACTION
    http_status = 404


class StrategyUnavailable(NextAutoDeskError):
    code = ErrorCode.STRATEGY_UNAVAILABLE
    http_status = 409
    """Raised when a configured/requested provider cannot run (missing dependency,
    unsupported operation, or disabled)."""


class StrategyNotSupported(NextAutoDeskError):
    code = ErrorCode.STRATEGY_NOT_SUPPORTED
    http_status = 409


class StrategyExecutionFailed(NextAutoDeskError):
    code = ErrorCode.STRATEGY_EXECUTION_FAILED
    http_status = 502


class DesktopUnavailable(NextAutoDeskError):
    code = ErrorCode.DESKTOP_UNAVAILABLE
    http_status = 503


class DriverUnavailable(NextAutoDeskError):
    code = ErrorCode.DRIVER_UNAVAILABLE
    http_status = 503


class ActionFailed(NextAutoDeskError):
    code = ErrorCode.ACTION_FAILED
    http_status = 502


class ActionTimeout(NextAutoDeskError):
    code = ErrorCode.ACTION_TIMEOUT
    http_status = 504


class WindowNotFound(NextAutoDeskError):
    code = ErrorCode.WINDOW_NOT_FOUND
    http_status = 404


class WindowActionFailed(NextAutoDeskError):
    code = ErrorCode.WINDOW_ACTION_FAILED
    http_status = 502


class ScreenshotFailed(NextAutoDeskError):
    code = ErrorCode.SCREENSHOT_FAILED
    http_status = 502


class ScreenshotNotFound(NextAutoDeskError):
    code = ErrorCode.SCREENSHOT_NOT_FOUND
    http_status = 404


class PerceptionUnavailable(NextAutoDeskError):
    """Raised when the perception layer is disabled or not wired into the runtime."""

    code = ErrorCode.PERCEPTION_UNAVAILABLE
    http_status = 409


class LocatorUnavailable(NextAutoDeskError):
    """Raised when a locator is unknown, disabled, or its dependency is missing."""

    code = ErrorCode.LOCATOR_UNAVAILABLE
    http_status = 409


class LocatorNotSupported(NextAutoDeskError):
    """Raised when a locator cannot handle the requested query kind."""

    code = ErrorCode.LOCATOR_NOT_SUPPORTED
    http_status = 400


class ElementNotFound(NextAutoDeskError):
    """Raised when no element matches a query within the wait deadline."""

    code = ErrorCode.ELEMENT_NOT_FOUND
    http_status = 404


class PerceptionFailed(NextAutoDeskError):
    """Raised when screen capture, image matching, or OCR execution fails."""

    code = ErrorCode.PERCEPTION_FAILED
    http_status = 502


class TaskNotFound(NextAutoDeskError):
    code = ErrorCode.TASK_NOT_FOUND
    http_status = 404


class TaskNotCancellable(NextAutoDeskError):
    code = ErrorCode.TASK_NOT_CANCELLABLE
    http_status = 409


class TaskCancelled(NextAutoDeskError):
    """``BaseException``-free cancellation signal used for cooperative stops."""

    code = ErrorCode.TASK_CANCELLED
    http_status = 409


class TaskTimeoutError(NextAutoDeskError):
    code = ErrorCode.TASK_TIMEOUT
    http_status = 504


class TaskFailed(NextAutoDeskError):
    code = ErrorCode.TASK_FAILED
    http_status = 502


class QueueFull(NextAutoDeskError):
    code = ErrorCode.QUEUE_FULL
    http_status = 429


class ExecutorUnavailable(NextAutoDeskError):
    code = ErrorCode.EXECUTOR_UNAVAILABLE
    http_status = 503


class StorageError(NextAutoDeskError):
    code = ErrorCode.STORAGE_ERROR
    http_status = 500


__all__ = [name for name in dir() if name.endswith("Error") or name == "ErrorCode"]
