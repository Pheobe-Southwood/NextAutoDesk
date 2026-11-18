"""Driver bundle assembly from :class:`~nextautodesk.config.AppConfig`.

The factory is the single place where the desktop environment is bound to the
drivers: it exports ``DISPLAY`` (PyAutoGUI's X11 backend reads it at import
time), verifies that the external tools the drivers depend on are installed and
then wires the four driver implementations into a
:class:`~nextautodesk.drivers.base.DriverBundle`.
"""

from __future__ import annotations

import logging
import os
import shutil

from ..config import AppConfig
from .base import DriverBundle, DriverError
from .pyautogui_x11 import PyAutoGuiKeyboardDriver, PyAutoGuiMouseDriver
from .screen_x11 import X11ScreenDriver
from .window_x11 import X11WindowDriver

LOGGER = logging.getLogger(__name__)

#: Tools needed regardless of configuration.
_BASE_TOOLS: tuple[tuple[str, str], ...] = (
    ("wmctrl", "window enumeration and EWMH state changes"),
    ("xprop", "window state/active-window properties"),
)


def _require_tool(binary: str, purpose: str) -> None:
    if shutil.which(binary) is None:
        raise DriverError(
            f"required tool {binary!r} is not installed: it is needed for {purpose}",
            driver="factory",
            details={"missing_tool": binary, "purpose": purpose},
        )


def _screenshot_tool() -> str:
    for binary in ("scrot", "gnome-screenshot"):
        if shutil.which(binary) is not None:
            return binary
    raise DriverError(
        "screenshot capture requires 'scrot' or 'gnome-screenshot', neither is installed",
        driver="factory",
        details={"missing_tool": "scrot", "alternative": "gnome-screenshot"},
    )


def resolve_display(config: AppConfig) -> str:
    """Return the configured X display and export it for PyAutoGUI/Xlib."""

    display = (config.desktop.display or "").strip()
    if not display or ":" not in display:
        raise DriverError(
            f"invalid X display {config.desktop.display!r} (expected e.g. ':99')",
            driver="factory",
        )
    os.environ["DISPLAY"] = display
    # Pyscreeze (PyAutoGUI's screenshot backend) only enables its X11 path when
    # XDG_SESSION_TYPE is "x11"; set it before PyAutoGUI is imported unless the
    # session already provided one (defense-in-depth for bare Xvfb sessions).
    os.environ.setdefault("XDG_SESSION_TYPE", "x11")
    return display


def check_required_tools(config: AppConfig) -> dict[str, str]:
    """Verify external tooling and return a ``{tool: path}`` mapping."""

    resolved: dict[str, str] = {}
    for binary, purpose in _BASE_TOOLS:
        _require_tool(binary, purpose)
        resolved[binary] = shutil.which(binary) or binary

    window_backend = config.driver.window.backend
    needs_xdotool = window_backend == "xdotool"
    if needs_xdotool:
        _require_tool("xdotool", "the configured window backend 'xdotool'")
        resolved["xdotool"] = shutil.which("xdotool") or "xdotool"

    unicode_mode = config.driver.keyboard.unicode_mode
    if unicode_mode == "xdotool":
        _require_tool("xdotool", "driver.keyboard.unicode_mode='xdotool'")
        resolved["xdotool"] = shutil.which("xdotool") or "xdotool"

    screenshot_tool = _screenshot_tool()
    resolved["screenshot"] = screenshot_tool
    return resolved


def create_driver_bundle(config: AppConfig) -> DriverBundle:
    """Build the driver bundle for the configured virtual desktop.

    Raises:
        DriverError: when ``DISPLAY`` is invalid or a required tool is missing.
    """

    display = resolve_display(config)
    tools = check_required_tools(config)

    mouse = PyAutoGuiMouseDriver()
    keyboard = PyAutoGuiKeyboardDriver(
        unicode_mode=config.driver.keyboard.unicode_mode,
    )
    screen = X11ScreenDriver(jpeg_quality=config.driver.screen.jpeg_quality)
    window = X11WindowDriver(
        backend=config.driver.window.backend,
        command_timeout_s=config.driver.window.command_timeout_ms / 1000.0,
    )
    LOGGER.info(
        "drivers ready (display=%s, window_backend=%s, unicode_mode=%s, tools=%s)",
        display,
        config.driver.window.backend,
        config.driver.keyboard.unicode_mode,
        ",".join(sorted(tools)),
    )
    return DriverBundle(mouse=mouse, keyboard=keyboard, screen=screen, window=window)


__all__ = [
    "check_required_tools",
    "create_driver_bundle",
    "resolve_display",
]
