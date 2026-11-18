"""ASGI middleware: request id, structured access log and body-size guard.

Implemented as a pure ASGI middleware (not ``BaseHTTPMiddleware``) so that:

* the request id contextvar is visible to every log line produced while the
  request is handled (no extra task hop),
* the body size limit can be enforced while the body is being read, not after,

Responsibilities
----------------
1. Accept an incoming ``X-Request-ID`` (sanitised) or generate one; expose it on
   ``request.state.request_id`` and echo it in the response header.
2. Emit one JSON access log line per request when ``logging.access_log`` is on.
3. Reject request bodies larger than ``server.max_request_body_bytes`` with a
   unified ``413 REQUEST_TOO_LARGE`` error.
4. Enforce ``auth.protect_docs`` for the documentation endpoints, which are
   registered by FastAPI before routers exist.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from http import HTTPStatus
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import AppConfig
from ..errors import ErrorCode
from ..logging_setup import action_var, request_id_var, task_id_var
from .errors_handlers import REQUEST_TOO_LARGE, json_error_response
from .security import WWW_AUTHENTICATE, check_api_key

LOGGER = logging.getLogger("nextautodesk.api.access")

_REQUEST_ID_HEADER = "x-request-id"
_MAX_REQUEST_ID_LENGTH = 128
_VALID_REQUEST_ID = re.compile(r"^[\w.:@+/=-]+$")


class _BodyTooLarge(Exception):
    """Raised by the wrapped ``receive`` when the client streams too much data."""

    def __init__(self, received: int, limit: int) -> None:
        super().__init__(f"request body exceeds {limit} bytes")
        self.received = received
        self.limit = limit


def _client_repr(scope: Scope) -> str | None:
    client = scope.get("client")
    if not client:
        return None
    host, port = client[0], client[1] if len(client) > 1 else None
    return f"{host}:{port}" if port is not None else str(host)


class RequestContextMiddleware:
    """Request correlation + access logging + request size enforcement."""

    def __init__(self, app: ASGIApp, *, config: AppConfig) -> None:
        self.app = app
        self.config = config

    # -- helpers -----------------------------------------------------------
    def _config_for(self, scope: Scope) -> AppConfig:
        """Prefer the live context configuration so reloads take effect immediately."""

        app = scope.get("app")
        context = getattr(getattr(app, "state", None), "context", None)
        return getattr(context, "config", None) or self.config

    @staticmethod
    def _request_id(scope: Scope) -> str:
        headers = Headers(scope=scope)
        incoming = (headers.get(_REQUEST_ID_HEADER) or "").strip()
        if (
            incoming
            and len(incoming) <= _MAX_REQUEST_ID_LENGTH
            and _VALID_REQUEST_ID.match(incoming)
        ):
            return incoming
        return uuid.uuid4().hex

    def _protected_docs_paths(self, config: AppConfig) -> set[str]:
        paths: set[str] = set()
        if not (config.auth.enabled and config.auth.protect_docs):
            return paths
        if config.server.docs_url:
            paths.add(config.server.docs_url)
            paths.add(f"{config.server.docs_url.rstrip('/')}/oauth2-redirect")
        if config.server.openapi_url:
            paths.add(config.server.openapi_url)
        return paths

    async def _send_error(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None,
        request_id: str,
        headers: dict[str, str] | None = None,
    ) -> int:
        extra_headers = {"X-Request-ID": request_id}
        if headers:
            extra_headers.update(headers)
        response = json_error_response(
            status_code,
            code,
            message,
            details=details,
            request_id=request_id,
            headers=extra_headers,
        )
        await response(scope, receive, send)
        return status_code

    def _access_log(
        self,
        scope: Scope,
        *,
        request_id: str,
        status_code: int,
        started: float,
        extra: dict[str, Any] | None = None,
    ) -> None:
        duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
        client = _client_repr(scope)
        log_extra: dict[str, Any] = {
            "request_id": request_id,
            "method": scope.get("method"),
            "path": scope.get("path"),
            "status": status_code,
            "duration_ms": duration_ms,
            "client": client,
        }
        if scope.get("query_string"):
            log_extra["query"] = scope["query_string"].decode("latin-1")
        if extra:
            log_extra.update({key: value for key, value in extra.items() if value is not None})
        LOGGER.info(
            "%s %s %s",
            scope.get("method", "?"),
            scope.get("path", "?"),
            status_code,
            extra=log_extra,
        )

    # -- ASGI entry point --------------------------------------------------
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        config = self._config_for(scope)
        request_id = self._request_id(scope)
        state: dict[str, Any] = scope.setdefault("state", {})
        state["request_id"] = request_id
        tokens = (
            request_id_var.set(request_id),
            task_id_var.set(None),
            action_var.set(None),
        )
        started = time.perf_counter()
        status_code = 500
        response_started = False
        logged = False

        def finish(extra: dict[str, Any] | None = None) -> None:
            nonlocal logged
            if logged:
                return
            logged = True
            if config.logging.access_log:
                self._access_log(
                    scope, request_id=request_id, status_code=status_code, started=started, extra=extra
                )

        try:
            # 1) documentation protection (auth.protect_docs)
            protected = self._protected_docs_paths(config)
            if protected and scope.get("path") in protected:
                error = check_api_key(Headers(scope=scope), config)
                if error is not None:
                    status_code = await self._send_error(
                        scope,
                        receive,
                        send,
                        status_code=error.http_status,
                        code=error.code.value,
                        message=error.message,
                        details=error.details,
                        request_id=request_id,
                        headers={"WWW-Authenticate": WWW_AUTHENTICATE},
                    )
                    finish({"error": error.code.value})
                    return

            # 2) body size limit (Content-Length fast path)
            limit = config.server.max_request_body_bytes
            headers = Headers(scope=scope)
            content_length = headers.get("content-length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except ValueError:
                    declared = -1
                if declared > limit:
                    status_code = await self._send_error(
                        scope,
                        receive,
                        send,
                        status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        code=REQUEST_TOO_LARGE,
                        message=f"request body exceeds the {limit} byte limit",
                        details={"limit": limit, "content_length": declared},
                        request_id=request_id,
                    )
                    finish({"error": REQUEST_TOO_LARGE})
                    return

            # 3) streaming guard for chunked / lying clients
            received = 0

            async def limited_receive() -> Message:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > limit:
                        raise _BodyTooLarge(received, limit)
                return message

            async def send_wrapper(message: Message) -> None:
                nonlocal status_code, response_started
                if message["type"] == "http.response.start":
                    status_code = int(message["status"])
                    response_started = True
                    response_headers = MutableHeaders(scope=message)
                    response_headers.setdefault("X-Request-ID", request_id)
                await send(message)

            try:
                await self.app(scope, limited_receive, send_wrapper)
            except _BodyTooLarge as exc:
                if response_started:  # pragma: no cover - defensive
                    raise
                status_code = await self._send_error(
                    scope,
                    receive,
                    send,
                    status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    code=REQUEST_TOO_LARGE,
                    message=f"request body exceeds the {limit} byte limit",
                    details={"limit": limit, "received": exc.received},
                    request_id=request_id,
                )
                finish({"error": REQUEST_TOO_LARGE})
                return
            except Exception:
                if response_started:  # pragma: no cover - headers already sent
                    raise
                LOGGER.exception(
                    "unhandled exception while processing %s %s (request_id=%s)",
                    scope.get("method"),
                    scope.get("path"),
                    request_id,
                )
                status_code = await self._send_error(
                    scope,
                    receive,
                    send,
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                    code=ErrorCode.INTERNAL_ERROR.value,
                    message="internal server error",
                    details=None,
                    request_id=request_id,
                )
                finish({"error": ErrorCode.INTERNAL_ERROR.value})
                return
        finally:
            if not logged:
                finish()
            request_id_var.reset(tokens[0])
            task_id_var.reset(tokens[1])
            action_var.reset(tokens[2])


__all__ = ["RequestContextMiddleware"]
