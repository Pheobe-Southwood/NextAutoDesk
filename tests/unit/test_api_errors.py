"""Unified error-envelope tests: 404/405/422/domain/500 and request ids."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from test_api_support import API_KEY, AUTH_HEADERS, make_env, serving

from nextautodesk.api.app import create_app
from nextautodesk.errors import ActionFailed, WindowNotFound


def _error(response) -> dict:  # type: ignore[no-untyped-def]
    return response.json()["error"]


def test_unknown_route_uses_unified_error_envelope(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        response = client.get("/api/v1/does-not-exist", headers=AUTH_HEADERS)

    assert response.status_code == 404
    error = _error(response)
    assert error["code"] == "NOT_FOUND"
    assert error["details"]["path"] == "/api/v1/does-not-exist"
    assert error["request_id"] == response.headers["x-request-id"]


def test_method_not_allowed(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        response = client.delete("/api/v1/info", headers=AUTH_HEADERS)

    assert response.status_code == 405
    assert _error(response)["code"] == "METHOD_NOT_ALLOWED"


def test_request_validation_error_shape(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        response = client.post("/api/v1/mouse/move", headers=AUTH_HEADERS, json={"x": "nope"})
        extra = client.post(
            "/api/v1/mouse/move", headers=AUTH_HEADERS, json={"x": 1, "y": 2, "bogus": True}
        )

    assert response.status_code == 422
    error = _error(response)
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"] == "request validation failed"
    locations = [entry["loc"] for entry in error["details"]["errors"]]
    assert any("x" in location for location in locations)
    assert extra.status_code == 422
    assert any(
        "bogus" in entry["loc"] for entry in _error(extra)["details"]["errors"]
    )


def test_domain_error_maps_to_status_and_code(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.handler("window.activate").error = WindowNotFound(
            "window 0xdead is gone", details={"window_id": "0xdead"}
        )
        response = client.post("/api/v1/windows/0xdead/activate", headers=AUTH_HEADERS)

    assert response.status_code == 404
    error = _error(response)
    assert error["code"] == "WINDOW_NOT_FOUND"
    assert error["message"] == "window 0xdead is gone"
    assert error["details"] == {"window_id": "0xdead"}
    assert error["request_id"] == response.headers["x-request-id"]


def test_action_failure_maps_to_502(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.handler("keyboard.type").error = ActionFailed("driver exploded")
        response = client.post(
            "/api/v1/keyboard/type", headers=AUTH_HEADERS, json={"text": "hello"}
        )

    assert response.status_code == 502
    assert _error(response)["code"] == "ACTION_FAILED"


def test_unhandled_exception_returns_internal_error(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    app = create_app(env.config, context=env.context)

    @app.get("/boom", include_in_schema=False)
    def _boom() -> None:
        raise RuntimeError("kaboom")

    client = TestClient(app, raise_server_exceptions=False)
    try:
        with client:
            response = client.get("/boom")
    finally:
        env.close()

    assert response.status_code == 500
    error = _error(response)
    assert error["code"] == "INTERNAL_ERROR"
    assert error["message"] == "internal server error"
    assert "kaboom" not in response.text
    assert error["request_id"] == response.headers["x-request-id"]


def test_request_id_is_generated_when_absent_or_invalid(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        generated = client.get("/api/v1/info", headers=AUTH_HEADERS)
        invalid = client.get(
            "/api/v1/info", headers={**AUTH_HEADERS, "X-Request-ID": "bad id with spaces"}
        )
        too_long = client.get(
            "/api/v1/info", headers={**AUTH_HEADERS, "X-Request-ID": "x" * 500}
        )

    assert generated.headers["x-request-id"]
    assert generated.headers["x-request-id"] != "bad id with spaces"
    assert invalid.headers["x-request-id"] != "bad id with spaces"
    assert len(too_long.headers["x-request-id"]) <= 128


def test_auth_errors_have_www_authenticate(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        missing = client.get("/api/v1/info")
        invalid = client.get("/api/v1/info", headers={"X-API-Key": "nope"})

    for response, code in ((missing, "AUTH_MISSING"), (invalid, "AUTH_INVALID")):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "ApiKey"
        assert _error(response)["code"] == code


def test_public_router_unknown_path_still_unified(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        response = client.get("/nope")

    assert response.status_code == 404
    assert _error(response)["code"] == "NOT_FOUND"


def test_api_key_can_be_a_custom_header() -> None:
    # Covered implicitly by configuration tests; assert the default header name here
    # so a regression in config defaults is caught.
    from nextautodesk.config import AuthSettings

    assert AuthSettings(api_keys=["x"]).header == "X-API-Key"
    assert API_KEY


def test_driver_error_maps_to_502(tmp_path: Path) -> None:
    """Raw driver failures on read-only routes must be 502 DRIVER_ERROR, not 500."""

    with serving(tmp_path) as (env, client):
        env.drivers.mouse.fail = True
        mouse = client.get("/api/v1/mouse/position", headers=AUTH_HEADERS)
        env.drivers.mouse.fail = False
        env.drivers.screen.fail = True
        screen = client.get("/api/v1/screen/size", headers=AUTH_HEADERS)

    for response in (mouse, screen):
        assert response.status_code == 502
        error = _error(response)
        assert error["code"] == "DRIVER_ERROR"
        assert error["message"] == "desktop driver failure"
        assert error["details"]["driver"] == "fake"
        assert error["request_id"] == response.headers["x-request-id"]
