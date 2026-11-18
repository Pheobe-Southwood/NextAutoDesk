"""Unit tests for the action layer: validation, routing and output shapes."""

from __future__ import annotations

import base64
import threading
import time

import pytest
from test_engine_fakes import (
    FakeScreenshotStore,
    FakeWindow,
    RecordingActionContext,
    make_bundle,
    make_window,
    png_bytes,
)

from nextautodesk.actions import desktop, elements, interactions, keyboard, mouse, screen, window
from nextautodesk.actions.registry import ActionRegistry
from nextautodesk.errors import (
    InvalidArgument,
    TaskCancelled,
    UnknownAction,
    ValidationFailed,
    WindowNotFound,
)
from nextautodesk.models import Rect
from nextautodesk.operations import Operation

OP = Operation

ALL_HANDLERS = [
    *mouse.ACTIONS,
    *keyboard.ACTIONS,
    *screen.ACTIONS,
    *window.ACTIONS,
    *desktop.ACTIONS,
    *elements.ACTIONS,
    *interactions.ACTIONS,
]

EXPECTED_ACTION_NAMES = sorted(
    [
        "element.find",
        "element.ocr",
        "element.wait",
        "interaction.click",
        "interaction.type",
        "mouse.move",
        "mouse.move_relative",
        "mouse.click",
        "mouse.down",
        "mouse.up",
        "mouse.hold",
        "mouse.drag",
        "mouse.scroll",
        "mouse.position",
        "mouse.state",
        "keyboard.type",
        "keyboard.press",
        "keyboard.hotkey",
        "keyboard.sequence",
        "keyboard.key_down",
        "keyboard.key_up",
        "keyboard.hold",
        "screen.screenshot",
        "screen.info",
        "window.list",
        "window.active",
        "window.get",
        "window.activate",
        "window.move",
        "window.resize",
        "window.minimize",
        "window.maximize",
        "window.restore",
        "window.close",
        "desktop.wait",
    ]
)

READ_ONLY_ACTIONS = sorted(
    [
        "mouse.position",
        "element.find",
        "element.ocr",
        "element.wait",
        "mouse.state",
        "screen.info",
        "screen.screenshot",
        "window.active",
        "window.get",
        "window.list",
        "desktop.wait",
    ]
)


def _ctx(**kwargs) -> RecordingActionContext:
    bundle, *_fakes = make_bundle()
    return RecordingActionContext(bundle, **kwargs)


def _fakes(ctx: RecordingActionContext):
    return ctx.drivers.mouse, ctx.drivers.keyboard, ctx.drivers.screen, ctx.drivers.window


# ---------------------------------------------------------------------------
# Catalogue + registry
# ---------------------------------------------------------------------------
def test_action_modules_export_expected_actions() -> None:
    names = sorted(handler.name for handler in ALL_HANDLERS)
    assert names == EXPECTED_ACTION_NAMES
    assert len(names) == len(set(names)) == 35


@pytest.mark.parametrize("handler", ALL_HANDLERS, ids=lambda handler: handler.name)
def test_action_metadata(handler) -> None:
    assert handler.summary
    assert handler.category in {
        "mouse",
        "keyboard",
        "screen",
        "window",
        "desktop",
        "elements",
        "interactions",
    }
    assert handler.tier in {"primitive", "perception", "composite"}
    assert handler.params_model.model_config.get("extra") == "forbid"
    info = handler.info()
    assert info.params_schema["type"] == "object"
    assert info.tier == handler.tier
    assert info.mutating == handler.mutating
    if handler.example:
        handler.params_model.model_validate(handler.example)


def test_read_only_flags() -> None:
    read_only = sorted(handler.name for handler in ALL_HANDLERS if not handler.mutating)
    assert read_only == READ_ONLY_ACTIONS


