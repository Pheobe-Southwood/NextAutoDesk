"""Exception handlers producing the unified error envelope.

Every failure returned by the HTTP API has the same shape::

    {"error": {"code": "...", "message": "...", "details": {...}, "request_id": "..."}}

``code`` values come from :class:`nextautodesk.errors.ErrorCode` so clients can
branch on stable identifiers. Validation errors carry the offending fields in
``details.errors``; unknown routes become ``NOT_FOUND``; unexpected failures are
logged and reported as ``INTERNAL_ERROR`` without leaking internals.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..drivers.base import DriverError
from ..errors import AuthError, ErrorCode, NextAutoDeskError

LOGGER = logging.getLogger(__name__)

#: HTTP status -> stable error code for framework-level failures.
_STATUS_CODES: dict[int, str] = {
    400: ErrorCode.BAD_REQUEST.value,
    401: ErrorCode.AUTH_INVALID.value,
    403: ErrorCode.FORBIDDEN.value,
    404: ErrorCode.NOT_FOUND.value,
    405: ErrorCode.METHOD_NOT_ALLOWED.value,
    409: ErrorCode.CONFLICT.value,
    413: ErrorCode.REQUEST_TOO_LARGE.value,
    422: ErrorCode.VALIDATION_ERROR.value,
    429: ErrorCode.TOO_MANY_REQUESTS.value,
}

#: Code used by the middleware for the "request body too large" rejection.
REQUEST_TOO_LARGE = ErrorCode.REQUEST_TOO_LARGE.value

#: Code used when a desktop driver fails below the action/strategy layer.
DRIVER_ERROR = ErrorCode.DRIVER_ERROR.value


def request_id_from_request(request: Request) -> str | None:
    """Request id set by the middleware (``None`` outside a request, e.g. unit tests)."""

    return getattr(request.state, "request_id", None)


def error_payload(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the JSON-serialisable unified error envelope."""

    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "details": details or {},
    }
    if request_id:
        payload["request_id"] = request_id
    return {"error": payload}


def json_error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Create a :class:`JSONResponse` carrying the unified error envelope."""

    return JSONResponse(
        status_code=status_code,
        content=error_payload(code, message, details, request_id),
        headers=headers,
    )


def _auth_headers(exc: NextAutoDeskError) -> dict[str, str] | None:
    if isinstance(exc, AuthError):
        from .security import WWW_AUTHENTICATE

        return {"WWW-Authenticate": WWW_AUTHENTICATE}
    return None


async def _domain_error_handler(request: Request, exc: NextAutoDeskError) -> JSONResponse:
    request_id = request_id_from_request(request) or exc.request_id
    payload = error_payload(
        exc.code.value,
        exc.message,
        exc.details,
        request_id,
    )
    status_code = exc.http_status
    if status_code >= 500:
        LOGGER.error(
            "request failed with domain error %s: %s (request_id=%s)",
            exc.code.value,
            exc.message,
            request_id,
        )
    else:
        LOGGER.info(
            "request rejected with domain error %s: %s (request_id=%s)",
            exc.code.value,
            exc.message,
            request_id,
        )
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers=_auth_headers(exc),
    )


async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    request_id = request_id_from_request(request)
    errors: list[dict[str, Any]] = []
    for item in exc.errors():
        errors.append(
            {
                "loc": [str(part) for part in item.get("loc", ())],
                "msg": item.get("msg", ""),
                "type": item.get("type", ""),
            }
        )
    LOGGER.info(
        "request validation failed (%d field error(s), request_id=%s)", len(errors), request_id
    )
    return json_error_response(
        422,
        ErrorCode.VALIDATION_ERROR.value,
        "request validation failed",
        details={"errors": errors},
        request_id=request_id,
    )


async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    request_id = request_id_from_request(request)
    code = _STATUS_CODES.get(exc.status_code, f"HTTP_{exc.status_code}")
    message = exc.detail if isinstance(exc.detail, str) and exc.detail else "request failed"
    if exc.status_code == 404:
        message = "resource not found"
    headers = dict(exc.headers or {})
    if exc.status_code == 401:
        from .security import WWW_AUTHENTICATE

        headers.setdefault("WWW-Authenticate", WWW_AUTHENTICATE)
    return json_error_response(
        exc.status_code,
        code,
        message,
        details={"path": request.url.path},
        request_id=request_id,
        headers=headers or None,
    )


async def _driver_error_handler(request: Request, exc: DriverError) -> JSONResponse:
    """Map raw driver failures (raised below the action layer) to 502."""

    request_id = request_id_from_request(request)
    details: dict[str, Any] = dict(exc.details or {})
    if exc.driver:
        details.setdefault("driver", exc.driver)
    LOGGER.warning(
        "desktop driver failure: %s (driver=%s, request_id=%s)",
        exc,
        exc.driver or "unknown",
        request_id,
    )
    return json_error_response(
        502,
        DRIVER_ERROR,
        "desktop driver failure",
        details=details or None,
        request_id=request_id,
    )


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = request_id_from_request(request)
    LOGGER.exception(
        "unhandled exception while processing %s %s (request_id=%s)",
        request.method,
        request.url.path,
        request_id,
    )
    return json_error_response(
        500,
        ErrorCode.INTERNAL_ERROR.value,
        "internal server error",
        request_id=request_id,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach all unified error handlers to ``app``."""

    app.add_exception_handler(DriverError, _driver_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(NextAutoDeskError, _domain_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_error_handler)


__all__ = [
    "DRIVER_ERROR",
    "REQUEST_TOO_LARGE",
    "error_payload",
    "json_error_response",
    "register_exception_handlers",
    "request_id_from_request",
]
