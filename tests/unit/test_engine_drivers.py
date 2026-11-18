"""Unit tests for the X11 driver layer (no X server required)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image
from test_engine_fakes import make_window

from nextautodesk.drivers import factory
from nextautodesk.drivers.base import DriverError
from nextautodesk.drivers.pyautogui_x11 import (
    PyAutoGuiKeyboardDriver,
    PyAutoGuiMouseDriver,
    _X11Keyboard,
    buttons_from_mask,
    char_to_keysym,
    normalize_key_name,
)
from nextautodesk.drivers.screen_x11 import X11ScreenDriver
from nextautodesk.drivers.window_x11 import (
    X11WindowDriver,
    normalize_window_id,
    parse_wmctrl_list,
    parse_xprop,
    state_from_atoms,
)
from nextautodesk.errors import InvalidArgument, WindowActionFailed, WindowNotFound
from nextautodesk.models import Rect, WindowSelector


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ctrl", "ctrl"),
        ("Control", "ctrl"),
        ("ENTER", "enter"),
        ("return", "enter"),
        ("escape", "esc"),
        ("pageup", "pageup"),
        ("pgdn", "pagedown"),
        ("arrowup", "up"),
        ("Arrow_Down", "down"),
        ("f12", "f12"),
        ("space", "space"),
        ("A", "a"),
        (" ", "space"),
        ("\n", "enter"),
        ("+", "+"),
        ("win", "win"),
        ("super", "win"),
    ],
)
def test_normalize_key_name(raw: str, expected: str) -> None:
    assert normalize_key_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "  ", "not-a-key", "中", "\x01"])
def test_normalize_key_name_rejects_invalid(raw: str) -> None:
    with pytest.raises(DriverError):
        normalize_key_name(raw)


@pytest.mark.parametrize(
    ("char", "keysym"),
    [
        ("a", 0x61),
        ("A", 0x41),
        ("!", 0x21),
        ("é", 0xE9),
        ("\n", 0xFF0D),
        ("\t", 0xFF09),
        ("中", 0x01004E2D),
        ("😀", 0x0101F600),
    ],
)
def test_char_to_keysym(char: str, keysym: int) -> None:
    assert char_to_keysym(char) == keysym


@pytest.mark.parametrize("char", ["\x01", "\x7f", "\ud800", "", "ab"])
def test_char_to_keysym_rejects_invalid(char: str) -> None:
    with pytest.raises(DriverError):
        char_to_keysym(char)


def test_buttons_from_mask() -> None:
    assert buttons_from_mask(0) == set()
    assert buttons_from_mask((1 << 8) | (1 << 10)) == {"left", "right"}
    assert buttons_from_mask(1 << 9) == {"middle"}


def test_window_id_normalisation() -> None:
    assert normalize_window_id("0x03A00007") == "0x3a00007"
    assert normalize_window_id("3a00007") == "0x3a00007"
    assert normalize_window_id("0x0") == "0x0"
    for bad in ["", "zzz", "0x", "nope"]:
        with pytest.raises(InvalidArgument):
            normalize_window_id(bad)


def test_parse_wmctrl_list() -> None:
    output = (
        "0x03a00007  0 2918   100  50  800  600  "
        "xfce4-terminal.Xfce4-terminal  nad-host Terminal - nad\n"
        "0x04a00003 -1 -1     0   0 1920 1080  N/A  nad-host Desktop\n"
    )
    windows = parse_wmctrl_list(output)
    assert len(windows) == 2
    first, second = windows
    assert first.id == "0x3a00007"
    assert first.title == "Terminal - nad"
    assert first.instance_name == "xfce4-terminal"
    assert first.class_name == "Xfce4-terminal"
    assert first.pid == 2918
    assert first.desktop == 0
    assert first.geometry == Rect(x=100, y=50, width=800, height=600)
    assert second.class_name is None and second.pid is None
    assert second.desktop is None


def test_parse_xprop() -> None:
    output = (
        '_NET_WM_NAME(UTF8_STRING) = "Terminal - nad"\n'
        'WM_CLASS(STRING) = "xfce4-terminal", "Xfce4-terminal"\n'
        "_NET_WM_STATE(ATOM) = _NET_WM_STATE_MAXIMIZED_VERT, "
        "_NET_WM_STATE_MAXIMIZED_HORZ\n"
        "_NET_WM_PID(CARDINAL) = 2918\n"
        "_NET_WM_STATE_HIDDEN:  not found.\n"
    )
    parsed = parse_xprop(output)
    assert parsed["_NET_WM_NAME"] == "Terminal - nad"
    assert parsed["_NET_WM_PID"] == "2918"
    assert "_NET_WM_STATE_MAXIMIZED_VERT" in parsed["_NET_WM_STATE"]
    assert parsed["_NET_WM_STATE_HIDDEN"] == ""


@pytest.mark.parametrize(
    ("atoms", "expected"),
    [
        ([], "normal"),
        (["_NET_WM_STATE_MAXIMIZED_VERT"], "normal"),
        (
            ["_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ"],
            "maximized",
        ),
        (["_NET_WM_STATE_HIDDEN"], "minimized"),
        (["_NET_WM_STATE_FULLSCREEN"], "fullscreen"),
    ],
)
def test_state_from_atoms(atoms: list[str], expected: str) -> None:
    assert state_from_atoms(atoms) == expected


# ---------------------------------------------------------------------------
# PyAutoGUI is only imported lazily (headless import safety)
# ---------------------------------------------------------------------------
def test_pyautogui_import_is_lazy() -> None:
    code = (
        "import sys\n"
        "import nextautodesk.drivers.pyautogui_x11\n"
        "import nextautodesk.drivers.screen_x11\n"
        "import nextautodesk.drivers.factory\n"
        "assert 'pyautogui' not in sys.modules, 'pyautogui imported eagerly'\n"
        "print('lazy-ok')\n"
    )
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    # pytest's `pythonpath = ["src"]` only applies to the pytest process; make
    # the child process resolve the working tree too.
    src_root = str(Path(factory.__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (src_root, env.get("PYTHONPATH")))
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "lazy-ok" in proc.stdout


# ---------------------------------------------------------------------------
# Mouse driver
# ---------------------------------------------------------------------------
class FakePage:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.FAILSAFE = True
        self.PAUSE = 1.0
        self.MINIMUM_DURATION = 1.0
        self.KEYBOARD_KEYS = [*"abcdefghijklmnopqrstuvwxyz0123456789", "enter", "space", " "]
        self._pos = (5, 6)

    def position(self):
        return self._pos

    def moveTo(self, x, y, duration=0.0, _pause=True):
        self.calls.append(("moveTo", x, y, duration, _pause))
        self._pos = (x, y)

    def mouseDown(self, button=None, _pause=True):
        self.calls.append(("mouseDown", button))

    def mouseUp(self, button=None, _pause=True):
        self.calls.append(("mouseUp", button))

    def vscroll(self, clicks, _pause=True):
        self.calls.append(("vscroll", clicks))

    def hscroll(self, clicks, _pause=True):
        self.calls.append(("hscroll", clicks))

    def keyDown(self, key, _pause=True):
        self.calls.append(("keyDown", key))

    def keyUp(self, key, _pause=True):
        self.calls.append(("keyUp", key))

    def write(self, text, interval=0.0, _pause=True):
        self.calls.append(("write", text))

    def size(self):
        return (1920, 1080)

    def screenshot(self, region=None):
        self.calls.append(("screenshot", region))
        image = Image.new("RGB", (20, 10), (1, 2, 3))
        return image


@pytest.fixture
def fake_page(monkeypatch: pytest.MonkeyPatch) -> FakePage:
    page = FakePage()

    def _load_pyautogui():
        page.FAILSAFE = False
        page.PAUSE = 0.0
        page.MINIMUM_DURATION = 0.0
        return page

    monkeypatch.setattr("nextautodesk.drivers.pyautogui_x11.load_pyautogui", _load_pyautogui)
    monkeypatch.setattr("nextautodesk.drivers.screen_x11.load_pyautogui", _load_pyautogui)
    return page


def test_mouse_driver_primitives(fake_page: FakePage) -> None:
    driver = PyAutoGuiMouseDriver()
    assert driver.position().as_tuple() == (5.0, 6.0)
    driver.move_to(10.4, 20.6, 0.25)
    assert fake_page.calls[-1] == ("moveTo", 10, 21, 0.25, False)
    driver.button_down("left")
    driver.button_up("left")
    assert fake_page.calls[-2:] == [("mouseDown", "left"), ("mouseUp", "left")]
    fake_page.calls.clear()
    driver.scroll(dx=2, dy=-3)
    assert ("hscroll", 2) in fake_page.calls
    assert ("vscroll", 3) in fake_page.calls


def test_mouse_scroll_sign_mapping(fake_page: FakePage) -> None:
    driver = PyAutoGuiMouseDriver()
    driver.scroll(0, 2)  # +dy scrolls down -> pyautogui vscroll(-2)
    assert fake_page.calls == [("vscroll", -2)]


def test_mouse_driver_rejects_invalid_button(fake_page: FakePage) -> None:
    driver = PyAutoGuiMouseDriver()
    with pytest.raises(DriverError):
        driver.button_down("x1")  # type: ignore[arg-type]


def test_mouse_buttons_down_uses_x11(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeX11:
        def pressed_buttons(self):
            return {"left"}

    driver = PyAutoGuiMouseDriver()
    driver._x11 = FakeX11()  # type: ignore[assignment]
    assert driver.buttons_down() == {"left"}


# ---------------------------------------------------------------------------
# Keyboard driver
# ---------------------------------------------------------------------------
def test_keyboard_named_keys(fake_page: FakePage) -> None:
    driver = PyAutoGuiKeyboardDriver()
    driver.key_down("ctrl")
    driver.key_up("ENTER")
    assert fake_page.calls == [("keyDown", "ctrl"), ("keyUp", "enter")]
    with pytest.raises(DriverError):
        driver.key_down("bogus-key")


def test_keyboard_type_text_splits_ascii_and_unicode(
    fake_page: FakePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = PyAutoGuiKeyboardDriver()
    typed: list[str] = []

    def _fake_unicode(char: str) -> None:
        typed.append(char)

    monkeypatch.setattr(driver, "_type_unicode_char", _fake_unicode)
    driver.type_text("ab中c")
    assert ("write", "ab") in fake_page.calls
    assert ("write", "c") in fake_page.calls
    assert typed == ["中"]


def test_keyboard_type_text_uppercase_uses_shift(fake_page: FakePage) -> None:
    """Uppercase ASCII is not in PyAutoGUI's KEYBOARD_KEYS; use Shift pairs."""

    driver = PyAutoGuiKeyboardDriver()
    driver.type_text("Hello WORLD 123")
    shift_held = False
    typed: list[str] = []
    uppercase_writes: list[str] = []
    for call in fake_page.calls:
        if call[0] == "keyDown":
            assert call[1] == "shift"
            shift_held = True
        elif call[0] == "keyUp":
            assert call[1] == "shift"
            shift_held = False
        elif call[0] == "write":
            if shift_held:
                uppercase_writes.append(call[1])
                typed.append(call[1].upper())
            else:
                typed.append(call[1])
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected pyautogui call {call!r}")
    assert "".join(typed) == "Hello WORLD 123"
    assert uppercase_writes == ["h", "w", "o", "r", "l", "d"]
    assert shift_held is False
    with pytest.raises(DriverError):
        driver.type_text("\x01")