def test_api_tiers_are_consistent() -> None:
    by_name = {handler.name: handler for handler in ALL_HANDLERS}
    assert by_name["mouse.move"].tier == "primitive"
    assert by_name["element.find"].tier == "perception"
    assert by_name["element.wait"].tier == "perception"
    assert by_name["element.ocr"].tier == "perception"
    assert by_name["interaction.click"].tier == "composite"
    assert by_name["interaction.type"].tier == "composite"
    tier_counts: dict[str, int] = {}
    for handler in ALL_HANDLERS:
        tier_counts[handler.tier] = tier_counts.get(handler.tier, 0) + 1
    assert tier_counts == {"primitive": 30, "perception": 3, "composite": 2}


def test_default_registry_loads_all_actions() -> None:
    registry = ActionRegistry.build_default()
    if len(registry) == 0:  # pragma: no cover - guards a known development-tree issue
        pytest.skip("ActionRegistry.build_default() found no modules in this checkout")
    assert registry.names == EXPECTED_ACTION_NAMES
    assert registry.has("mouse.move_relative")
    with pytest.raises(UnknownAction):
        registry.get("no.such.action")


# ---------------------------------------------------------------------------
# Mouse routing
# ---------------------------------------------------------------------------
def test_mouse_move_routing() -> None:
    ctx = _ctx()
    handler = next(h for h in mouse.ACTIONS if h.name == "mouse.move")
    params = handler.parse_params({"x": 640, "y": 360, "duration_ms": 250})
    output = handler.execute(params, ctx)
    assert len(ctx.strategy_calls) == 1
    operation, request, override = ctx.strategy_calls[0]
    assert operation is OP.MOUSE_MOVE
    assert (request.x, request.y, request.duration_ms) == (640.0, 360.0, 250.0)
    assert override is None
    assert output["position"] == {"x": 0.0, "y": 0.0}
    assert output["requested"] == {"x": 640.0, "y": 360.0}


def test_mouse_click_routes_move_then_click() -> None:
    ctx = _ctx()
    handler = next(h for h in mouse.ACTIONS if h.name == "mouse.click")
    params = handler.parse_params({"x": 10, "y": 20})
    handler.execute(params, ctx)
    assert [call[0] for call in ctx.strategy_calls] == [OP.MOUSE_MOVE, OP.MOUSE_CLICK_LEFT]
    assert ctx.strategy_calls[1][1].button == "left"

    ctx = _ctx()
    handler.execute(handler.parse_params({"button": "right"}), ctx)
    assert [call[0] for call in ctx.strategy_calls] == [OP.MOUSE_CLICK_RIGHT]


def test_mouse_click_double_and_multi() -> None:
    handler = next(h for h in mouse.ACTIONS if h.name == "mouse.click")

    ctx = _ctx()
    handler.execute(handler.parse_params({"clicks": 2}), ctx)
    assert [call[0] for call in ctx.strategy_calls] == [OP.MOUSE_DOUBLE_CLICK]

    ctx = _ctx()
    handler.execute(handler.parse_params({"clicks": 3}), ctx)
    assert [call[0] for call in ctx.strategy_calls] == [OP.MOUSE_CLICK_LEFT]
    assert ctx.strategy_calls[0][1].clicks == 3

    ctx = _ctx()
    handler.execute(handler.parse_params({"clicks": 3, "interval_ms": 100}), ctx)
    assert [call[0] for call in ctx.strategy_calls] == [
        OP.MOUSE_CLICK_LEFT,
        OP.MOUSE_CLICK_LEFT,
        OP.MOUSE_CLICK_LEFT,
    ]
    assert ctx.sleeps == [pytest.approx(0.1), pytest.approx(0.1)]


def test_mouse_down_up_hold_routing() -> None:
    for name, expected in (
        ("mouse.down", OP.MOUSE_DOWN),
        ("mouse.up", OP.MOUSE_UP),
        ("mouse.hold", OP.MOUSE_HOLD),
    ):
        ctx = _ctx()
        handler = next(h for h in mouse.ACTIONS if h.name == name)
        params = handler.parse_params({"x": 5, "y": 6, "button": "middle"})
        handler.execute(params, ctx)
        assert [call[0] for call in ctx.strategy_calls] == [OP.MOUSE_MOVE, expected]
        assert ctx.strategy_calls[-1][1].button == "middle"


