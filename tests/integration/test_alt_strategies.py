"""End-to-end tests for the alternate strategy providers (container only).

These tests need a real X11 desktop (Xvfb + XFCE) and the optional strategy
dependencies installed, so they are only collected when the explicit guard is
present:

    NAD_INTEGRATION=1 DISPLAY=:99 pytest -s tests/integration/test_alt_strategies.py

They are *not* run by the normal headless unit-test job. The test-side drivers
are intentionally minimal PyAutoGUI adapters implementing the same
``MouseDriver`` / ``KeyboardDriver`` protocols the production driver layer uses,
so the strategies are exercised through the real driver contract and real X11
input rather than through mocks.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

if os.environ.get("NAD_INTEGRATION") != "1" or not os.environ.get("DISPLAY"):
    pytest.skip(
        "integration tests require NAD_INTEGRATION=1 and a running X11 DISPLAY",
        allow_module_level=True,
    )

pyautogui = pytest.importorskip("pyautogui")
# The strategy layer owns the pacing; PyAutoGUI must not add its own 0.1 s per
# call.
# The production PyAutoGUI driver also sets MINIMUM_DURATION=0.0 so short
# driver-level interpolation hints (WindMouse step_duration_s) are honoured.
pyautogui.PAUSE = 0.0
pyautogui.MINIMUM_DURATION = 0.0

from nextautodesk.drivers.base import DriverBundle  # noqa: E402
from nextautodesk.models import Point  # noqa: E402
from nextautodesk.operations import Operation  # noqa: E402
from nextautodesk.strategies.base import OperationRequest, StrategyContext  # noqa: E402
from nextautodesk.strategies.providers.clack import ClackProvider  # noqa: E402
from nextautodesk.strategies.providers.human_move_mouse import HumanMoveMouseProvider  # noqa: E402
from nextautodesk.strategies.providers.windmouse import WindMouseProvider  # noqa: E402

LOGGER = logging.getLogger("tests.integration.alt_strategies")

pytestmark = pytest.mark.integration

TERMINAL_TITLE = "NAD_CLACK_E2E"
CLACK_OK_COMMAND = "printf '%s' 'CLACK_OK' > /tmp/clack_typed.txt"
CLACK_FIXED_COMMAND = "printf '%s' 'CLACK_FIXED' > /tmp/clack_fixed.txt"


# ---------------------------------------------------------------------------
# minimal PyAutoGUI driver adapters
# ---------------------------------------------------------------------------
class PyAutoGUIMouse:
    """``MouseDriver`` implementation used by the E2E tests."""

    def __init__(self) -> None:
        self.moves: list[tuple[float, float, float]] = []

    def position(self) -> Point:
        pos = pyautogui.position()
        return Point(x=float(pos.x), y=float(pos.y))

    def move_to(self, x: float, y: float, duration_s: float = 0.0) -> None:
        self.moves.append((float(x), float(y), float(duration_s)))
        pyautogui.moveTo(x, y, duration=duration_s)

    def button_down(self, button: str) -> None:  # pragma: no cover - unused here
        pyautogui.mouseDown(button=button)

    def button_up(self, button: str) -> None:  # pragma: no cover - unused here
        pyautogui.mouseUp(button=button)

    def scroll(self, dx: int, dy: int) -> None:  # pragma: no cover - unused here
        pyautogui.scroll(dy)

    def buttons_down(self) -> set[str]:  # pragma: no cover - unused here
        return set()


class PyAutoGUIKeyboard:
    """``KeyboardDriver`` implementation used by the E2E tests."""

    def __init__(self) -> None:
        self.typed: list[str] = []
        self.keys: list[tuple[str, str]] = []

    def key_down(self, key: str) -> None:
        self.keys.append(("down", key))
        pyautogui.keyDown(key)

    def key_up(self, key: str) -> None:
        self.keys.append(("up", key))
        pyautogui.keyUp(key)

    def type_text(self, text: str) -> None:
        self.typed.append(text)
        pyautogui.write(text)

    def char_down(self, char: str) -> None:  # pragma: no cover - unused here
        pyautogui.keyDown(char)

    def char_up(self, char: str) -> None:  # pragma: no cover - unused here
        pyautogui.keyUp(char)

    def supports_unicode(self) -> bool:
        return False


def make_context(
    *, position: tuple[float, float] | None = None
) -> tuple[StrategyContext, PyAutoGUIMouse, PyAutoGUIKeyboard]:
    mouse = PyAutoGUIMouse()
    keyboard = PyAutoGUIKeyboard()
    bundle = DriverBundle(mouse=mouse, keyboard=keyboard, screen=object(), window=object())
    ctx = StrategyContext(drivers=bundle, cancel=threading.Event(), logger=LOGGER)
    if position is not None:
        pyautogui.moveTo(position[0], position[1], duration=0)
    return ctx, mouse, keyboard


def require_provider_available(provider: object) -> None:
    availability = provider.availability()  # type: ignore[attr-defined]
    if not availability.available:
        pytest.skip(f"{provider.name} unavailable: {availability.reason}")  # type: ignore[attr-defined]


def wait_for_file(path: Path, expected: str, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        if path.is_file():
            last = path.read_text(encoding="utf-8", errors="replace")
            if last == expected:
                return last
        time.sleep(0.1)
    return last


# ---------------------------------------------------------------------------
# desktop / terminal fixtures
# ---------------------------------------------------------------------------
def _run(command: list[str], *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture(scope="module", autouse=True)
def desktop_ready() -> None:
    """Skip the whole module unless real X11 input works."""

    try:
        pyautogui.position()
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"X11 display is not usable: {exc}")
    missing = [tool for tool in ("xdotool", "xfce4-terminal") if not _which(tool)]
    if missing:  # pragma: no cover - environment guard
        pytest.skip(f"missing desktop tools: {missing}")


def _which(tool: str) -> bool:
    return any((Path(directory) / tool).is_file() for directory in os.environ.get("PATH", "").split(":"))


def _find_window(title: str, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _run(["xdotool", "search", "--name", title])
        ids = result.stdout.split()
        if ids:
            return ids[-1]
        time.sleep(0.2)
    raise AssertionError(f"window {title!r} did not appear within {timeout}s")


def _focus_terminal(window_id: str) -> None:
    _run(["xdotool", "windowactivate", "--sync", window_id])
    _run(["xdotool", "windowfocus", "--sync", window_id])
    geometry = _run(["xdotool", "getwindowgeometry", "--shell", window_id]).stdout
    values: dict[str, int] = {}
    for line in geometry.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            try:
                values[key.strip()] = int(value.strip())
            except ValueError:
                continue
    if {"X", "Y", "WIDTH", "HEIGHT"} <= values.keys():
        pyautogui.click(
            values["X"] + values["WIDTH"] // 2,
            values["Y"] + values["HEIGHT"] // 2,
        )
    time.sleep(1.0)


@pytest.fixture(scope="module")
def terminal() -> Iterator[str]:
    """A dedicated xfce4-terminal window receiving the injected keystrokes."""

    env = os.environ.copy()
    process = subprocess.Popen(
        [
            "xfce4-terminal",
            "--disable-server",
            "--title",
            TERMINAL_TITLE,
            "--geometry=120x30+40+40",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    window_id = ""
    try:
        window_id = _find_window(TERMINAL_TITLE)
        _focus_terminal(window_id)
        yield window_id
    finally:
        if window_id:
            _run(["xdotool", "windowkill", window_id])
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn terminal
            process.kill()


# ---------------------------------------------------------------------------
# HumanMoveMouse E2E
# ---------------------------------------------------------------------------
def test_human_move_mouse_e2e() -> None:
    provider = HumanMoveMouseProvider()
    require_provider_available(provider)

    target = (640.0, 360.0)
    ctx, mouse, _ = make_context(position=(80.0, 80.0))
    started = time.monotonic()
    outcome = provider.execute(
        OperationRequest(operation=Operation.MOUSE_MOVE, x=target[0], y=target[1]),
        {"num_points": 80, "jitter_amplitude": 0.3, "speed_factor": 2.5, "seed": 7},
        ctx,
    )
    wall_duration = time.monotonic() - started

    final = pyautogui.position()
    xdotool_location = _run(["xdotool", "getmouselocation"]).stdout.strip()
    distance_to_target = math.hypot(final.x - target[0], final.y - target[1])
    move_points = len(mouse.moves)
    distinct_points = len({(round(x, 1), round(y, 1)) for x, y, _ in mouse.moves})
    path_length = float(outcome.details["path_length_px"])

    print(
        "[E2E human_move_mouse] "
        f"points={move_points} distinct={distinct_points} "
        f"path_px={path_length:.1f} final=({final.x},{final.y}) "
        f"distance_to_target={distance_to_target:.2f}px xdotool={xdotool_location!r} "
        f"wall={wall_duration:.2f}s outcome={outcome.duration_ms:.0f}ms"
    )
    assert move_points == 80
    assert distinct_points > 20
    assert path_length > 200.0
    assert distance_to_target <= 6.0


# ---------------------------------------------------------------------------
# WindMouse E2E
# ---------------------------------------------------------------------------
def test_windmouse_e2e() -> None:
    provider = WindMouseProvider()
    require_provider_available(provider)

    target = (760.0, 420.0)
    ctx, mouse, _ = make_context(position=(120.0, 120.0))
    started = time.monotonic()
    outcome = provider.execute(
        OperationRequest(operation=Operation.MOUSE_MOVE, x=target[0], y=target[1]),
        {"step_duration_s": 0.1, "seed": 42},
        ctx,
    )
    wall_duration = time.monotonic() - started

    final = pyautogui.position()
    xdotool_location = _run(["xdotool", "getmouselocation"]).stdout.strip()
    distance_to_target = math.hypot(final.x - target[0], final.y - target[1])
    points = int(outcome.details["points_yielded"])
    path_length = float(outcome.details["path_length_px"])

    print(
        "[E2E windmouse] "
        f"points={points} path_px={path_length:.1f} final=({final.x},{final.y}) "
        f"distance_to_target={distance_to_target:.2f}px xdotool={xdotool_location!r} "
        f"wall={wall_duration:.2f}s outcome={outcome.duration_ms:.0f}ms"
    )
    assert points > 20
    assert len(mouse.moves) == points
    assert all(move[2] == 0.1 for move in mouse.moves)
    assert path_length > 300.0
    assert distance_to_target <= 3.0


# ---------------------------------------------------------------------------
# Clack E2E: type a shell command into xfce4-terminal, press Enter, verify file
# ---------------------------------------------------------------------------
def test_clack_types_command_without_typos(terminal: str) -> None:
    provider = ClackProvider()
    require_provider_available(provider)
    output = Path("/tmp/clack_typed.txt")
    output.unlink(missing_ok=True)

    ctx, _, keyboard = make_context()
    started = time.monotonic()
    outcome = provider.execute(
        # The trailing newline travels through Clack (0x0a -> Enter); with
        # error_rate=0 the CLI cannot emit a premature newline mid-command.
        OperationRequest(operation=Operation.KEYBOARD_TYPE_TEXT, text=CLACK_OK_COMMAND + "\n"),
        {
            "wpm": 200,
            "error_rate": 0.0,
            "jitter": 0.15,
            "no_fatigue": True,
            "seed": 42,
            "max_pause_ms": 1500,
            "thinking_pause_prob": 0.0,
            "startup_timeout_s": 15,
            "char_timeout_s": 20,
        },
        ctx,
    )
    content = wait_for_file(output, "CLACK_OK", timeout=20.0)
    wall_duration = time.monotonic() - started

    print(
        "[E2E clack no-typos] "
        f"typed={outcome.details['chars_typed']} backspaces={outcome.details['backspaces']} "
        f"enters={outcome.details['enters']} output_bytes={outcome.details['output_bytes']} "
        f"wall={wall_duration:.2f}s outcome={outcome.duration_ms:.0f}ms file={content!r}"
    )
    assert output.is_file(), "terminal command did not create the output file"
    assert content == "CLACK_OK"
    assert outcome.details["chars_typed"] == len(CLACK_OK_COMMAND)
    assert outcome.details["backspaces"] == 0
    assert outcome.details["enters"] == 1
    # The command itself is typed through the driver, one character at a time.
    assert len(keyboard.typed) == len(CLACK_OK_COMMAND)


def test_clack_typo_correction_still_produces_correct_command(terminal: str) -> None:
    provider = ClackProvider()
    require_provider_available(provider)
    output = Path("/tmp/clack_fixed.txt")
    output.unlink(missing_ok=True)

    ctx, _, keyboard = make_context()
    started = time.monotonic()
    outcome = provider.execute(
        # No embedded newline in the typo stream: Clack's correction model may
        # emit 0x0a inside a typo region, but a terminal executes Enter
        # immediately and backspaces cannot undo an executed newline (documented
        # limitation in docs/strategies.md). Submit with the harness
        # Enter after the corrected command line has been typed.
        OperationRequest(operation=Operation.KEYBOARD_TYPE_TEXT, text=CLACK_FIXED_COMMAND),
        {
            "wpm": 240,
            "error_rate": 1.0,
            "correction_rate": 1.0,
            "jitter": 0.15,
            "no_fatigue": True,
            "seed": 123,
            "max_pause_ms": 1000,
            "thinking_pause_prob": 0.0,
            "startup_timeout_s": 15,
            "char_timeout_s": 20,
        },
        ctx,
    )
    pyautogui.press("enter")
    content = wait_for_file(output, "CLACK_FIXED", timeout=30.0)
    wall_duration = time.monotonic() - started

    print(
        "[E2E clack typo-correction] "
        f"typed={outcome.details['chars_typed']} backspaces={outcome.details['backspaces']} "
        f"enters={outcome.details['enters']} output_bytes={outcome.details['output_bytes']} "
        f"wall={wall_duration:.2f}s outcome={outcome.duration_ms:.0f}ms file={content!r}"
    )
    assert output.is_file(), "typo-corrected terminal command did not create the output file"
    assert content == "CLACK_FIXED"
    assert outcome.details["backspaces"] > 0, "error_rate=1.0 must generate correction backspaces"
    assert outcome.details["enters"] == 0
    assert sum(1 for direction, key in keyboard.keys if direction == "down" and key == "backspace") > 0
