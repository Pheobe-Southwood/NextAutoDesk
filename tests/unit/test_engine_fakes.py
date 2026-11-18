"""Shared fakes for the core-engine unit tests (no X server required).

Imported by sibling ``test_engine_*`` modules; pytest puts ``tests/unit`` on
``sys.path`` because the test directory is not a package.
"""

from __future__ import annotations

import logging
import threading
from io import BytesIO
from types import SimpleNamespace
from typing import Any

from PIL import Image

from nextautodesk.drivers.base import DriverBundle
from nextautodesk.models import Point, Rect, Size, StrategyOutcome, WindowInfo, WindowSelector
from nextautodesk.operations import Operation
from nextautodesk.strategies.base import OperationRequest, StrategyContext


def png_bytes(width: int = 12, height: int = 8) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeMouse:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._position = Point(x=0, y=0)
        self._buttons: set[str] = set()

    def position(self) -> Point:
        self.calls.append(("position",))
        return self._position

    def move_to(self, x: float, y: float, duration_s: float = 0.0) -> None:
        self.calls.append(("move_to", float(x), float(y), float(duration_s)))
        self._position = Point(x=float(x), y=float(y))

    def button_down(self, button: str) -> None:
        self.calls.append(("button_down", button))
        self._buttons.add(button)

    def button_up(self, button: str) -> None:
        self.calls.append(("button_up", button))
        self._buttons.discard(button)

    def scroll(self, dx: int, dy: int) -> None:
        self.calls.append(("scroll", dx, dy))

    def buttons_down(self) -> set[str]:
        return set(self._buttons)

    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakeKeyboard:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def key_down(self, key: str) -> None:
        self.calls.append(("key_down", key))

    def key_up(self, key: str) -> None:
        self.calls.append(("key_up", key))

    def type_text(self, text: str) -> None:
        self.calls.append(("type_text", text))

    def char_down(self, char: str) -> None:
        self.calls.append(("char_down", char))

    def char_up(self, char: str) -> None:
        self.calls.append(("char_up", char))

    def supports_unicode(self) -> bool:
        return True

    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakeScreen:
    def __init__(self, width: int = 1920, height: int = 1080) -> None:
        self.width = width
        self.height = height
        self.screenshot_calls: list[tuple[Rect | None, str]] = []

    def size(self) -> Size:
        return Size(width=self.width, height=self.height)

    def screenshot(self, region: Rect | None = None, fmt: str = "png") -> bytes:
        self.screenshot_calls.append((region, fmt))
        if fmt == "jpeg":
            buffer = BytesIO()
            Image.new("RGB", (12, 8), (0, 0, 0)).save(buffer, format="JPEG")
            return buffer.getvalue()
        return png_bytes()


class FakeWindow:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.windows: list[WindowInfo] = []
        self._active: WindowInfo | None = None

    def set_windows(self, windows: list[WindowInfo], active: WindowInfo | None = None) -> None:
        self.windows = list(windows)
        self._active = active

    def _find(self, selector: WindowSelector) -> WindowInfo:
        for window in self.windows:
            if selector.id is not None and window.id != selector.id:
                continue
            if selector.title is not None and window.title != selector.title:
                continue
            if selector.class_name is not None and window.class_name != selector.class_name:
                continue
            return window
        from nextautodesk.errors import WindowNotFound

        raise WindowNotFound("no window", details={"selector": selector.model_dump()})

    def list_windows(self) -> list[WindowInfo]:
        self.calls.append(("list_windows",))
        for window in self.windows:
            window.active = self._active is not None and window.id == self._active.id
        return list(self.windows)

    def active_window(self) -> WindowInfo | None:
        self.calls.append(("active_window",))
        return self._active

    def get_window(self, selector: WindowSelector) -> WindowInfo:
        self.calls.append(("get_window", selector.id, selector.title, selector.active))
        return self._find(selector)

    def _mutate(self, action: str, window: WindowInfo, *extra: Any) -> None:
        self.calls.append((action, window.id, *extra))

    def activate(self, window: WindowInfo) -> None:
        self._mutate("activate", window)
        self._active = window
        window.active = True

    def move(self, window: WindowInfo, x: int, y: int) -> None:
        self._mutate("move", window, x, y)
        window.geometry = Rect(x=x, y=y, width=window.geometry.width, height=window.geometry.height)

    def resize(self, window: WindowInfo, width: int, height: int) -> None:
        self._mutate("resize", window, width, height)
        window.geometry = Rect(
            x=window.geometry.x, y=window.geometry.y, width=width, height=height
        )

    def move_resize(self, window: WindowInfo, x: int, y: int, width: int, height: int) -> None:
        self._mutate("move_resize", window, x, y, width, height)
        window.geometry = Rect(x=x, y=y, width=width, height=height)

    def minimize(self, window: WindowInfo) -> None:
        self._mutate("minimize", window)
        window.state = "minimized"

    def maximize(self, window: WindowInfo) -> None:
        self._mutate("maximize", window)
        window.state = "maximized"

    def restore(self, window: WindowInfo) -> None:
        self._mutate("restore", window)
        window.state = "normal"

    def close(self, window: WindowInfo) -> None:
        self._mutate("close", window)
        self.windows = [item for item in self.windows if item.id != window.id]

    def window_manager_name(self) -> str | None:
        return "Xfwm4"