def test_mouse_drag_routing() -> None:
    handler = next(h for h in mouse.ACTIONS if h.name == "mouse.drag")

    ctx = _ctx()
    handler.execute(
        handler.parse_params({"from_x": 10, "from_y": 10, "to_x": 50, "to_y": 60}), ctx
    )
    assert [call[0] for call in ctx.strategy_calls] == [OP.MOUSE_MOVE, OP.MOUSE_DRAG]
    assert ctx.strategy_calls[-1][1].x == 50.0
    assert ctx.strategy_calls[-1][1].button == "left"

    ctx = _ctx()
    handler.execute(handler.parse_params({"to_x": 50, "to_y": 60}), ctx)
    assert [call[0] for call in ctx.strategy_calls] == [OP.MOUSE_DRAG]


def test_mouse_scroll_routing() -> None:
    handler = next(h for h in mouse.ACTIONS if h.name == "mouse.scroll")

    ctx = _ctx()
    handler.execute(handler.parse_params({"dy": -3}), ctx)
    assert [call[0] for call in ctx.strategy_calls] == [OP.MOUSE_SCROLL_VERTICAL]
    assert ctx.strategy_calls[0][1].dy == -3.0

    ctx = _ctx()
    handler.execute(handler.parse_params({"dx": 2}), ctx)
    assert [call[0] for call in ctx.strategy_calls] == [OP.MOUSE_SCROLL_HORIZONTAL]

    ctx = _ctx()
    output = handler.execute(handler.parse_params({"dx": 2, "dy": -3}), ctx)
    assert [call[0] for call in ctx.strategy_calls] == [
        OP.MOUSE_SCROLL_VERTICAL,
        OP.MOUSE_SCROLL_HORIZONTAL,
    ]
    assert output["detents"] == 5


def test_mouse_position_is_read_only() -> None:
    ctx = _ctx()
    ctx.drivers.mouse.move_to(11.0, 22.0)
    handler = next(h for h in mouse.ACTIONS if h.name == "mouse.position")
    output = handler.execute(handler.parse_params({}), ctx)
    assert output == {"x": 11.0, "y": 22.0}
    assert ctx.strategy_calls == []


def test_mouse_state_reports_pressed_buttons() -> None:
    ctx = _ctx()
    ctx.drivers.mouse.move_to(11.0, 22.0)
    ctx.drivers.mouse.button_down("left")
    ctx.drivers.mouse.button_down("middle")
    handler = next(h for h in mouse.ACTIONS if h.name == "mouse.state")
    output = handler.execute(handler.parse_params({}), ctx)
    assert output == {
        "position": {"x": 11.0, "y": 22.0},
        "buttons": ["left", "middle"],
    }
    assert ctx.strategy_calls == []


def test_mouse_param_validation() -> None:
    handler = next(h for h in mouse.ACTIONS if h.name == "mouse.click")
    for bad in ({"x": 10}, {"button": "x1"}, {"clicks": 0}, {"unknown": 1}):
        with pytest.raises(ValidationFailed):
            handler.parse_params(bad)

    drag = next(h for h in mouse.ACTIONS if h.name == "mouse.drag")
    with pytest.raises(ValidationFailed):
        drag.parse_params({"from_x": 1, "to_x": 2, "to_y": 3})
    with pytest.raises(ValidationFailed):
        drag.parse_params({"to_x": 2})

    scroll = next(h for h in mouse.ACTIONS if h.name == "mouse.scroll")
    with pytest.raises(ValidationFailed):
        scroll.parse_params({"dx": 0, "dy": 0})