class FakeXlibSession:
    """Duck-typed ``_X11Keyboard`` for state-tracking tests (no X server)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def resolve_char(self, char: str) -> tuple[int, bool, int | None]:
        self.calls.append(("resolve_char", char))
        return 50, char.isupper(), None

    def press(self, keycode: int) -> None:
        self.calls.append(("press", keycode))

    def release(self, keycode: int) -> None:
        self.calls.append(("release", keycode))

    def press_shift(self) -> None:
        self.calls.append(("press_shift",))

    def release_shift(self) -> None:
        self.calls.append(("release_shift",))

    def release_spare(self, spare: int) -> None:
        self.calls.append(("release_spare", spare))


def test_keyboard_type_text_keeps_caller_held_shift(fake_page: FakePage) -> None:
    """A key_down("shift") from the state primitive must not be released."""

    driver = PyAutoGuiKeyboardDriver()
    driver.key_down("shift")
    fake_page.calls.clear()
    try:
        driver.type_text("A")
        calls = list(fake_page.calls)
    finally:
        driver.key_up("shift")
    # With Shift already down, writing the unshifted key already produces "A";
    # an extra shift down/up pair would release the caller's modifier.
    assert calls == [("write", "a")]
    assert driver._held_keys == set()


def test_keyboard_char_events_share_held_shift(fake_page: FakePage) -> None:
    driver = PyAutoGuiKeyboardDriver()
    session = FakeXlibSession()
    driver._x11 = session  # type: ignore[assignment]

    # Two Shift-requiring characters held at once press Shift only once.
    driver.char_down("A")
    driver.char_down("B")
    assert [call for call in session.calls if call == ("press_shift",)] == [("press_shift",)]
    driver.char_up("A")
    assert ("release_shift",) in session.calls
    assert driver._char_shift_depth == 0
    driver.char_up("B")
    assert session.calls.count(("release_shift",)) == 1
    assert driver._char_shift_depth == 0


def test_keyboard_char_events_do_not_release_caller_shift(fake_page: FakePage) -> None:
    driver = PyAutoGuiKeyboardDriver()
    session = FakeXlibSession()
    driver._x11 = session  # type: ignore[assignment]
    driver.key_down("shift")
    session.calls.clear()
    try:
        driver.char_down("A")
        driver.char_up("A")
    finally:
        driver.key_up("shift")
    assert ("press_shift",) not in session.calls
    assert ("release_shift",) not in session.calls
    assert session.calls.count(("press", 50)) == 1
    assert session.calls.count(("release", 50)) == 1


class FakeXlibDisplay:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.closed = False

    def change_keyboard_mapping(self, keycode: int, mapping: list[tuple[int, int]]) -> None:
        self.calls.append(("map", keycode, mapping))

    def sync(self) -> None:
        self.calls.append(("sync",))

    def close(self) -> None:
        self.closed = True
        self.calls.append(("close",))


def test_xlib_spare_keymap_restored_on_release_and_close() -> None:
    """Unicode spare-keycode remaps must not leak into the global X keymap."""

    import threading

    session = object.__new__(_X11Keyboard)
    session._lock = threading.RLock()
    display = FakeXlibDisplay()
    session._display = display
    session._remapped = {200: 0x01004E2D, 201: 0x01004E2E}
    session._spares_in_use = {200, 201}

    session.release_spare(200)
    assert ("map", 200, [(0, 0)]) in display.calls
    assert ("map", 201, [(0, 0)]) not in display.calls
    assert 200 not in session._spares_in_use
    assert 200 not in session._remapped

    session.close()
    assert ("map", 201, [(0, 0)]) in display.calls
    assert display.closed is True
    assert session._remapped == {}
    assert session._spares_in_use == set()


def test_keyboard_xdotool_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = PyAutoGuiKeyboardDriver(unicode_mode="auto")
    driver._x11_error = DriverError("no display")  # force fallback
    calls: list[tuple[list[str], dict[str, object]]] = []

    class FakeProc:
        returncode = 0
        stderr = ""
        stdout = ""

    def _fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return FakeProc()

    monkeypatch.setattr(
        "nextautodesk.drivers.pyautogui_x11.shutil.which", lambda binary: f"/usr/bin/{binary}"
    )
    monkeypatch.setattr("nextautodesk.drivers.pyautogui_x11.subprocess.run", _fake_run)
    driver.char_down("中")
    driver.type_text("中")
    assert calls[0][0][:2] == ["xdotool", "type"]
    assert "中" in calls[0][0]
    assert len(calls) == 2
    # xdotool stderr is decoded by the child; a non-UTF-8 locale or legacy
    # stderr bytes must not crash error reporting.
    for _args, kwargs in calls:
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"


def test_keyboard_xlib_mode_requires_xlib() -> None:
    driver = PyAutoGuiKeyboardDriver(unicode_mode="xlib")
    with pytest.raises(DriverError):
        driver._x11_session()


# ---------------------------------------------------------------------------
# Screen driver
# ---------------------------------------------------------------------------
def _patch_imagegrab(monkeypatch: pytest.MonkeyPatch, func) -> None:
    import PIL.ImageGrab

    monkeypatch.setattr(PIL.ImageGrab, "grab", func)


def test_screen_driver_prefers_pillow_imagegrab(
    fake_page: FakePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = X11ScreenDriver(jpeg_quality=80)
    assert driver.size().width == 1920 and driver.size().height == 1080
    bboxes: list[tuple | None] = []

    def _grab(bbox=None, include_layered_windows=False, all_screens=False, xdisplay=None, **kwargs):
        bboxes.append(bbox)
        width, height = 20, 10
        if bbox is not None:
            width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        return Image.new("RGB", (width, height), (1, 2, 3))

    _patch_imagegrab(monkeypatch, _grab)
    png = driver.screenshot()
    assert png.startswith(b"\x89PNG")
    jpeg = driver.screenshot(Rect(x=3, y=4, width=5, height=6), "jpeg")
    assert jpeg.startswith(b"\xff\xd8")
    assert bboxes == [None, (3, 4, 8, 10)]
    # PyAutoGUI's screenshot must not be used when ImageGrab works.
    assert not any(call[0] == "screenshot" for call in fake_page.calls)
    with pytest.raises(DriverError):
        driver.screenshot(fmt="bmp")  # type: ignore[arg-type]


def test_screen_driver_falls_back_to_pyautogui(
    fake_page: FakePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = X11ScreenDriver()

    def _fail(**kwargs):
        raise OSError("X connection failed")

    _patch_imagegrab(monkeypatch, _fail)
    png = driver.screenshot()
    assert png.startswith(b"\x89PNG")
    assert ("screenshot", None) in fake_page.calls
    jpeg = driver.screenshot(Rect(x=1, y=2, width=5, height=5), "jpeg")
    assert jpeg.startswith(b"\xff\xd8")
    assert ("screenshot", (1, 2, 5, 5)) in fake_page.calls


def test_screen_driver_wraps_total_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = X11ScreenDriver()

    def _fail(**kwargs):
        raise OSError("X connection failed")

    def _load_fail():
        raise DriverError("pyautogui unavailable", driver="keyboard")

    _patch_imagegrab(monkeypatch, _fail)
    monkeypatch.setattr("nextautodesk.drivers.screen_x11.load_pyautogui", _load_fail)
    with pytest.raises(DriverError) as excinfo:
        driver.screenshot()
    assert excinfo.value.driver == "screen"
    assert set(excinfo.value.details["errors"]) == {
        "pillow_imagegrab",
        "pyautogui_screenshot",
    }


# ---------------------------------------------------------------------------
# Window driver selectors and commands
# ---------------------------------------------------------------------------
@pytest.fixture
def window_driver() -> X11WindowDriver:
    driver = X11WindowDriver(backend="xdotool", command_timeout_s=1.0)
    terminal = make_window()
    other = make_window("0x4a00001", title="File Manager", class_name="Thunar")
    terminal.active = True
    other.active = False
    driver.list_windows = lambda: [terminal, other]  # type: ignore[method-assign]
    return driver


def test_window_list_retries_during_window_churn(monkeypatch: pytest.MonkeyPatch) -> None:
    """wmctrl aborts with BadWindow if a window dies during enumeration."""

    driver = X11WindowDriver(backend="xdotool", command_timeout_s=1.0)
    output = (
        "0x03a00007  0 2918   100  50  800  600  "
        "xfce4-terminal.Xfce4-terminal  nad-host Terminal - nad\n"
    )
    attempts = {"count": 0}

    def flaky_run(args, **kwargs):
        if len(args) > 1 and args[1] == "-l":
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise DriverError("wmctrl failed: BadWindow", driver="window")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(driver, "_run_tool", flaky_run)
    monkeypatch.setattr(driver, "_active_window_id", lambda: "0x3a00007")
    windows = driver.list_windows()
    assert attempts["count"] == 3
    assert len(windows) == 1 and windows[0].active is True


def test_window_selector_by_id_title_regex_class(window_driver: X11WindowDriver) -> None:
    assert window_driver.get_window(WindowSelector(id="0x03A00007")).title.startswith("Terminal")
    assert (
        window_driver.get_window(WindowSelector(title="File Manager")).class_name == "Thunar"
    )
    assert (
        window_driver.get_window(WindowSelector(title="^Term.*nad$", title_regex=True)).id
        == "0x3a00007"
    )
    thunar = window_driver.get_window(WindowSelector(class_name="thunar"))
    assert thunar.id == "0x4a00001" and thunar.class_name == "Thunar"
    assert window_driver.get_window(WindowSelector(instance_name="xfce4-terminal")).title
    active = window_driver.get_window(WindowSelector(active=True))
    assert active.id == "0x3a00007"


def test_window_selector_not_found_has_candidates(window_driver: X11WindowDriver) -> None:
    with pytest.raises(WindowNotFound) as excinfo:
        window_driver.get_window(WindowSelector(title="does not exist"))
    details = excinfo.value.details
    assert details["window_count"] == 2
    assert len(details["candidates"]) == 2
    assert details["candidates"][0]["title"].startswith("Terminal")


def test_window_selector_validation(window_driver: X11WindowDriver) -> None:
    with pytest.raises(InvalidArgument):
        window_driver.get_window(WindowSelector())
    with pytest.raises(InvalidArgument):
        window_driver.get_window(WindowSelector(title="[", title_regex=True))


class RecordingWindowDriver(X11WindowDriver):
    def __init__(self, backend: str = "wmctrl") -> None:
        super().__init__(backend=backend, command_timeout_s=1.0)
        self.commands: list[list[str]] = []

    def _run_tool(self, args, *, what, error="action", allow_failure=False, timeout_s=None):
        self.commands.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")


def test_window_mutations_use_expected_tools() -> None:
    window = make_window()
    wmctrl_driver = RecordingWindowDriver(backend="wmctrl")
    wmctrl_driver.move(window, 10, 20)
    assert wmctrl_driver.commands[-1][:3] == ["wmctrl", "-i", "-r"]
    assert "0,10,20,-1,-1" in wmctrl_driver.commands[-1]

    wmctrl_driver.resize(window, 640, 480)
    assert "0,-1,-1,640,480" in wmctrl_driver.commands[-1]

    wmctrl_driver.maximize(window)
    assert "add,maximized_vert,maximized_horz" in wmctrl_driver.commands[-1]

    wmctrl_driver.close(window)
    assert wmctrl_driver.commands[-1][0] == "wmctrl"

    xdotool_driver = RecordingWindowDriver(backend="xdotool")
    xdotool_driver.activate(window)
    assert xdotool_driver.commands[-1][:2] == ["xdotool", "windowactivate"]

    xdotool_driver.move(window, 5, 6)
    assert xdotool_driver.commands[-1][:2] == ["xdotool", "windowmove"]

    xdotool_driver.minimize(window)
    assert xdotool_driver.commands[-1][:2] == ["xdotool", "windowminimize"]


def test_window_action_failure_maps_to_domain_error() -> None:
    class FailingDriver(X11WindowDriver):
        def _run_tool(self, args, *, what, error="action", allow_failure=False, timeout_s=None):
            raise WindowActionFailed(what, details={"command": args})

    driver = FailingDriver(backend="wmctrl")
    with pytest.raises(WindowActionFailed):
        driver.move(make_window(), 1, 2)


def test_window_tools_decode_utf8_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-ASCII titles must not crash enumeration under a C/POSIX locale."""

    import subprocess

    captured: list[dict[str, object]] = []

    def _fake_run(args, **kwargs):
        captured.append(kwargs)
        stdout = "0x04000007  0 42 10 20 640 480  xfce4-terminal.Xfce4-terminal  host 终端 1\n"
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(
        "nextautodesk.drivers.window_x11.subprocess.run", _fake_run
    )
    driver = X11WindowDriver(backend="xdotool", command_timeout_s=1.0)
    windows = driver.list_windows()
    assert len(windows) == 1
    assert windows[0].title == "终端 1"
    assert captured[0]["encoding"] == "utf-8"
    assert captured[0]["errors"] == "replace"


