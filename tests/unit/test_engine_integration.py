"""Real-X11 integration checks for the core engine.

These tests only run inside a live X11 desktop (Xvfb + window manager, e.g. the
project's Docker image). On a headless host the whole module is skipped, so the
regular unit suite stays green.

Run inside the container with::

    DISPLAY=:99 pytest tests/unit/test_engine_integration.py -q -m integration
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from nextautodesk.models import Rect, WindowSelector


def _display_reachable() -> bool:
    """True when $DISPLAY points at a responsive X server."""

    try:
        from Xlib.display import Display

        display = Display()
        display.close()
        return True
    except Exception:  # pragma: no cover - headless environments
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DISPLAY") or not _display_reachable(),
        reason="integration tests require a live X11 DISPLAY",
    ),
]

LOGGER = logging.getLogger("nextautodesk.integration")
_UNICODE_TEXT = "中文OK"
_TERMINAL_TIMEOUT_S = 15.0


@pytest.fixture(scope="module")
def bundle():
    from nextautodesk.config import load_config
    from nextautodesk.drivers.factory import create_driver_bundle

    return create_driver_bundle(load_config())


@pytest.fixture(scope="module")
def strategy_ctx(bundle):
    from nextautodesk.strategies.base import StrategyContext

    return StrategyContext(
        drivers=bundle,
        cancel=threading.Event(),
        logger=LOGGER,
    )


def _movement_provider():
    from nextautodesk.strategies.providers.human_movement import HumanMovementProvider

    return HumanMovementProvider()


def _window_ids(bundle) -> set[str]:
    return {window.id for window in bundle.window.list_windows()}


def _launch_terminal(args: list[str] | None = None) -> subprocess.Popen:
    if not shutil.which("xfce4-terminal"):
        pytest.skip("xfce4-terminal is not installed in this image")
    return subprocess.Popen(
        ["xfce4-terminal", "--disable-server", *(args or [])],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_terminal(bundle, existing_ids: set[str], timeout_s: float = _TERMINAL_TIMEOUT_S):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for window in bundle.window.list_windows():
            if window.id in existing_ids:
                continue
            class_name = (window.class_name or "").casefold()
            instance_name = (window.instance_name or "").casefold()
            if "xfce4-terminal" in {class_name, instance_name} or "terminal" in class_name:
                return window
        time.sleep(0.1)
    pytest.fail("xfce4-terminal window did not appear (is a window manager running?)")
    return None  # pragma: no cover - pytest.fail never returns


def _wait_state(bundle, window, expected: set[str], timeout_s: float = 5.0):
    deadline = time.monotonic() + timeout_s
    current = window
    while time.monotonic() < deadline:
        current = bundle.window.get_window(WindowSelector(id=window.id))
        if current.state in expected:
            return current
        time.sleep(0.1)
    return current


def _wait_file(path: Path, expected: str, timeout_s: float = 20.0) -> str | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            if content == expected:
                return content
        time.sleep(0.1)
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None


def _type_with_provider(bundle, operation, **request_kwargs) -> None:
    from nextautodesk.strategies.base import OperationRequest, StrategyContext

    provider = _movement_provider()
    ctx = StrategyContext(drivers=bundle, cancel=threading.Event(), logger=LOGGER)
    provider.execute(
        OperationRequest(operation=operation, **request_kwargs),
        {
            "profile": "standard",
            "engine": "minimum_jerk",
            "realtime": True,
            "seed": 1234,
            "target_width": 24.0,
            "hold_default_ms": 120,
            "sequence_interval_ms": 150,
        },
        ctx,
    )


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def test_screen_probe_and_screenshot(bundle) -> None:
    size = bundle.screen.size()
    assert size.width >= 320 and size.height >= 200
    png = bundle.screen.screenshot()
    assert png.startswith(b"\x89PNG")
    region = bundle.screen.screenshot(Rect(x=10, y=10, width=100, height=80))
    assert region.startswith(b"\x89PNG")
    assert bundle.screen.screenshot(fmt="jpeg").startswith(b"\xff\xd8")


def test_mouse_move_click_scroll(bundle) -> None:
    start = bundle.mouse.position()
    bundle.mouse.move_to(200, 200)
    position = bundle.mouse.position()
    assert abs(position.x - 200) <= 1 and abs(position.y - 200) <= 1
    bundle.mouse.move_to(start.x, start.y)

    bundle.mouse.button_down("left")
    assert "left" in bundle.mouse.buttons_down()
    bundle.mouse.button_up("left")
    assert "left" not in bundle.mouse.buttons_down()

    bundle.mouse.scroll(0, 1)  # one detent down
    bundle.mouse.scroll(1, 0)  # one detent right
    assert bundle.mouse.buttons_down() == set()


def test_keyboard_ascii_and_unicode_primitives(bundle) -> None:
    assert bundle.keyboard.supports_unicode() is True
    backend = getattr(bundle.keyboard, "unicode_backend", lambda: None)()
    LOGGER.info("unicode backend in container: %s", backend)
    assert backend in {"xlib", "xdotool"}
    bundle.keyboard.type_text("ascii")
    bundle.keyboard.char_down("中")
    bundle.keyboard.char_up("中")


# ---------------------------------------------------------------------------
# human-movement pacing
# ---------------------------------------------------------------------------
def test_human_movement_move_is_naturally_paced(bundle, strategy_ctx) -> None:
    from nextautodesk.operations import Operation
    from nextautodesk.strategies.base import OperationRequest

    provider = _movement_provider()
    bundle.mouse.move_to(100, 100)
    started = time.monotonic()
    outcome = provider.execute(
        OperationRequest(operation=Operation.MOUSE_MOVE, x=1000, y=600),
        {
            "profile": "standard",
            "engine": "minimum_jerk",
            "realtime": True,
            "seed": 42,
            "target_width": 24.0,
        },
        strategy_ctx,
    )
    elapsed = time.monotonic() - started
    position = bundle.mouse.position()
    LOGGER.info(
        "human_movement 900px move: %.3fs, engine=%s, events=%s, position=%s",
        elapsed,
        outcome.engine,
        outcome.details.get("events"),
        position,
    )
    assert elapsed > 0.3, f"900px human-like move was too fast: {elapsed:.3f}s"
    assert abs(position.x - 1000) <= 2 and abs(position.y - 600) <= 2
    assert outcome.engine in {"minimum_jerk", "sigma_lognormal", "feedback", "submovements"}
    assert outcome.details["events"] > 5


def test_human_movement_scroll_and_click(bundle, strategy_ctx) -> None:
    from nextautodesk.operations import Operation
    from nextautodesk.strategies.base import OperationRequest

    provider = _movement_provider()
    params = {"profile": "standard", "realtime": True, "seed": 7}
    provider.execute(
        OperationRequest(operation=Operation.MOUSE_CLICK_LEFT), params, strategy_ctx
    )
    outcome = provider.execute(
        OperationRequest(operation=Operation.MOUSE_SCROLL_VERTICAL, dy=3), params, strategy_ctx
    )
    assert outcome.details["scroll_detents"] == 3


# ---------------------------------------------------------------------------
# Unicode text injection into a real terminal
# ---------------------------------------------------------------------------
def test_unicode_text_into_terminal(bundle) -> None:
    from nextautodesk.operations import Operation

    target = Path(f"/tmp/nad-e2e-unicode-{os.getpid()}.txt")
    target.unlink(missing_ok=True)
    command = f"printf '%s' '{_UNICODE_TEXT}' > {shlex.quote(str(target))}"

    existing = _window_ids(bundle)
    terminal_process = _launch_terminal()
    try:
        window = _wait_for_terminal(bundle, existing)
        bundle.window.activate(window)
        time.sleep(0.5)
        _type_with_provider(bundle, Operation.KEYBOARD_TYPE_TEXT, text=command)
        _type_with_provider(bundle, Operation.KEYBOARD_KEY_PRESS, key="enter")
        content = _wait_file(target, _UNICODE_TEXT)
        LOGGER.info("typed unicode command %r -> file content %r", command, content)
        assert content == _UNICODE_TEXT
    finally:
        terminal_process.terminate()
        try:
            terminal_process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            terminal_process.kill()
        target.unlink(missing_ok=True)


def test_direct_strategy_uppercase_text_into_terminal(bundle) -> None:
    """Regression: PyAutoGUI KEYBOARD_KEYS lacks uppercase; direct type_text must work."""

    from nextautodesk.operations import Operation
    from nextautodesk.strategies.base import OperationRequest, StrategyContext
    from nextautodesk.strategies.providers.direct import DirectProvider

    expected = "Hello WORLD 123"
    target = Path(f"/tmp/nad-e2e-direct-{os.getpid()}.txt")
    target.unlink(missing_ok=True)
    existing = _window_ids(bundle)
    terminal_process = _launch_terminal(
        [
            "-x",
            "bash",
            "-c",
            f"IFS= read -r line; printf '%s' \"$line\" > {shlex.quote(str(target))}",
        ]
    )
    try:
        window = _wait_for_terminal(bundle, existing)
        bundle.window.activate(window)
        time.sleep(0.5)
        provider = DirectProvider()
        ctx = StrategyContext(drivers=bundle, cancel=threading.Event(), logger=LOGGER)
        provider.execute(
            OperationRequest(operation=Operation.KEYBOARD_TYPE_TEXT, text=expected), {}, ctx
        )
        provider.execute(
            OperationRequest(operation=Operation.KEYBOARD_KEY_PRESS, key="enter"), {}, ctx
        )
        content = _wait_file(target, expected)
        LOGGER.info("direct type_text(%r) -> file content %r", expected, content)
        assert content == expected
    finally:
        terminal_process.terminate()
        try:
            terminal_process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            terminal_process.kill()
        target.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Window management
# ---------------------------------------------------------------------------
def test_window_management_on_terminal(bundle) -> None:
    if not bundle.window.window_manager_name():
        pytest.skip("no EWMH window manager is running")
    existing = _window_ids(bundle)
    terminal_process = _launch_terminal()
    try:
        window = _wait_for_terminal(bundle, existing)
        listed = bundle.window.list_windows()
        assert any(item.id == window.id for item in listed)

        bundle.window.activate(window)
        active = bundle.window.active_window()
        assert active is not None and active.id == window.id

        # Xfwm4 repositions the *frame*: the client geometry reported by
        # wmctrl/xprop carries a constant decoration offset, so verify that two
        # absolute moves differ by exactly the requested delta (portable).
        original_geometry = bundle.window.get_window(WindowSelector(id=window.id)).geometry
        bundle.window.move(window, 120, 140)
        deadline = time.monotonic() + 5.0
        first = window
        while time.monotonic() < deadline:
            first = bundle.window.get_window(WindowSelector(id=window.id))
            if (first.geometry.x, first.geometry.y) != (
                original_geometry.x,
                original_geometry.y,
            ):
                break
            time.sleep(0.1)
        assert (first.geometry.x, first.geometry.y) != (
            original_geometry.x,
            original_geometry.y,
        ), f"window did not move from {original_geometry}"
        # _NET_MOVERESIZE_WINDOW positions the frame; client geometry keeps a
        # constant decoration offset, so a +200 frame request is a +200 client
        # delta. Verify that instead of assuming client == requested position.
        bundle.window.move(window, 120 + 200, 140 + 200)
        moved = _wait_geometry(
            bundle, window, x=first.geometry.x + 200, y=first.geometry.y + 200
        )
        assert (
            moved.geometry.x - first.geometry.x,
            moved.geometry.y - first.geometry.y,
        ) == (200, 200)

        # Xfwm4 honours the terminal's WM_NORMAL_HINTS (character-cell grid), so
        # the granted size can be a few pixels off the requested one.
        bundle.window.resize(window, 700, 420)
        resized = _wait_geometry(
            bundle, window, width=700, height=420, tolerance=40
        )
        assert abs(resized.geometry.width - 700) <= 40
        assert abs(resized.geometry.height - 420) <= 40

        bundle.window.minimize(window)
        assert _wait_state(bundle, window, {"minimized"}).state == "minimized"

        bundle.window.restore(window)
        assert _wait_state(bundle, window, {"normal"}).state == "normal"

        bundle.window.maximize(window)
        assert _wait_state(bundle, window, {"maximized"}).state == "maximized"

        bundle.window.restore(window)
        assert _wait_state(bundle, window, {"normal"}).state == "normal"

        wm_name = bundle.window.window_manager_name()
        LOGGER.info("window manager: %s", wm_name)
        assert wm_name

        bundle.window.get_window(WindowSelector(id=window.id))
        bundle.window.close(window)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if not any(item.id == window.id for item in bundle.window.list_windows()):
                break
            time.sleep(0.1)
        assert not any(item.id == window.id for item in bundle.window.list_windows())
    finally:
        terminal_process.terminate()
        try:
            terminal_process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            terminal_process.kill()


def _wait_geometry(
    bundle, window, *, x=None, y=None, width=None, height=None, timeout_s=5.0, tolerance=0
):
    deadline = time.monotonic() + timeout_s
    current = window
    while time.monotonic() < deadline:
        current = bundle.window.get_window(WindowSelector(id=window.id))
        geometry = current.geometry
        if (
            (x is None or abs(geometry.x - x) <= tolerance)
            and (y is None or abs(geometry.y - y) <= tolerance)
            and (width is None or abs(geometry.width - width) <= tolerance)
            and (height is None or abs(geometry.height - height) <= tolerance)
        ):
            return current
        time.sleep(0.1)
    return current