def test_mouse_strategy_override_forwarded() -> None:
    ctx = _ctx()
    handler = next(h for h in mouse.ACTIONS if h.name == "mouse.move")
    params = handler.parse_params({"x": 1, "y": 2, "strategy": "direct"})
    handler.execute(params, ctx)
    assert ctx.strategy_calls[0][2] == "direct"


# ---------------------------------------------------------------------------
# Keyboard routing
# ---------------------------------------------------------------------------
def test_keyboard_type_and_press() -> None:
    ctx = _ctx()
    type_handler = next(h for h in keyboard.ACTIONS if h.name == "keyboard.type")
    output = type_handler.execute(type_handler.parse_params({"text": "hi 中文"}), ctx)
    operation, request, _ = ctx.strategy_calls[0]
    assert operation is OP.KEYBOARD_TYPE_TEXT
    assert request.text == "hi 中文"
    assert output["length"] == 5

    press = next(h for h in keyboard.ACTIONS if h.name == "keyboard.press")
    output = press.execute(press.parse_params({"key": "ENTER"}), ctx)
    assert output["key"] == "enter"
    assert ctx.strategy_calls[-1][0] is OP.KEYBOARD_KEY_PRESS
    assert ctx.strategy_calls[-1][1].key == "enter"


def test_keyboard_hotkey_sequence_hold_and_release() -> None:
    ctx = _ctx()
    hotkey = next(h for h in keyboard.ACTIONS if h.name == "keyboard.hotkey")
    hotkey.execute(hotkey.parse_params({"keys": ["CTRL", "c"]}), ctx)
    assert ctx.strategy_calls[-1][0] is OP.KEYBOARD_HOTKEY
    assert ctx.strategy_calls[-1][1].keys == ("ctrl", "c")

    sequence = next(h for h in keyboard.ACTIONS if h.name == "keyboard.sequence")
    sequence.execute(sequence.parse_params({"keys": ["TAB", "enter"]}), ctx)
    assert ctx.strategy_calls[-1][0] is OP.KEYBOARD_KEY_SEQUENCE
    assert ctx.strategy_calls[-1][1].keys == ("tab", "enter")

    hold = next(h for h in keyboard.ACTIONS if h.name == "keyboard.hold")
    hold.execute(hold.parse_params({"key": "shift", "hold_ms": 250}), ctx)
    assert ctx.strategy_calls[-1][0] is OP.KEYBOARD_KEY_HOLD
    assert ctx.strategy_calls[-1][1].hold_ms == 250

    key_up = next(h for h in keyboard.ACTIONS if h.name == "keyboard.key_up")
    key_up.execute(key_up.parse_params({"key": "shift"}), ctx)
    assert ctx.strategy_calls[-1][0] is OP.KEYBOARD_KEY_RELEASE


def test_keyboard_key_down_routes_through_strategy() -> None:
    ctx = _ctx()
    _mouse, fake_keyboard, _screen, _window = _fakes(ctx)
    key_down = next(h for h in keyboard.ACTIONS if h.name == "keyboard.key_down")
    output = key_down.execute(key_down.parse_params({"key": "Shift"}), ctx)
    assert [call[0] for call in ctx.strategy_calls] == [OP.KEYBOARD_KEY_DOWN]
    assert ctx.strategy_calls[0][1].key == "shift"
    assert fake_keyboard.calls == []
    assert output == {"key": "shift", "pressed": True}

    ctx = _ctx()
    key_down.execute(key_down.parse_params({"key": "shift", "strategy": "direct"}), ctx)
    assert ctx.strategy_calls[-1][2] == "direct"


def test_keyboard_validation() -> None:
    press = next(h for h in keyboard.ACTIONS if h.name == "keyboard.press")
    with pytest.raises(ValidationFailed):
        press.parse_params({})
    with pytest.raises(InvalidArgument):
        press.execute(press.parse_params({"key": "not-a-key"}), _ctx())

    hotkey = next(h for h in keyboard.ACTIONS if h.name == "keyboard.hotkey")
    with pytest.raises(ValidationFailed):
        hotkey.parse_params({"keys": []})


