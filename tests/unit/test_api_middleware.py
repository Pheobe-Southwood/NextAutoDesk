"""Middleware tests: request id propagation, access logging, body-size limit."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from test_api_support import AUTH_HEADERS, make_config, make_env, serving

from nextautodesk.api.middleware import RequestContextMiddleware

ACCESS_LOGGER = "nextautodesk.api.access"


def test_request_id_header_is_set_on_every_response(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        for path in ("/healthz", "/", "/api/v1/info"):
            headers = AUTH_HEADERS if path.startswith("/api") else {}
            response = client.get(path, headers=headers)
            assert response.headers.get("x-request-id"), path


def test_access_log_contains_correlation_fields(tmp_path: Path, caplog: Any) -> None:
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    with serving(tmp_path) as (_env, client):
        response = client.get(
            "/api/v1/info",
            headers={**AUTH_HEADERS, "X-Request-ID": "access-log-1"},
        )

    records = [r for r in caplog.records if r.name == ACCESS_LOGGER]
    assert records, "no access log record was emitted"
    record = records[-1]
    assert record.request_id == "access-log-1"
    assert record.method == "GET"
    assert record.path == "/api/v1/info"
    assert record.status == 200
    assert record.duration_ms >= 0
    assert record.client
    assert record.status == response.status_code


def test_access_log_can_be_disabled(tmp_path: Path, caplog: Any) -> None:
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    with serving(tmp_path, access_log=False) as (_env, client):
        client.get("/api/v1/info", headers=AUTH_HEADERS)

    assert [r for r in caplog.records if r.name == ACCESS_LOGGER] == []


def test_body_limit_rejects_oversized_content_length(tmp_path: Path) -> None:
    with serving(tmp_path, max_body_bytes=1024) as (_env, client):
        payload = {"text": "x" * 4096}
        response = client.post("/api/v1/keyboard/type", headers=AUTH_HEADERS, json=payload)
        # The rejection happens in middleware, before authentication.
        unauthenticated = client.post("/api/v1/keyboard/type", json=payload)

    for result in (response, unauthenticated):
        assert result.status_code == 413
        error = result.json()["error"]
        assert error["code"] == "REQUEST_TOO_LARGE"
        assert error["details"]["limit"] == 1024
        assert error["request_id"] == result.headers["x-request-id"]
    assert response.json()["error"]["details"]["content_length"] > 1024


def test_body_limit_accepts_small_requests(tmp_path: Path) -> None:
    with serving(tmp_path, max_body_bytes=1024) as (_env, client):
        response = client.post(
            "/api/v1/keyboard/type", headers=AUTH_HEADERS, json={"text": "ok"}
        )

    assert response.status_code == 200


async def test_body_limit_applies_to_streamed_bodies(tmp_path: Path) -> None:
    """A client that omits Content-Length must still be cut off while streaming."""

    config = make_config(tmp_path, max_body_bytes=1024)

    async def inner_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = RequestContextMiddleware(inner_app, config=config)
    chunks: list[dict[str, Any]] = [
        {"type": "http.request", "body": b"x" * 800, "more_body": True},
        {"type": "http.request", "body": b"x" * 800, "more_body": False},
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return chunks.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/keyboard/type",
        "headers": [],
        "state": {},
        "client": ("127.0.0.1", 12345),
    }
    await middleware(scope, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 413
    body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    assert b"REQUEST_TOO_LARGE" in body


async def test_streamed_body_within_limit_passes(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_body_bytes=4096)
    observed: dict[str, Any] = {}

    async def inner_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        observed["total"] = total
        observed["request_id"] = scope["state"]["request_id"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = RequestContextMiddleware(inner_app, config=config)
    chunks: list[dict[str, Any]] = [
        {"type": "http.request", "body": b"a" * 100, "more_body": True},
        {"type": "http.request", "body": b"b" * 100, "more_body": False},
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return chunks.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/upload",
        "headers": [],
        "state": {},
        "client": ("127.0.0.1", 12345),
    }
    await middleware(scope, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 200
    assert observed["total"] == 200
    assert observed["request_id"]


def test_middleware_does_not_touch_non_http_scopes(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    calls: list[str] = []

    async def inner_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        calls.append(scope["type"])

    middleware = RequestContextMiddleware(inner_app, config=config)

    import asyncio

    async def run() -> None:
        async def receive() -> dict[str, Any]:
            return {"type": "lifespan.startup"}

        async def send(_message: dict[str, Any]) -> None:
            return None

        await middleware({"type": "lifespan"}, receive, send)

    asyncio.run(run())
    assert calls == ["lifespan"]


def test_make_env_helper_creates_context(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    try:
        assert env.context.service is env.service
        assert env.context.task_manager is env.manager
        assert env.config.auth.enabled is True
    finally:
        env.close()
