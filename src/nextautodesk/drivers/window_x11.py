"""EWMH window management for an X11 desktop.

The driver never mutates windows through raw X11 calls: all state changes go
through the standard EWMH tooling so the window manager stays in charge.

* ``wmctrl`` is the primary source of truth for the window list (``-l -p -G -x``
  gives id, desktop, pid, geometry, class and title in one call) and for EWMH
  state changes that xdotool does not expose (maximize/restore/close).
* ``xdotool`` performs focus/geometry/minimize operations when selected as the
  configured backend (``driver.window.backend``).
* ``xprop`` fills in ``_NET_WM_STATE`` (minimized/maximized/fullscreen) and the
  window-manager name.

All tool invocations are bounded by ``driver.window.command_timeout_ms`` and
failures are mapped to :class:`WindowNotFound` / :class:`WindowActionFailed`
(with the useful details callers need to debug).
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import Literal

from ..errors import InvalidArgument, WindowActionFailed, WindowNotFound
from ..models import Rect, WindowInfo, WindowSelector
from .base import DriverError

LOGGER = logging.getLogger(__name__)

ToolError = Literal["action", "driver"]

_WMCTRL_ID_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
_XPROP_LINE_RE = re.compile(r"^(?P<name>[A-Za-z0-9_]+)(?:\((?P<type>[^)]*)\))?\s*=\s*(?P<value>.*)$")
_XPROP_MISSING_RE = re.compile(r"^(?P<name>[A-Za-z0-9_]+):\s+not found\.?$")
_ACTIVE_WINDOW_RE = re.compile(r"window id # (?P<id>0x[0-9a-fA-F]+)")

# ---------------------------------------------------------------------------
# Pure parsing helpers (unit-tested without an X server)
# ---------------------------------------------------------------------------
def normalize_window_id(value: str) -> str:
    """Normalise an X11 window id to the canonical lowercase ``0x...`` form."""

    if not isinstance(value, str):
        raise InvalidArgument(f"window id must be a string, got {type(value).__name__}")
    candidate = value.strip().lower()
    if not candidate:
        raise InvalidArgument("window id must not be empty")
    if candidate.startswith("0x"):
        candidate = candidate[2:]
    if not candidate or not all(ch in "0123456789abcdef" for ch in candidate):
        raise InvalidArgument(
            f"invalid window id {value!r} (expected hexadecimal, e.g. '0x3a00007')"
        )
    return hex(int(candidate, 16))


def parse_xprop(output: str) -> dict[str, str]:
    """Parse ``xprop`` output into ``{property_name: raw_value}``.

    String values are unquoted. Missing properties are mapped to an empty string
    so callers can distinguish "queried but absent" from "not queried".
    """

    result: dict[str, str] = {}
    current: str | None = None
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        match = _XPROP_LINE_RE.match(line)
        if match:
            name = match.group("name")
            value = match.group("value").strip()
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                value = value[1:-1]
            result[name] = value
            current = name
            continue
        missing = _XPROP_MISSING_RE.match(line)
        if missing:
            result[missing.group("name")] = ""
            current = None
            continue
        if current is not None and line[:1].isspace():
            # Continuation of a wrapped value (xprop wraps long atom lists).
            result[current] = f"{result[current]},{line.strip()}"
    return result


def parse_wmctrl_list(output: str) -> list[WindowInfo]:
    """Parse ``wmctrl -l -p -G -x`` into :class:`WindowInfo` records."""

    windows: list[WindowInfo] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        parts = line.split(None, 9)
        if len(parts) < 9 or not _WMCTRL_ID_RE.match(parts[0]):
            LOGGER.debug("window: skipping unparsable wmctrl line: %r", line)
            continue
        (
            window_id,
            desktop_raw,
            pid_raw,
            x_raw,
            y_raw,
            width_raw,
            height_raw,
            wm_class,
            _host,
        ) = parts[:9]
        title = parts[9] if len(parts) > 9 else ""

        instance_name: str | None = None
        class_name: str | None = None
        if wm_class and wm_class != "N/A":
            if "." in wm_class:
                instance_name, _, class_name = wm_class.partition(".")
            else:
                class_name = wm_class
        try:
            desktop: int | None = int(desktop_raw)
        except ValueError:
            desktop = None
        try:
            pid: int | None = int(pid_raw)
        except ValueError:
            pid = None
        if desktop is not None and desktop < 0:
            desktop = None
        if pid is not None and pid <= 0:
            pid = None
        try:
            geometry = Rect(
                x=int(x_raw),
                y=int(y_raw),
                width=max(1, int(width_raw)),
                height=max(1, int(height_raw)),
            )
        except ValueError:
            LOGGER.debug("window: skipping window %s with invalid geometry", window_id)
            continue
        windows.append(
            WindowInfo(
                id=normalize_window_id(window_id),
                title=title,
                class_name=class_name or None,
                instance_name=instance_name or None,
                pid=pid,
                desktop=desktop,
                geometry=geometry,
                state="unknown",
            )
        )
    return windows


def state_from_atoms(atoms: list[str]) -> str:
    """Map a ``_NET_WM_STATE`` atom list onto the ``WindowInfo.state`` literal."""

    normalised = {atom.strip() for atom in atoms if atom.strip()}
    if "_NET_WM_STATE_FULLSCREEN" in normalised:
        return "fullscreen"
    if "_NET_WM_STATE_HIDDEN" in normalised:
        return "minimized"
    if (
        "_NET_WM_STATE_MAXIMIZED_VERT" in normalised
        and "_NET_WM_STATE_MAXIMIZED_HORZ" in normalised
    ):
        return "maximized"
    return "normal"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
class X11WindowDriver:
    """EWMH window enumeration and manipulation through wmctrl/xdotool/xprop."""

    def __init__(
        self,
        *,
        backend: str = "xdotool",
        command_timeout_s: float = 5.0,
        wmctrl_binary: str = "wmctrl",
        xdotool_binary: str = "xdotool",
        xprop_binary: str = "xprop",
    ) -> None:
        if backend not in {"xdotool", "wmctrl"}:
            raise DriverError(f"invalid window backend {backend!r}", driver="window")
        self.backend = backend
        self.command_timeout_s = float(command_timeout_s)
        self._wmctrl = wmctrl_binary
        self._xdotool = xdotool_binary
        self._xprop = xprop_binary

    # -- tool execution ----------------------------------------------------
    def _run_tool(
        self,
        args: list[str],
        *,
        what: str,
        error: ToolError = "action",
        allow_failure: bool = False,
        timeout_s: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        timeout = self.command_timeout_s if timeout_s is None else timeout_s
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                # wmctrl/xprop emit titles in the *user's* locale and may emit
                # invalid UTF-8 for legacy clients; decoding explicitly keeps a
                # non-ASCII title from crashing the whole enumeration under a
                # C/POSIX locale (Python otherwise uses the locale codec).
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DriverError(
                f"window driver requires {args[0]!r}, which is not installed",
                driver="window",
                details={"command": args[0]},
            ) from exc
        except subprocess.TimeoutExpired as exc:
            if error == "driver":
                raise DriverError(
                    f"{what} timed out after {timeout:.1f}s", driver="window"
                ) from exc
            raise WindowActionFailed(
                f"{what} timed out after {timeout:.1f}s",
                details={"command": args, "timeout_s": timeout},
            ) from exc
        except OSError as exc:
            raise DriverError(
                f"{what} failed to start: {exc}", driver="window", details={"command": args}
            ) from exc
        if proc.returncode != 0 and not allow_failure:
            message = (
                f"{what} failed with exit code {proc.returncode}"
                f": {proc.stderr.strip() or proc.stdout.strip() or 'no output'}"
            )
            if error == "driver":
                raise DriverError(message, driver="window", details={"command": args})
            raise WindowActionFailed(
                message,
                details={
                    "command": args,
                    "returncode": proc.returncode,
                    "stderr": proc.stderr.strip()[-500:],
                },
            )
        return proc

    # -- enumeration -------------------------------------------------------
    def list_windows(self) -> list[WindowInfo]:
        # wmctrl can abort with "BadWindow" when a window is destroyed between
        # its tree walk and the per-window property fetches (common while
        # windows open/close); retry the enumeration before giving up.
        for attempt in range(3):
            try:
                proc = self._run_tool(
                    [self._wmctrl, "-l", "-p", "-G", "-x"],
                    what="window list (wmctrl -l -p -G -x)",
                    error="driver",
                )
                break
            except DriverError as exc:
                # Only the known-race X error is retried; all other failures
                # (missing wmctrl, no window manager, ...) surface immediately.
                message = str(exc)
                if attempt == 2 or "BadWindow" not in message:
                    raise
                LOGGER.debug(
                    "window list hit a BadWindow race (%s); retrying", message
                )
                time.sleep(0.05)
        windows = parse_wmctrl_list(proc.stdout)
        active_id = self._active_window_id()
        for window in windows:
            window.active = window.id == active_id
            window.state = self._window_state(window.id)
        return windows

    def active_window(self) -> WindowInfo | None:
        for window in self.list_windows():
            if window.active:
                return window
        return None

    def _active_window_id(self) -> str | None:
        proc = self._run_tool(
            [self._xprop, "-root", "_NET_ACTIVE_WINDOW"],
            what="active window probe (xprop -root _NET_ACTIVE_WINDOW)",
            error="driver",
            allow_failure=True,
        )
        if proc.returncode != 0:
            return None
        match = _ACTIVE_WINDOW_RE.search(proc.stdout)
        if not match:
            return None
        window_id = normalize_window_id(match.group("id"))
        return None if window_id == "0x0" else window_id

    def _window_state(self, window_id: str) -> str:
        proc = self._run_tool(
            [self._xprop, "-id", window_id, "_NET_WM_STATE"],
            what=f"window state probe (xprop -id {window_id} _NET_WM_STATE)",
            error="driver",
            allow_failure=True,
        )
        if proc.returncode != 0:
            return "unknown"
        properties = parse_xprop(proc.stdout)
        if "_NET_WM_STATE" not in properties:
            return "unknown"
        atoms = [atom for atom in properties["_NET_WM_STATE"].split(",") if atom.strip()]
        return state_from_atoms(atoms)

    def get_window(self, selector: WindowSelector) -> WindowInfo:
        if selector.is_empty():
            raise InvalidArgument(
                "window selector requires at least one of id/title/class_name/"
                "instance_name/active"
            )
        title_pattern: re.Pattern[str] | None = None
        if selector.title is not None and selector.title_regex:
            try:
                title_pattern = re.compile(selector.title)
            except re.error as exc:
                raise InvalidArgument(
                    f"invalid window title regular expression {selector.title!r}: {exc}"
                ) from exc
        wanted_id = normalize_window_id(selector.id) if selector.id else None
        windows = self.list_windows()
        matches = [
            window
            for window in windows
            if self._matches(
                window,
                selector,
                wanted_id=wanted_id,
                title_pattern=title_pattern,
            )
        ]
        if not matches:
            raise WindowNotFound(
                f"no window matches selector {selector.model_dump(exclude_none=True)}",
                details={
                    "selector": selector.model_dump(exclude_none=True),
                    "window_count": len(windows),
                    "candidates": [
                        {
                            "id": window.id,
                            "title": window.title,
                            "class_name": window.class_name,
                        }
                        for window in windows[:5]
                    ],
                },
            )
        return matches[0]

    @staticmethod
    def _matches(
        window: WindowInfo,
        selector: WindowSelector,
        *,
        wanted_id: str | None,
        title_pattern: re.Pattern[str] | None,
    ) -> bool:
        if wanted_id is not None and window.id != wanted_id:
            return False
        if selector.title is not None:
            if title_pattern is not None:
                if not title_pattern.search(window.title):
                    return False
            elif window.title != selector.title:
                return False
        if selector.class_name is not None and (
            window.class_name or ""
        ).casefold() != selector.class_name.casefold():
            return False
        if selector.instance_name is not None and (
            window.instance_name or ""
        ).casefold() != selector.instance_name.casefold():
            return False
        return window.active or not selector.active

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _window_label(window: WindowInfo) -> str:
        return window.title or window.id

    def _activate(self, window: WindowInfo) -> None:
        attempts: list[list[str]] = []
        if self.backend == "xdotool":
            attempts.append([self._xdotool, "windowactivate", "--sync", window.id])
        attempts.append([self._wmctrl, "-i", "-a", window.id])
        self._try_commands(attempts, what=f"activate window {self._window_label(window)!r}")

    def _try_commands(self, attempts: list[list[str]], *, what: str) -> None:
        errors: list[str] = []
        for args in attempts:
            try:
                self._run_tool(args, what=what, allow_failure=False)
                return
            except WindowActionFailed as exc:
                errors.append(f"{args[0]}: {exc}")
                LOGGER.debug("%s via %s failed: %s", what, args[0], exc)
        raise WindowActionFailed(what, details={"errors": errors})

    # -- mutations ---------------------------------------------------------
    def activate(self, window: WindowInfo) -> None:
        self._activate(window)

    def move(self, window: WindowInfo, x: int, y: int) -> None:
        if self.backend == "xdotool":
            attempts = [
                [self._xdotool, "windowmove", window.id, str(int(x)), str(int(y))],
                [self._wmctrl, "-i", "-r", window.id, "-e", f"0,{int(x)},{int(y)},-1,-1"],
            ]
        else:
            attempts = [
                [self._wmctrl, "-i", "-r", window.id, "-e", f"0,{int(x)},{int(y)},-1,-1"],
                [self._xdotool, "windowmove", window.id, str(int(x)), str(int(y))],
            ]
        self._try_commands(attempts, what=f"move window {self._window_label(window)!r}")

    def resize(self, window: WindowInfo, width: int, height: int) -> None:
        if int(width) < 1 or int(height) < 1:
            raise InvalidArgument("window width and height must be >= 1")
        if self.backend == "xdotool":
            attempts = [
                [self._xdotool, "windowsize", window.id, str(int(width)), str(int(height))],
                [self._wmctrl, "-i", "-r", window.id, "-e", f"0,-1,-1,{int(width)},{int(height)}"],
            ]
        else:
            attempts = [
                [self._wmctrl, "-i", "-r", window.id, "-e", f"0,-1,-1,{int(width)},{int(height)}"],
                [self._xdotool, "windowsize", window.id, str(int(width)), str(int(height))],
            ]
        self._try_commands(attempts, what=f"resize window {self._window_label(window)!r}")

    def move_resize(self, window: WindowInfo, x: int, y: int, width: int, height: int) -> None:
        if int(width) < 1 or int(height) < 1:
            raise InvalidArgument("window width and height must be >= 1")
        what = f"move/resize window {self._window_label(window)!r}"
        try:
            self._run_tool(
                [
                    self._wmctrl,
                    "-i",
                    "-r",
                    window.id,
                    "-e",
                    f"0,{int(x)},{int(y)},{int(width)},{int(height)}",
                ],
                what=what,
            )
            return
        except WindowActionFailed as exc:
            LOGGER.debug("move_resize via wmctrl failed: %s", exc)
        self._run_tool(
            [self._xdotool, "windowmove", window.id, str(int(x)), str(int(y))], what=what
        )
        self._run_tool(
            [self._xdotool, "windowsize", window.id, str(int(width)), str(int(height))],
            what=what,
        )

    def minimize(self, window: WindowInfo) -> None:
        if self.backend == "xdotool":
            attempts = [
                [self._xdotool, "windowminimize", window.id],
                [self._wmctrl, "-i", "-r", window.id, "-b", "add,hidden"],
            ]
        else:
            attempts = [
                [self._wmctrl, "-i", "-r", window.id, "-b", "add,hidden"],
                [self._xdotool, "windowminimize", window.id],
            ]
        self._try_commands(attempts, what=f"minimize window {self._window_label(window)!r}")

    def maximize(self, window: WindowInfo) -> None:
        attempts = [
            [
                self._wmctrl,
                "-i",
                "-r",
                window.id,
                "-b",
                "add,maximized_vert,maximized_horz",
            ],
            [self._wmctrl, "-i", "-r", window.id, "-b", "toggle,maximized_vert,maximized_horz"],
        ]
        self._try_commands(attempts, what=f"maximize window {self._window_label(window)!r}")

    def restore(self, window: WindowInfo) -> None:
        attempts = [
            [
                self._wmctrl,
                "-i",
                "-r",
                window.id,
                "-b",
                "remove,maximized_vert,maximized_horz",
            ],
        ]
        self._try_commands(attempts, what=f"restore window {self._window_label(window)!r}")
        # Removing the maximized state does not de-iconify a minimized window.
        self._run_tool(
            [self._wmctrl, "-i", "-a", window.id],
            what=f"activate restored window {self._window_label(window)!r}",
            allow_failure=True,
        )

    def close(self, window: WindowInfo) -> None:
        attempts = [
            [self._wmctrl, "-i", "-c", window.id],
            [self._xdotool, "windowclose", window.id],
        ]
        self._try_commands(attempts, what=f"close window {self._window_label(window)!r}")

    # -- window manager ----------------------------------------------------
    def window_manager_name(self) -> str | None:
        proc = self._run_tool(
            [self._wmctrl, "-m"],
            what="window manager probe (wmctrl -m)",
            error="driver",
            allow_failure=True,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if line.lower().startswith("name:"):
                    name = line.partition(":")[2].strip()
                    if name:
                        return name
        # Fall back to the EWMH supporting-WM check.
        proc = self._run_tool(
            [self._xprop, "-root", "_NET_SUPPORTING_WM_CHECK"],
            what="window manager probe (xprop _NET_SUPPORTING_WM_CHECK)",
            error="driver",
            allow_failure=True,
        )
        match = re.search(r"window id # (?P<id>0x[0-9a-fA-F]+)", proc.stdout)
        if not match:
            return None
        wm_id = normalize_window_id(match.group("id"))
        if wm_id == "0x0":
            return None
        proc = self._run_tool(
            [self._xprop, "-id", wm_id, "_NET_WM_NAME"],
            what="window manager name probe",
            error="driver",
            allow_failure=True,
        )
        properties = parse_xprop(proc.stdout)
        name = properties.get("_NET_WM_NAME") or ""
        return name or None


__all__ = [
    "X11WindowDriver",
    "normalize_window_id",
    "parse_wmctrl_list",
    "parse_xprop",
    "state_from_atoms",
]
