"""Route-level smoke tests with the fake engine (headless, no X server)."""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from test_api_support import (
    AUTH_HEADERS,
    FAKE_ACTION_NAMES,
    PNG_BYTES,
    make_env,
    serving,
)

from nextautodesk.api.app import create_app
from nextautodesk.logging_setup import reset_logging
from nextautodesk.models import Point


def _wait_terminal(env, task_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = env.task_store.get(task_id)
        if record is not None and record.is_terminal:
            return record.model_dump(mode="json")
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not reach a terminal state within {timeout}s")


def _write_reload_overlay(
    tmp_path: Path,
    *,
    data_dir: Path | None = None,
    extra_lines: list[str] | None = None,
) -> Path:
    overlay = tmp_path / "overlay.yml"
    overlay.write_text(
        "\n".join(
            [
                "auth:",
                "  enabled: true",
                "  api_keys: ['test-key']",
                "desktop:",
                "  width: 1280",
                "  height: 720",
                "storage:",
                f"  data_dir: '{data_dir or (tmp_path / 'data')}'",
                "logging:",
                "  level: WARNING",
                *(extra_lines or []),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return overlay

# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------
def test_info_actions_strategies_and_config(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        info = client.get("/api/v1/info", headers=AUTH_HEADERS)
        actions = client.get("/api/v1/actions", headers=AUTH_HEADERS)
        strategies = client.get("/api/v1/strategies", headers=AUTH_HEADERS)
        config = client.get("/api/v1/config", headers=AUTH_HEADERS)

    assert info.status_code == 200
    info_body = info.json()
    assert info_body["name"] == "nextautodesk"
    assert info_body["api_version"] == "v1"
    assert info_body["screen"] == {"width": 1280, "height": 720}
    assert "mouse.move" in info_body["actions"]
    assert info_body["uptime_s"] >= 0

    assert actions.status_code == 200
    action_body = actions.json()
    assert action_body["count"] == len(action_body["actions"])
    names = {item["name"] for item in action_body["actions"]}
    assert {"mouse.move", "keyboard.type", "screen.screenshot"}.issubset(names)
    move_entry = next(item for item in action_body["actions"] if item["name"] == "mouse.move")
    assert move_entry["params_schema"]["type"] == "object"

    assert strategies.status_code == 200
    strategy_body = strategies.json()
    assert len(strategy_body["operations"]) == 19
    assert {provider["name"] for provider in strategy_body["providers"]} == {
        "direct",
        "human_movement",
    }

    assert config.status_code == 200
    config_body = config.json()
    assert config_body["auth"]["api_keys"] == ["***"]
    assert config_body["server"]["port"] == 8765


def test_openapi_schema_is_valid(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        schema = client.get("/openapi.json")
        docs = client.get("/docs")

    assert schema.status_code == 200
    payload = schema.json()
    assert payload["info"]["title"] == "NextAutoDesk"
    assert "/api/v1/mouse/move" in payload["paths"]
    assert "/api/v1/tasks" in payload["paths"]
    assert "/healthz" in payload["paths"]
    assert docs.status_code == 200


def test_config_reload_applies_hot_reloadable_settings(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    overlay = tmp_path / "overlay.yml"
    overlay.write_text(
        "\n".join(
            [
                "auth:",
                "  enabled: true",
                "  api_keys: ['test-key']",
                "desktop:",
                "  width: 1280",
                "  height: 720",
                "storage:",
                f"  data_dir: '{data_dir}'",
                "logging:",
                "  level: ERROR",
                "",
            ]
        ),
        encoding="utf-8",
    )
    with serving(tmp_path) as (env, client):
        env.config.source_path = str(overlay)
        try:
            response = client.post("/api/v1/config/reload", headers=AUTH_HEADERS)
            still_authorised = client.get("/api/v1/info", headers=AUTH_HEADERS)
        finally:
            reset_logging()

    assert response.status_code == 200
    body = response.json()
    assert body["reloaded"] is True
    assert body["config"]["logging"]["level"] == "ERROR"
    assert body["config"]["auth"]["api_keys"] == ["***"]
    assert env.context.config.logging.level == "ERROR"
    # task settings and logging are hot-reloadable per the architecture contract
    assert env.manager.config.logging.level == "ERROR"
    assert env.task_store.config.logging.level == "ERROR"
    assert logging.getLogger().level == logging.ERROR
    assert still_authorised.status_code == 200


def test_config_reload_failure_is_reported_as_reload_failed(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yml"
    bad.write_text("auth: [this, is, not, a, mapping]\n", encoding="utf-8")
    with serving(tmp_path) as (env, client):
        env.config.source_path = str(bad)
        response = client.post("/api/v1/config/reload", headers=AUTH_HEADERS)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "RELOAD_FAILED"
    assert error["details"]["path"] == str(bad)


# ---------------------------------------------------------------------------
# mouse / keyboard / desktop
# ---------------------------------------------------------------------------
def test_mouse_routes(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        move = client.post("/api/v1/mouse/move", headers=AUTH_HEADERS, json={"x": 10, "y": 20})
        click = client.post(
            "/api/v1/mouse/click",
            headers=AUTH_HEADERS,
            json={"x": 10, "y": 20, "button": "right", "clicks": 2},
        )
        down = client.post("/api/v1/mouse/down", headers=AUTH_HEADERS, json={"button": "middle"})
        up = client.post("/api/v1/mouse/up", headers=AUTH_HEADERS, json={"button": "middle"})
        hold = client.post(
            "/api/v1/mouse/hold", headers=AUTH_HEADERS, json={"button": "left", "hold_ms": 50}
        )
        drag = client.post(
            "/api/v1/mouse/drag",
            headers=AUTH_HEADERS,
            json={"to_x": 100, "to_y": 200, "button": "left"},
        )
        scroll = client.post(
            "/api/v1/mouse/scroll", headers=AUTH_HEADERS, json={"dx": 0, "dy": 3}
        )
        env.drivers.mouse.point = Point(x=77, y=88)
        env.drivers.mouse.buttons = {"left"}
        position = client.get("/api/v1/mouse/position", headers=AUTH_HEADERS)
        state = client.get("/api/v1/mouse/state", headers=AUTH_HEADERS)

    for response, action in (
        (move, "mouse.move"),
        (click, "mouse.click"),
        (down, "mouse.down"),
        (up, "mouse.up"),
        (hold, "mouse.hold"),
        (drag, "mouse.drag"),
        (scroll, "mouse.scroll"),
    ):
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["action"] == action
        assert body["success"] is True
        assert body["duration_ms"] >= 0

    assert position.status_code == 200
    assert position.json() == {"x": 77.0, "y": 88.0}
    assert state.status_code == 200
    assert state.json() == {"position": {"x": 77.0, "y": 88.0}, "buttons": ["left"]}
    assert env.recorder.params_for("mouse.move")[-1] == {"x": 10.0, "y": 20.0}
    assert env.recorder.params_for("mouse.click")[-1]["button"] == "right"
    assert env.recorder.params_for("mouse.scroll")[-1] == {"dx": 0, "dy": 3}


def test_keyboard_routes(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        typed = client.post(
            "/api/v1/keyboard/type", headers=AUTH_HEADERS, json={"text": "héllo 中文"}
        )
        press = client.post("/api/v1/keyboard/press", headers=AUTH_HEADERS, json={"key": "enter"})
        hotkey = client.post(
            "/api/v1/keyboard/hotkey", headers=AUTH_HEADERS, json={"keys": ["ctrl", "c"]}
        )
        sequence = client.post(
            "/api/v1/keyboard/sequence", headers=AUTH_HEADERS, json={"keys": ["tab", "enter"]}
        )
        key_down = client.post(
            "/api/v1/keyboard/key/down", headers=AUTH_HEADERS, json={"key": "shift"}
        )
        key_up = client.post("/api/v1/keyboard/key/up", headers=AUTH_HEADERS, json={"key": "shift"})
        hold = client.post(
            "/api/v1/keyboard/hold", headers=AUTH_HEADERS, json={"key": "a", "hold_ms": 20}
        )

    assert typed.status_code == 200
    assert env.recorder.params_for("keyboard.type")[-1] == {"text": "héllo 中文"}
    assert press.json()["action"] == "keyboard.press"
    assert hotkey.json()["action"] == "keyboard.hotkey"
    assert sequence.json()["action"] == "keyboard.sequence"
    assert key_down.json()["action"] == "keyboard.key_down"
    assert key_up.json()["action"] == "keyboard.key_up"
    assert hold.json()["action"] == "keyboard.hold"
    assert env.recorder.params_for("keyboard.hotkey")[-1] == {"keys": ["ctrl", "c"]}


def test_desktop_wait_route(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        response = client.post("/api/v1/desktop/wait", headers=AUTH_HEADERS, json={"ms": 5})

    assert response.status_code == 200
    assert response.json()["action"] == "desktop.wait"
    assert env.recorder.params_for("desktop.wait")[-1] == {"ms": 5}


def test_invalid_action_body_rejected(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        negative_wait = client.post(
            "/api/v1/desktop/wait", headers=AUTH_HEADERS, json={"ms": -1}
        )
        missing_click_target = client.post(
            "/api/v1/keyboard/press", headers=AUTH_HEADERS, json={}
        )
        unknown_field = client.post(
            "/api/v1/mouse/hold", headers=AUTH_HEADERS, json={"button": "left", "nope": 1}
        )

    assert negative_wait.status_code == 422
    assert missing_click_target.status_code == 422
    assert unknown_field.status_code == 422


# ---------------------------------------------------------------------------
# screen
# ---------------------------------------------------------------------------
def test_screen_size_and_screenshot_image(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        size = client.get("/api/v1/screen/size", headers=AUTH_HEADERS)
        png = client.get("/api/v1/screen/screenshot", headers=AUTH_HEADERS)
        jpeg = client.get(
            "/api/v1/screen/screenshot",
            headers=AUTH_HEADERS,
            params={"region": "0,0,10,20", "format": "jpeg"},
        )
        invalid_region = client.get(
            "/api/v1/screen/screenshot", headers=AUTH_HEADERS, params={"region": "not-a-region"}
        )
        out_of_bounds = client.get(
            "/api/v1/screen/screenshot", headers=AUTH_HEADERS, params={"region": "0,0,0,10"}
        )

    assert size.status_code == 200
    assert size.json() == {"width": 1280, "height": 720}
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"
    assert png.content == PNG_BYTES
    assert jpeg.status_code == 200
    assert jpeg.headers["content-type"] == "image/jpeg"
    region_call = [call for call in env.drivers.screen.calls if call[0] == "screenshot"]
    assert region_call[-1][1].width == 10
    assert region_call[-1][2] == "jpeg"
    assert invalid_region.status_code == 400
    assert invalid_region.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert out_of_bounds.status_code == 400


def test_screenshot_json_and_saved_file_crud(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.handler("screen.screenshot").output = {
            "format": "png",
            "width": 1280,
            "height": 720,
            "size_bytes": len(PNG_BYTES),
            "image_base64": base64.b64encode(PNG_BYTES).decode(),
        }
        metadata = client.post(
            "/api/v1/screen/screenshot", headers=AUTH_HEADERS, json={"format": "png"}
        )
        inline = client.post(
            "/api/v1/screen/screenshot",
            headers=AUTH_HEADERS,
            json={"format": "png", "inline": True},
        )
        # Create a file directly through the screenshot store, then exercise CRUD.
        env.service.store.screenshots.save(PNG_BYTES, fmt="png", name="saved-shot.png")
        listing = client.get("/api/v1/screen/screenshots", headers=AUTH_HEADERS)
        fetched = client.get("/api/v1/screen/screenshots/saved-shot.png", headers=AUTH_HEADERS)
        deleted = client.delete("/api/v1/screen/screenshots/saved-shot.png", headers=AUTH_HEADERS)
        missing = client.get("/api/v1/screen/screenshots/saved-shot.png", headers=AUTH_HEADERS)
        traversal = client.get(
            "/api/v1/screen/screenshots/..%2F..%2Fetc%2Fpasswd", headers=AUTH_HEADERS
        )
        bad_name = client.get("/api/v1/screen/screenshots/bad%20name.png", headers=AUTH_HEADERS)

    assert metadata.status_code == 200
    assert "image_base64" not in metadata.json()
    assert metadata.json()["width"] == 1280
    assert inline.status_code == 200
    assert base64.b64decode(inline.json()["image_base64"]) == PNG_BYTES

    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] >= 1
    assert any(item["name"] == "saved-shot.png" for item in body["items"])
    assert body["limit"] == 50 and body["offset"] == 0

    assert fetched.status_code == 200
    assert fetched.content == PNG_BYTES
    assert fetched.headers["content-type"] == "image/png"
    assert deleted.status_code == 204
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "SCREENSHOT_NOT_FOUND"
    assert traversal.status_code == 404
    assert bad_name.status_code == 422


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------
def test_windows_queries(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        listing = client.get("/api/v1/windows", headers=AUTH_HEADERS)
        active = client.get("/api/v1/windows/active", headers=AUTH_HEADERS)
        by_id = client.get("/api/v1/windows/0x3a00008", headers=AUTH_HEADERS)
        missing = client.get("/api/v1/windows/0xdead", headers=AUTH_HEADERS)
        invalid = client.get("/api/v1/windows/not-an-id", headers=AUTH_HEADERS)

    assert listing.status_code == 200
    assert listing.json()["count"] == 2
    assert len(listing.json()["windows"]) == 2
    assert active.status_code == 200
    assert active.json()["active"] is True
    assert by_id.status_code == 200
    assert by_id.json()["title"] == "Editor"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "WINDOW_NOT_FOUND"
    assert invalid.status_code == 422


def test_window_mutations_are_routed_through_actions(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        activate = client.post("/api/v1/windows/0x3a00008/activate", headers=AUTH_HEADERS)
        move = client.post(
            "/api/v1/windows/0x3a00008/move", headers=AUTH_HEADERS, json={"x": 11, "y": 22}
        )
        resize = client.post(
            "/api/v1/windows/0x3a00008/resize",
            headers=AUTH_HEADERS,
            json={"width": 300, "height": 200},
        )
        minimize = client.post("/api/v1/windows/0x3a00008/minimize", headers=AUTH_HEADERS)
        maximize = client.post("/api/v1/windows/0x3a00008/maximize", headers=AUTH_HEADERS)
        restore = client.post("/api/v1/windows/0x3a00008/restore", headers=AUTH_HEADERS)
        close = client.post("/api/v1/windows/0x3a00008/close", headers=AUTH_HEADERS)

    for response, action in (
        (activate, "window.activate"),
        (move, "window.move"),
        (resize, "window.resize"),
        (minimize, "window.minimize"),
        (maximize, "window.maximize"),
        (restore, "window.restore"),
        (close, "window.close"),
    ):
        assert response.status_code == 200, response.text
        assert response.json()["action"] == action

    assert env.recorder.params_for("window.move")[-1] == {
        "id": "0x3a00008",
        "x": 11,
        "y": 22,
    }
    assert env.recorder.params_for("window.resize")[-1] == {
        "id": "0x3a00008",
        "width": 300,
        "height": 200,
    }


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------
TASK_STEPS = [
    {"action": "mouse.move", "params": {"x": 5, "y": 6}, "name": "move"},
    {"action": "keyboard.type", "params": {"text": "hi"}, "delay_after_ms": 1},
]


def test_task_lifecycle_and_pagination(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        submit = client.post(
            "/api/v1/tasks",
            headers=AUTH_HEADERS,
            json={
                "name": "demo",
                "steps": TASK_STEPS,
                "metadata": {"origin": "unit-test"},
                "client_request_id": "req-1",
            },
        )
        assert submit.status_code == 202
        created = submit.json()
        task_id = created["id"]
        assert created["status"] in {"queued", "running"}
        assert created["name"] == "demo"
        assert created["metadata"] == {"origin": "unit-test"}
        assert created["client_request_id"] == "req-1"
        assert len(created["steps"]) == 2

        final = _wait_terminal(env, task_id)
        assert final["status"] == "succeeded", final
        assert final["started_at"] and final["finished_at"]
        assert [step["status"] for step in final["step_results"]] == ["succeeded", "succeeded"]
        assert final["step_results"][0]["action"] == "mouse.move"

        fetched = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH_HEADERS)
        listing = client.get("/api/v1/tasks", headers=AUTH_HEADERS)
        filtered = client.get(
            "/api/v1/tasks", headers=AUTH_HEADERS, params={"status": "succeeded"}
        )
        paged = client.get(
            "/api/v1/tasks", headers=AUTH_HEADERS, params={"limit": 1, "offset": 0}
        )
        invalid_status = client.get(
            "/api/v1/tasks", headers=AUTH_HEADERS, params={"status": "nope"}
        )

    assert fetched.status_code == 200
    assert fetched.json()["id"] == task_id
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1
    assert listing.json()["limit"] == 50
    assert filtered.json()["total"] >= 1
    assert paged.json()["limit"] == 1
    assert len(paged.json()["items"]) == 1
    assert invalid_status.status_code == 422


def test_task_validation_and_errors(tmp_path: Path) -> None:
    with serving(tmp_path, max_steps=2) as (_env, client):
        unknown_action = client.post(
            "/api/v1/tasks",
            headers=AUTH_HEADERS,
            json={"steps": [{"action": "does.not.exist", "params": {}}]},
        )
        too_many = client.post(
            "/api/v1/tasks",
            headers=AUTH_HEADERS,
            json={
                "steps": [
                    {"action": "mouse.move", "params": {"x": 1, "y": 1}},
                    {"action": "mouse.move", "params": {"x": 2, "y": 2}},
                    {"action": "mouse.move", "params": {"x": 3, "y": 3}},
                ]
            },
        )
        empty = client.post("/api/v1/tasks", headers=AUTH_HEADERS, json={"steps": []})
        not_found = client.get("/api/v1/tasks/unknown-task", headers=AUTH_HEADERS)
        cancel_not_found = client.post(
            "/api/v1/tasks/unknown-task/cancel", headers=AUTH_HEADERS
        )

    assert unknown_action.status_code == 404
    assert unknown_action.json()["error"]["code"] == "UNKNOWN_ACTION"
    assert too_many.status_code == 422
    assert too_many.json()["error"]["code"] == "VALIDATION_ERROR"
    assert empty.status_code == 422
    assert not_found.status_code == 404
    assert not_found.json()["error"]["code"] == "TASK_NOT_FOUND"
    assert cancel_not_found.status_code == 404


def test_task_cancel_finished_task_conflicts(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        submit = client.post(
            "/api/v1/tasks",
            headers=AUTH_HEADERS,
            json={"steps": [{"action": "mouse.move", "params": {"x": 1, "y": 2}}]},
        )
        task_id = submit.json()["id"]
        _wait_terminal(env, task_id)
        response = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=AUTH_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TASK_NOT_CANCELLABLE"


def test_tasks_endpoint_returns_503_without_task_manager(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.context.task_manager = None
    app = create_app(env.config, context=env.context)
    client = TestClient(app)
    try:
        with client:
            response = client.post(
                "/api/v1/tasks",
                headers=AUTH_HEADERS,
                json={"steps": [{"action": "mouse.move", "params": {"x": 1, "y": 1}}]},
            )
            listing = client.get("/api/v1/tasks", headers=AUTH_HEADERS)
    finally:
        env.close()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "EXECUTOR_UNAVAILABLE"
    assert listing.status_code == 503


# ---------------------------------------------------------------------------
# contract tests against the real action layer (no X required: only schemas)
# ---------------------------------------------------------------------------
def test_real_action_catalogue_matches_api_contract() -> None:
    """The real registry must expose the same names our routes/fake engine use."""

    from nextautodesk.actions import default_action_registry

    registry = default_action_registry()
    real_names = set(registry.names)
    assert len(real_names) == 35
    assert real_names == set(FAKE_ACTION_NAMES)
    # every catalogued action carries a parameter JSON schema for docs
    for entry in registry.catalog():
        assert entry.params_schema.get("type") == "object"


def test_route_request_schemas_match_real_action_params_models() -> None:
    """HTTP payloads must validate against the authoritative action param models."""

    from nextautodesk.actions import default_action_registry
    from nextautodesk.api.schemas import (
        DesktopWaitRequest,
        KeyboardHoldRequest,
        KeyboardHotkeyRequest,
        KeyboardKeyRequest,
        KeyboardPressRequest,
        KeyboardSequenceRequest,
        KeyboardTypeRequest,
        MouseButtonRequest,
        MouseClickRequest,
        MouseDragRequest,
        MouseHoldRequest,
        MouseMoveRelativeRequest,
        MouseMoveRequest,
        MouseScrollRequest,
        ScreenshotRequest,
        WindowMoveRequest,
        WindowResizeRequest,
    )
    from nextautodesk.api.schemas_perception import (
        ElementFindRequest,
        ElementOcrRequest,
        ElementWaitRequest,
        InteractionClickRequest,
        InteractionTypeRequest,
    )

    element_query = {
        "locator": "text",
        "text": {"text": "Submit", "match": "contains"},
    }

    payloads = {
        "mouse.move": MouseMoveRequest(x=10, y=20, duration_ms=5),
        "mouse.move_relative": MouseMoveRelativeRequest(dx=4, dy=-2),
        "mouse.click": MouseClickRequest(x=1, y=2, button="right", clicks=2, interval_ms=10),
        "mouse.down": MouseButtonRequest(button="left", x=1, y=2),
        "mouse.up": MouseButtonRequest(button="middle"),
        "mouse.hold": MouseHoldRequest(button="left", hold_ms=50),
        "mouse.drag": MouseDragRequest(to_x=100, to_y=200, button="left", duration_ms=10),
        "mouse.scroll": MouseScrollRequest(dx=1, dy=-2, x=3, y=4),
        "keyboard.type": KeyboardTypeRequest(text="hello 中文"),
        "keyboard.press": KeyboardPressRequest(key="enter"),
        "keyboard.hotkey": KeyboardHotkeyRequest(keys=["ctrl", "c"]),
        "keyboard.sequence": KeyboardSequenceRequest(keys=["tab", "enter"]),
        "keyboard.key_down": KeyboardKeyRequest(key="shift"),
        "keyboard.key_up": KeyboardKeyRequest(key="shift"),
        "keyboard.hold": KeyboardHoldRequest(key="a", hold_ms=20),
        "desktop.wait": DesktopWaitRequest(ms=10),
        "screen.screenshot": ScreenshotRequest(format="jpeg", save=True, name="x.jpg"),
        "element.find": ElementFindRequest(query=element_query),
        "element.wait": ElementWaitRequest(query=element_query, timeout_ms=5000),
        "element.ocr": ElementOcrRequest(region={"x": 0, "y": 0, "width": 10, "height": 10}),
        "interaction.click": InteractionClickRequest(query=element_query, wait_timeout_ms=1000),
        "interaction.type": InteractionTypeRequest(
            query=element_query, text="hello", clear=True, submit=True
        ),
    }
    registry = default_action_registry()
    for action, payload in payloads.items():
        handler = registry.get(action)
        params = payload.model_dump(exclude_none=True)
        params.pop("strategy", None)
        params.pop("timeout_ms", None)
        params.pop("inline", None)
        handler.parse_params(params)  # raises ValidationFailed on contract drift

    # window actions are addressed by path id + a small body
    window_payloads = {
        "window.activate": {"id": "0x3a00007"},
        "window.move": {"id": "0x3a00007", **WindowMoveRequest(x=1, y=2).model_dump()},
        "window.resize": {"id": "0x3a00007", **WindowResizeRequest(width=300, height=200).model_dump()},
        "window.minimize": {"id": "0x3a00007"},
        "window.maximize": {"id": "0x3a00007"},
        "window.restore": {"id": "0x3a00007"},
        "window.close": {"id": "0x3a00007"},
        "window.get": {"id": "0x3a00007"},
    }
    for action, params in window_payloads.items():
        registry.get(action).parse_params(params)


def test_config_reload_rejects_app_construction_fields(tmp_path: Path) -> None:
    overlay = _write_reload_overlay(
        tmp_path,
        extra_lines=["server:", "  port: 9999", "  docs_url: null"],
    )
    with serving(tmp_path) as (env, client):
        original_port = env.context.config.server.port
        original_docs = env.context.config.server.docs_url
        env.config.source_path = str(overlay)
        response = client.post("/api/v1/config/reload", headers=AUTH_HEADERS)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "RELOAD_FAILED"
    assert set(error["details"]["fields"]) == {"server.port", "server.docs_url"}
    # the running app must not pretend the change was applied
    assert env.context.config.server.port == original_port
    assert env.context.config.server.docs_url == original_docs


def test_config_reload_rejects_non_hot_reloadable_sections(tmp_path: Path) -> None:
    overlay = _write_reload_overlay(tmp_path, data_dir=tmp_path / "other-data")
    with serving(tmp_path) as (env, client):
        original = env.context.config.storage.data_dir
        env.config.source_path = str(overlay)
        response = client.post("/api/v1/config/reload", headers=AUTH_HEADERS)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "RELOAD_FAILED"
    assert error["details"]["fields"] == ["storage"]
    assert env.context.config.storage.data_dir == original


def test_config_response_redacts_sensitive_provider_params(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        params = env.config.strategies.providers["human_movement"].params
        params["api_token"] = "s3cret-value"
        params["engine"] = "minimum_jerk"
        response = client.get("/api/v1/config", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert "s3cret-value" not in response.text
    provider_params = response.json()["strategies"]["providers"]["human_movement"]["params"]
    assert provider_params["api_token"] == "***"
    assert provider_params["engine"] == "minimum_jerk"


def test_strategies_response_redacts_sensitive_operation_params(tmp_path: Path) -> None:
    from nextautodesk.models import OperationStrategyView
    from nextautodesk.operations import ALL_OPERATIONS

    with serving(tmp_path) as (env, client):
        def views() -> dict[str, OperationStrategyView]:
            return {
                operation.value: OperationStrategyView(
                    operation=operation,
                    provider="human_movement",
                    enabled=True,
                    available=True,
                    params={"password": "hunter2", "engine": "windmouse"},
                    candidates=["human_movement"],
                )
                for operation in ALL_OPERATIONS
            }

        env.strategies.operation_views = views  # type: ignore[method-assign]
        response = client.get("/api/v1/strategies", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert "hunter2" not in response.text
    operations = response.json()["operations"]
    params = operations[next(iter(operations))]["params"]
    assert params["password"] == "***"
    assert params["engine"] == "windmouse"