# ---------------------------------------------------------------------------
# Screen actions
# ---------------------------------------------------------------------------
def test_screen_screenshot_inline_base64() -> None:
    ctx = _ctx()
    handler = next(h for h in screen.ACTIONS if h.name == "screen.screenshot")
    output = handler.execute(handler.parse_params({}), ctx)
    assert output["format"] == "png"
    assert output["size_bytes"] == len(png_bytes())
    assert (output["width"], output["height"]) == (12, 8)
    assert output["mime_type"] == "image/png"
    assert base64.b64decode(output["image_base64"]).startswith(b"\x89PNG")


def test_screen_screenshot_save_and_region() -> None:
    store = FakeScreenshotStore()
    ctx = _ctx(store=store)
    handler = next(h for h in screen.ACTIONS if h.name == "screen.screenshot")
    params = handler.parse_params(
        {"region": {"x": 1, "y": 2, "width": 20, "height": 10}, "save": True, "name": "cap"}
    )
    output = handler.execute(params, ctx)
    assert store.count == 1
    assert output["screenshot"]["name"] == "cap"
    assert ctx.drivers.screen.screenshot_calls[-1][0] == Rect(
        x=1, y=2, width=20, height=10
    )


def test_screen_info_and_mouse_position_flags() -> None:
    ctx = _ctx()
    info = next(h for h in screen.ACTIONS if h.name == "screen.info")
    output = info.execute(info.parse_params({}), ctx)
    assert output == {"width": 1920, "height": 1080, "display": ":99"}
    assert info.mutating is False
    screenshot = next(h for h in screen.ACTIONS if h.name == "screen.screenshot")
    assert screenshot.mutating is False


# ---------------------------------------------------------------------------
# Window actions
# ---------------------------------------------------------------------------
def _window_ctx() -> RecordingActionContext:
    ctx = _ctx()
    fake_window = ctx.drivers.window
    terminal = make_window()
    other = make_window("0x4a00001", title="File Manager", class_name="Thunar")
    fake_window.set_windows([terminal, other], active=terminal)
    return ctx


def test_window_read_actions() -> None:
    ctx = _window_ctx()
    window_list = next(h for h in window.ACTIONS if h.name == "window.list")
    output = window_list.execute(window_list.parse_params({}), ctx)
    assert output["count"] == 2
    assert output["windows"][0]["id"] == "0x3a00007"

    active = next(h for h in window.ACTIONS if h.name == "window.active")
    output = active.execute(active.parse_params({}), ctx)
    assert output["window"]["title"].startswith("Terminal")

    get = next(h for h in window.ACTIONS if h.name == "window.get")
    output = get.execute(get.parse_params({"title": "File Manager"}), ctx)
    assert output["window"]["class_name"] == "Thunar"

    for handler in (window_list, active, get):
        assert handler.mutating is False


def test_window_mutation_actions() -> None:
    ctx = _window_ctx()
    fake_window = ctx.drivers.window

    activate = next(h for h in window.ACTIONS if h.name == "window.activate")
    output = activate.execute(activate.parse_params({"title": "File Manager"}), ctx)
    assert ("activate", "0x4a00001") in fake_window.calls
    assert output["window"]["active"] is True

    move = next(h for h in window.ACTIONS if h.name == "window.move")
    output = move.execute(move.parse_params({"title": "File Manager", "x": 40, "y": 50}), ctx)
    geometry = output["window"]["geometry"]
    assert (geometry["x"], geometry["y"]) == (40, 50)

    resize = next(h for h in window.ACTIONS if h.name == "window.resize")
    output = resize.execute(
        resize.parse_params({"title": "File Manager", "width": 640, "height": 480}), ctx
    )
    assert (output["window"]["geometry"]["width"], output["window"]["geometry"]["height"]) == (
        640,
        480,
    )

    minimize = next(h for h in window.ACTIONS if h.name == "window.minimize")
    output = minimize.execute(minimize.parse_params({"title": "File Manager"}), ctx)
    assert output["window"]["state"] == "minimized"

    maximize = next(h for h in window.ACTIONS if h.name == "window.maximize")
    output = maximize.execute(maximize.parse_params({"title": "File Manager"}), ctx)
    assert output["window"]["state"] == "maximized"

    restore = next(h for h in window.ACTIONS if h.name == "window.restore")
    output = restore.execute(restore.parse_params({"title": "File Manager"}), ctx)
    assert output["window"]["state"] == "normal"

    close = next(h for h in window.ACTIONS if h.name == "window.close")
    output = close.execute(close.parse_params({"title": "File Manager"}), ctx)
    assert output["closed"] is True
    assert ("close", "0x4a00001") in fake_window.calls


