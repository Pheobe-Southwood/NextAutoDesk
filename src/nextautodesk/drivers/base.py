"""Driver layer contracts.

Drivers are *strategy-agnostic execution engines*: they know how to move the
pointer, press a button, type a character, capture the screen or manipulate a
window, but they never decide *when* or *how human-like* an action should be.
Pacing and behaviour belong to the strategy layer.

The default implementation lives in :mod:`nextautodesk.drivers.pyautogui_x11`
(mouse/keyboard/screen via PyAutoGUI) and
:mod:`nextautodesk.drivers.window_x11` (window management via xdotool/wmctrl).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import MouseButton, Point, Rect, ScreenshotFormat, Size, WindowInfo, WindowSelector


class DriverError(RuntimeError):
    """Raised when the underlying execution engine fails."""

    def __init__(self, message: str, *, driver: str = "", details: dict | None = None):
        super().__init__(message)
        self.driver = driver
        self.details = details or {}


@runtime_checkable
class MouseDriver(Protocol):
    """Pointer / button / wheel primitives."""

    def position(self) -> Point: ...

    def move_to(self, x: float, y: float, duration_s: float = 0.0) -> None:
        """Move the pointer to absolute coordinates.

        ``duration_s`` is a *driver-level* interpolation hint used by strategies
        that delegate interpolation to the engine (e.g. WindMouse step pacing).
        Human-like pacing normally comes from the strategy layer.
        """

    def button_down(self, button: MouseButton) -> None: ...

    def button_up(self, button: MouseButton) -> None: ...

    def scroll(self, dx: int, dy: int) -> None:
        """Wheel detents. Positive ``dy`` scrolls *down*, positive ``dx`` scrolls right."""

    def buttons_down(self) -> set[MouseButton]: ...


@runtime_checkable
class KeyboardDriver(Protocol):
    """Key / text primitives."""

    def key_down(self, key: str) -> None: ...

    def key_up(self, key: str) -> None: ...

    def type_text(self, text: str) -> None:
        """Inject a complete string. Implementations handle non-ASCII characters."""

    def char_down(self, char: str) -> None:
        """Press (and hold) the key that produces ``char``.

        Used by timestamped text strategies (human-movement emits ``text_down`` /
        ``text_up`` events per code point). Implementations must support Unicode.
        """

    def char_up(self, char: str) -> None:
        """Release the key pressed by :meth:`char_down`."""

    def supports_unicode(self) -> bool: ...


@runtime_checkable
class ScreenDriver(Protocol):
    """Screen probing and capture."""

    def size(self) -> Size: ...

    def screenshot(self, region: Rect | None = None, fmt: ScreenshotFormat = "png") -> bytes: ...


@runtime_checkable
class WindowDriver(Protocol):
    """Window enumeration and manipulation (EWMH)."""

    def list_windows(self) -> list[WindowInfo]: ...

    def active_window(self) -> WindowInfo | None: ...

    def get_window(self, selector: WindowSelector) -> WindowInfo: ...

    def activate(self, window: WindowInfo) -> None: ...

    def move(self, window: WindowInfo, x: int, y: int) -> None: ...

    def resize(self, window: WindowInfo, width: int, height: int) -> None: ...

    def move_resize(self, window: WindowInfo, x: int, y: int, width: int, height: int) -> None: ...

    def minimize(self, window: WindowInfo) -> None: ...

    def maximize(self, window: WindowInfo) -> None: ...

    def restore(self, window: WindowInfo) -> None: ...

    def close(self, window: WindowInfo) -> None: ...

    def window_manager_name(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class DriverBundle:
    """All drivers of a desktop node bundled for injection into strategies/actions."""

    mouse: MouseDriver
    keyboard: KeyboardDriver
    screen: ScreenDriver
    window: WindowDriver

    def probe(self) -> None:
        """Cheap liveness probe used by readiness checks. Raises :class:`DriverError`."""

        self.screen.size()
        self.mouse.position()


__all__ = [
    "DriverBundle",
    "DriverError",
    "KeyboardDriver",
    "MouseDriver",
    "ScreenDriver",
    "WindowDriver",
]