def make_bundle(
    *,
    mouse: FakeMouse | None = None,
    keyboard: FakeKeyboard | None = None,
    screen: FakeScreen | None = None,
    window: FakeWindow | None = None,
) -> tuple[DriverBundle, FakeMouse, FakeKeyboard, FakeScreen, FakeWindow]:
    """Build a DriverBundle of fakes and return it with the concrete fakes."""

    fake_mouse = mouse or FakeMouse()
    fake_keyboard = keyboard or FakeKeyboard()
    fake_screen = screen or FakeScreen()
    fake_window = window or FakeWindow()
    bundle = DriverBundle(
        mouse=fake_mouse,
        keyboard=fake_keyboard,
        screen=fake_screen,
        window=fake_window,
    )
    return bundle, fake_mouse, fake_keyboard, fake_screen, fake_window


def make_strategy_context(
    bundle: DriverBundle,
    *,
    cancel: threading.Event | None = None,
    seed: int | None = None,
) -> StrategyContext:
    return StrategyContext(
        drivers=bundle,
        cancel=cancel or threading.Event(),
        logger=logging.getLogger("nextautodesk.tests"),
        seed=seed,
    )


class FakeScreenshotInfo:
    def __init__(self, record: dict[str, Any]) -> None:
        self._record = record

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        del mode
        return dict(self._record)


class FakeScreenshotStore:
    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []

    def save(self, data: bytes, *, fmt="png", name=None, width=None, height=None):
        from nextautodesk.models import utc_now

        record = {
            "name": name or f"screen_{len(self.saved)}.{fmt}",
            "size_bytes": len(data),
            "width": width,
            "height": height,
            "format": fmt,
            "created_at": utc_now().isoformat(),
            "download_url": "/api/v1/screen/screenshots/x",
        }
        self.saved.append(record)
        return FakeScreenshotInfo(record)

    @property
    def count(self) -> int:
        return len(self.saved)


class RecordingActionContext:
    """Minimal ActionContext stand-in that records strategy invocations."""

    def __init__(
        self,
        bundle: DriverBundle,
        *,
        store: Any = None,
        cancel: threading.Event | None = None,
        strategy_override: str | None = None,
    ) -> None:
        from nextautodesk.errors import TaskCancelled

        self.drivers = bundle
        self.config = SimpleNamespace(desktop=SimpleNamespace(display=":99"))
        self.store = SimpleNamespace(screenshots=store or FakeScreenshotStore())
        self.cancel = cancel or threading.Event()
        self.logger = logging.getLogger("nextautodesk.tests")
        self.strategy_override = strategy_override
        self.outcomes: list[StrategyOutcome] = []
        self.strategy_calls: list[tuple[Operation, OperationRequest, str | None]] = []
        self.sleeps: list[float] = []
        self._task_cancelled = TaskCancelled

    def run_strategy(
        self,
        operation: Operation,
        request: OperationRequest,
        *,
        strategy_override: str | None = None,
    ) -> StrategyOutcome:
        override = strategy_override if strategy_override is not None else self.strategy_override
        self.strategy_calls.append((operation, request, override))
        outcome = StrategyOutcome(operation=operation, provider="direct", duration_ms=0.0)
        self.outcomes.append(outcome)
        return outcome

    def strategy_context(self, seed: int | None = None) -> StrategyContext:
        return make_strategy_context(self.drivers, cancel=self.cancel, seed=seed)

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise self._task_cancelled("execution cancelled")

    def sleep(self, seconds: float) -> None:
        self.check_cancelled()
        if seconds > 0:
            self.sleeps.append(seconds)

    def require_supported(self, operation: Operation, *, strategy_override: str | None = None) -> None:
        return None

    def operations(self) -> list[Operation]:
        return [call[0] for call in self.strategy_calls]


def make_window(
    window_id: str = "0x3a00007",
    *,
    title: str = "Terminal - nad",
    class_name: str | None = "Xfce4-terminal",
    instance_name: str | None = "xfce4-terminal",
    state: str = "normal",
) -> WindowInfo:
    return WindowInfo(
        id=window_id,
        title=title,
        class_name=class_name,
        instance_name=instance_name,
        pid=1234,
        desktop=0,
        geometry=Rect(x=100, y=80, width=800, height=500),
        state=state,
        active=False,
    )


__all__ = [
    "FakeKeyboard",
    "FakeMouse",
    "FakeScreen",
    "FakeScreenshotStore",
    "FakeWindow",
    "RecordingActionContext",
    "make_bundle",
    "make_strategy_context",
    "make_window",
    "png_bytes",
]