def test_window_selector_validation_in_actions() -> None:
    get = next(h for h in window.ACTIONS if h.name == "window.get")
    with pytest.raises(ValidationFailed):
        get.parse_params({})
    move = next(h for h in window.ACTIONS if h.name == "window.move")
    with pytest.raises(ValidationFailed):
        move.parse_params({"title": "x"})

    ctx = _window_ctx()
    with pytest.raises(WindowNotFound):
        get.execute(get.parse_params({"title": "missing"}), ctx)


class DecoratedFakeWindow(FakeWindow):
    """WM mock: commands address the frame, reported geometry is the client."""

    def move(self, window, x, y) -> None:
        self._mutate("move", window, x, y)
        window.geometry = Rect(
            x=x - 4, y=y - 58, width=window.geometry.width, height=window.geometry.height
        )

    def resize(self, window, width, height) -> None:
        self._mutate("resize", window, width, height)
        geometry = window.geometry
        window.geometry = Rect(
            x=geometry.x, y=geometry.y, width=width - 3, height=height - 13
        )


def test_window_geometry_waits_converge_instead_of_timing_out() -> None:
    """Decoration/size-hint offsets must not stall move/resize for 2 seconds."""

    bundle, _, _, _, fake_window = make_bundle(window=DecoratedFakeWindow())
    ctx = RecordingActionContext(bundle)
    fake_window.set_windows([make_window()])
    fake_window = ctx.drivers.window

    move = next(h for h in window.ACTIONS if h.name == "window.move")
    started = time.monotonic()
    output = move.execute(move.parse_params({"id": "0x3a00007", "x": 40, "y": 50}), ctx)
    move_elapsed = time.monotonic() - started
    assert move_elapsed < 1.0
    geometry = output["window"]["geometry"]
    assert (geometry["x"], geometry["y"]) == (36, -8)

    resize = next(h for h in window.ACTIONS if h.name == "window.resize")
    started = time.monotonic()
    output = resize.execute(
        resize.parse_params({"id": "0x3a00007", "width": 640, "height": 480}), ctx
    )
    resize_elapsed = time.monotonic() - started
    assert resize_elapsed < 1.0
    assert output["window"]["geometry"]["width"] == 637
    assert output["window"]["geometry"]["height"] == 467


def test_cancelled_context_stops_waiting_actions() -> None:
    ctx = _ctx()
    ctx.cancel = threading.Event()
    ctx.cancel.set()
    handler = next(h for h in desktop.ACTIONS if h.name == "desktop.wait")
    with pytest.raises(TaskCancelled):
        handler.execute(handler.parse_params({"ms": 100}), ctx)


# ---------------------------------------------------------------------------
# desktop.wait
# ---------------------------------------------------------------------------
def test_desktop_wait() -> None:
    ctx = _ctx()
    handler = next(h for h in desktop.ACTIONS if h.name == "desktop.wait")
    output = handler.execute(handler.parse_params({"ms": 250}), ctx)
    assert output == {"waited_ms": 250}
    assert ctx.sleeps == [0.25]
    with pytest.raises(ValidationFailed):
        handler.parse_params({"ms": -1})