def test_window_manager_name_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = X11WindowDriver()

    def _run_tool(args, **kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="Name: Xfwm4\nClass: Xfwm4\n", stderr=""
        )

    monkeypatch.setattr(driver, "_run_tool", _run_tool)
    assert driver.window_manager_name() == "Xfwm4"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def test_factory_sets_display_and_checks_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    from nextautodesk.config import load_config

    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "test-key")
    monkeypatch.setattr(
        "nextautodesk.drivers.factory.shutil.which", lambda binary: f"/usr/bin/{binary}"
    )
    config = load_config()
    old_display = os.environ.get("DISPLAY")
    try:
        bundle = factory.create_driver_bundle(config)
        assert os.environ["DISPLAY"] == config.desktop.display
        assert bundle.mouse is not None and bundle.window is not None
    finally:
        if old_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = old_display


def test_factory_reports_missing_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    from nextautodesk.config import load_config

    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "test-key")
    monkeypatch.setattr("nextautodesk.drivers.factory.shutil.which", lambda binary: None)
    config = load_config()
    with pytest.raises(DriverError) as excinfo:
        factory.create_driver_bundle(config)
    assert excinfo.value.driver == "factory"


def test_resolve_display_rejects_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    from nextautodesk.config import load_config

    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "test-key")
    config = load_config()
    config.desktop.display = "not-a-display"
    with pytest.raises(DriverError):
        factory.resolve_display(config)
