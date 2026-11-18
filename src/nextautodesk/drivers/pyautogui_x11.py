"""PyAutoGUI + python-xlib execution engine for an X11 virtual desktop.

This module is the *only* place that knows how input is actually injected. It is
deliberately free of any pacing/human-behaviour logic: strategies decide *when*
to press a key, the driver only performs the primitive immediately.

Design notes
------------
* ``pyautogui`` is imported **lazily**. Importing PyAutoGUI opens an X display
  (and therefore requires ``DISPLAY``), so module import must stay side-effect
  free: unit tests and configuration validation run on headless hosts.
* Mouse movement, buttons, wheel and screenshots use PyAutoGUI (XTEST).
* Non-ASCII text cannot be typed with PyAutoGUI on X11. The keyboard driver
  therefore uses python-xlib to remap a spare keycode to the desired Unicode
  keysym (the same technique xdotool uses) and injects it through XTEST.
  ``unicode_mode=auto`` falls back to ``xdotool type`` when Xlib injection is
  not possible (missing package/display); ``xdotool`` forces that fallback and
  ``xlib`` disables it.
* Human key names (``ctrl``, ``enter``, ``pageup``, ``f5`` ...) are normalised
  by :func:`normalize_key_name` before being handed to PyAutoGUI.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from typing import Any, NoReturn

from ..models import MouseButton, Point
from .base import DriverError

LOGGER = logging.getLogger(__name__)

#: PyAutoGUI mouse button names accepted on X11.
_MOUSE_BUTTONS: frozenset[str] = frozenset({"left", "right", "middle"})

#: Map of X11 button masks (from XQueryPointer) to our button names.
_BUTTON_MASK_NAMES: tuple[tuple[int, MouseButton], ...] = (
    (1 << 8, "left"),
    (1 << 9, "middle"),
    (1 << 10, "right"),
)


# ---------------------------------------------------------------------------
# Lazy PyAutoGUI access
# ---------------------------------------------------------------------------
_PYAUTOGUI: Any = None
_PYAUTOGUI_LOCK = threading.Lock()


def load_pyautogui() -> Any:
    """Import and configure PyAutoGUI on first use.

    PyAutoGUI's X11 backend opens ``$DISPLAY`` at import time, which is why this
    helper exists: importing :mod:`nextautodesk.drivers.pyautogui_x11` itself is
    safe on a headless host.
    """

    global _PYAUTOGUI
    if _PYAUTOGUI is not None:
        return _PYAUTOGUI
    with _PYAUTOGUI_LOCK:
        if _PYAUTOGUI is None:
            try:
                import pyautogui  # intentional lazy import: needs DISPLAY
            except Exception as exc:  # pragma: no cover - environment failure
                raise DriverError(
                    f"cannot import pyautogui on this host: {type(exc).__name__}: {exc}",
                    driver="pyautogui",
                    details={"display_set": "DISPLAY" in os.environ},
                ) from exc
            # Deterministic, non-failsafe injection: the *strategy* owns pacing.
            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0.0
            pyautogui.MINIMUM_DURATION = 0.0
            _PYAUTOGUI = pyautogui
    return _PYAUTOGUI


# ---------------------------------------------------------------------------
# Key-name normalisation (pure, unit-testable without X)
# ---------------------------------------------------------------------------
_SPECIAL_KEYS: dict[str, str] = {
    # modifiers
    "ctrl": "ctrl",
    "control": "ctrl",
    "strg": "ctrl",
    "ctrlleft": "ctrl",
    "alt": "alt",
    "option": "alt",
    "altleft": "alt",
    "shift": "shift",
    "shiftleft": "shift",
    "win": "win",
    "super": "win",
    "meta": "win",
    "cmd": "win",
    "command": "win",
    "winleft": "win",
    "winright": "winright",
    "superright": "winright",
    "altright": "altright",
    "shiftright": "shiftright",
    "ctrlright": "ctrlright",
    # editing / navigation
    "enter": "enter",
    "return": "enter",
    "kp_enter": "enter",
    "esc": "esc",
    "escape": "esc",
    "tab": "tab",
    "backtab": "tab",
    "backspace": "backspace",
    "bksp": "backspace",
    "delete": "delete",
    "del": "delete",
    "insert": "insert",
    "ins": "insert",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "page_up": "pageup",
    "pgup": "pageup",
    "prior": "pageup",
    "pagedown": "pagedown",
    "page_down": "pagedown",
    "pgdn": "pagedown",
    "next": "pagedown",
    "space": "space",
    "spacebar": "space",
    "capslock": "capslock",
    "numlock": "numlock",
    "scrolllock": "scrolllock",
    "printscreen": "printscreen",
    "print": "printscreen",
    "prtsc": "printscreen",
    "prtscr": "printscreen",
    "prntscrn": "printscreen",
    "pause": "pause",
    "break": "pause",
    "menu": "apps",
    "apps": "apps",
    # arrows
    "up": "up",
    "arrowup": "up",
    "arrow_up": "up",
    "up_arrow": "up",
    "down": "down",
    "arrowdown": "down",
    "arrow_down": "down",
    "down_arrow": "down",
    "left": "left",
    "arrowleft": "left",
    "arrow_left": "left",
    "left_arrow": "left",
    "right": "right",
    "arrowright": "right",
    "arrow_right": "right",
    "right_arrow": "right",
    # punctuation aliases
    "plus": "+",
    "minus": "-",
    "equal": "=",
    "comma": ",",
    "period": ".",
    "dot": ".",
    "slash": "/",
    "backslash": "\\",
    "semicolon": ";",
    "colon": ":",
    "quote": "'",
    "apostrophe": "'",
    "grave": "`",
    "lbrace": "[",
    "rbrace": "]",
    "lbracket": "[",
    "rbracket": "]",
}
for _index in range(1, 25):
    _SPECIAL_KEYS[f"f{_index}"] = f"f{_index}"
for _index in range(0, 10):
    _SPECIAL_KEYS[f"kp{_index}"] = f"num{_index}"
    _SPECIAL_KEYS[f"kp_{_index}"] = f"num{_index}"

_CHAR_KEYS: dict[str, str] = {
    " ": "space",
    "\t": "tab",
    "\n": "enter",
    "\r": "enter",
    "\b": "backspace",
    "\x1b": "esc",
}

SUPPORTED_KEY_NAMES: tuple[str, ...] = tuple(sorted(_SPECIAL_KEYS))


def normalize_key_name(name: str) -> str:
    """Map a human key name onto the name PyAutoGUI/X11 expects.

    Raises:
        DriverError: when the key cannot be represented.
    """

    if not isinstance(name, str):
        raise DriverError(f"key name must be a string, got {type(name).__name__}", driver="keyboard")
    if len(name) == 1:
        if name in _CHAR_KEYS:
            return _CHAR_KEYS[name]
        if name.isascii() and name.isprintable():
            return name.lower() if name.isalpha() else name
        raise DriverError(
            f"unsupported single-character key {name!r}; use keyboard.type for text",
            driver="keyboard",
        )
    key = name.strip().lower()
    if not key:
        raise DriverError("empty key name", driver="keyboard")
    if key in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[key]
    raise DriverError(
        f"unsupported key name {name!r}",
        driver="keyboard",
        details={"supported": list(SUPPORTED_KEY_NAMES)},
    )


def char_to_keysym(char: str) -> int:
    """Return the X11 keysym for one Unicode code point.

    Latin-1 printable characters map to their code point; everything else uses
    the Unicode keysym convention ``0x01000000 + codepoint``.
    """

    if not isinstance(char, str) or len(char) != 1:
        raise DriverError(
            f"char_to_keysym expects a single character, got {char!r}", driver="keyboard"
        )
    code = ord(char)
    named = {
        "\n": 0xFF0D,  # Return
        "\r": 0xFF0D,
        "\t": 0xFF09,  # Tab
        "\b": 0xFF08,  # BackSpace
        "\x1b": 0xFF1B,  # Escape
    }
    if char in named:
        return named[char]
    if 0xD800 <= code <= 0xDFFF:
        raise DriverError("unpaired Unicode surrogate cannot be typed", driver="keyboard")
    if code < 0x20 or 0x7F <= code <= 0x9F:
        raise DriverError(f"control character {char!r} cannot be typed", driver="keyboard")
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code
    return 0x01000000 + code


def buttons_from_mask(mask: int) -> set[MouseButton]:
    """Decode an XQueryPointer state mask into the set of pressed buttons."""

    return {name for bit, name in _BUTTON_MASK_NAMES if mask & bit}


# ---------------------------------------------------------------------------
# python-xlib session (keyboard injection + pointer button state)
# ---------------------------------------------------------------------------
def _xlib_version() -> str:
    try:
        import Xlib

        version = getattr(Xlib, "__version__", None)
        if isinstance(version, tuple):
            return ".".join(str(part) for part in version)
        return str(version or "unknown")
    except Exception:  # pragma: no cover - optional dependency
        return "unavailable"


class _X11Keyboard:
    """Minimal XTEST keyboard injector with Unicode spare-keycode remapping."""

    def __init__(self) -> None:
        try:
            from Xlib import XK, X, display
            from Xlib.ext import xtest
        except Exception as exc:  # pragma: no cover - optional dependency
            raise DriverError(
                f"python-xlib is not usable: {type(exc).__name__}: {exc}",
                driver="keyboard",
            ) from exc
        try:
            self._display = display.Display()
        except Exception as exc:  # pragma: no cover - needs a live X server
            raise DriverError(
                f"cannot open X display for keyboard injection: {type(exc).__name__}: {exc}",
                driver="keyboard",
            ) from exc
        self._X = X
        self._XK = XK
        self._xtest = xtest
        self.version = _xlib_version()
        self._lock = threading.RLock()
        self._spare_pool: list[int] | None = None
        self._spares_in_use: set[int] = set()
        self._remapped: dict[int, int] = {}
        self._shift_keycode = self._display.keysym_to_keycode(
            XK.string_to_keysym("Shift_L")
        )
        if not self._shift_keycode:
            raise DriverError("cannot resolve Shift_L keycode", driver="keyboard")

    # -- low level ---------------------------------------------------------
    def sync(self) -> None:
        self._display.sync()

    def key_event(self, keycode: int, press: bool) -> None:
        with self._lock:
            self._xtest.fake_input(
                self._display, self._X.KeyPress if press else self._X.KeyRelease, keycode
            )
            self._display.sync()

    def _spares(self) -> list[int]:
        if self._spare_pool is None:
            info = self._display.display.info
            pool: list[int] = []
            for keycode in range(info.min_keycode, info.max_keycode + 1):
                mapping = self._display.get_keyboard_mapping(keycode, 1)[0]
                if not any(mapping):
                    pool.append(keycode)
            self._spare_pool = pool
            LOGGER.debug("xlib: %d spare keycodes available", len(pool))
        return self._spare_pool

    def _acquire_spare(self) -> int:
        for keycode in self._spares():
            if keycode not in self._spares_in_use:
                self._spares_in_use.add(keycode)
                return keycode
        raise DriverError(
            "no spare X11 keycode available for Unicode input; "
            "too many distinct characters are held at once",
            driver="keyboard",
        )

    def release_spare(self, keycode: int) -> None:
        # The remap is server-global and would otherwise persist after this
        # process exits (or even after the character is released, affecting
        # other clients that query the keymap), so restore the empty mapping.
        with self._lock:
            if self._remapped.pop(keycode, None) is not None:
                try:
                    self._display.change_keyboard_mapping(keycode, [(0, 0)])
                    self._display.sync()
                except Exception:  # pragma: no cover - display may be closing
                    LOGGER.debug(
                        "xlib: failed to restore spare keycode %d", keycode, exc_info=True
                    )
            self._spares_in_use.discard(keycode)

    def _remap(self, keycode: int, keysym: int) -> None:
        # Map both shift levels to the same keysym so the character is produced
        # regardless of the modifier state of the desktop at injection time.
        self._display.change_keyboard_mapping(keycode, [(keysym, keysym)])
        self._display.sync()
        self._remapped[keycode] = keysym

    def resolve_char(self, char: str) -> tuple[int, bool, int | None]:
        """Resolve a character to ``(keycode, needs_shift, spare_keycode)``."""

        keysym = char_to_keysym(char)
        with self._lock:
            keycode = self._display.keysym_to_keycode(keysym)
            if keycode:
                mapping = tuple(self._display.get_keyboard_mapping(keycode, 1)[0])
                for level, symbol in enumerate(mapping[:2]):
                    if symbol == keysym:
                        return keycode, level == 1, None
            spare = self._acquire_spare()
            try:
                self._remap(spare, keysym)
            except Exception:
                self.release_spare(spare)
                raise
            return spare, False, spare

    def press(self, keycode: int) -> None:
        self.key_event(keycode, press=True)

    def release(self, keycode: int) -> None:
        self.key_event(keycode, press=False)

    def press_shift(self) -> None:
        self.key_event(self._shift_keycode, press=True)

    def release_shift(self) -> None:
        self.key_event(self._shift_keycode, press=False)

    # -- queries -----------------------------------------------------------
    def pressed_buttons(self) -> set[MouseButton]:
        with self._lock:
            data = self._display.screen().root.query_pointer()._data
            return buttons_from_mask(int(data["mask"]))

    def close(self) -> None:
        with self._lock:
            # Restore every spare keycode we remapped: the X keymap is global,
            # so leaving Unicode keysyms behind mutates the desktop for other
            # clients (and for the next process run) until the X server resets.
            for keycode in list(self._remapped):
                try:
                    self._display.change_keyboard_mapping(keycode, [(0, 0)])
                except Exception:  # pragma: no cover - defensive
                    LOGGER.debug(
                        "xlib: failed to restore spare keycode %d", keycode, exc_info=True
                    )
            self._remapped.clear()
            self._spares_in_use.clear()
            try:
                self._display.sync()
            except Exception:  # pragma: no cover - defensive
                LOGGER.debug("xlib: sync before close failed", exc_info=True)
            try:
                self._display.close()
            except Exception:  # pragma: no cover - defensive
                LOGGER.debug("xlib: display close failed", exc_info=True)


# ---------------------------------------------------------------------------
# Mouse driver
# ---------------------------------------------------------------------------
class PyAutoGuiMouseDriver:
    """Mouse primitives executed through PyAutoGUI/XTEST."""

    def __init__(self) -> None:
        self._x11: _X11Keyboard | None = None
        self._x11_lock = threading.Lock()

    def _x11_session(self) -> _X11Keyboard:
        if self._x11 is None:
            with self._x11_lock:
                if self._x11 is None:
                    self._x11 = _X11Keyboard()
        return self._x11

    @staticmethod
    def _fail(action: str, exc: Exception) -> NoReturn:
        raise DriverError(
            f"mouse {action} failed: {type(exc).__name__}: {exc}",
            driver="mouse",
        ) from exc

    def position(self) -> Point:
        try:
            x, y = load_pyautogui().position()
        except Exception as exc:
            self._fail("position", exc)
        return Point(x=float(x), y=float(y))

    def move_to(self, x: float, y: float, duration_s: float = 0.0) -> None:
        duration = max(0.0, float(duration_s or 0.0))
        try:
            load_pyautogui().moveTo(
                round(float(x)), round(float(y)), duration=duration, _pause=False
            )
        except Exception as exc:
            self._fail("move_to", exc)

    def button_down(self, button: MouseButton) -> None:
        self._check_button(button)
        try:
            load_pyautogui().mouseDown(button=button, _pause=False)
        except Exception as exc:
            self._fail("button_down", exc)

    def button_up(self, button: MouseButton) -> None:
        self._check_button(button)
        try:
            load_pyautogui().mouseUp(button=button, _pause=False)
        except Exception as exc:
            self._fail("button_up", exc)

    def scroll(self, dx: int, dy: int) -> None:
        """Wheel detents: ``+dy`` scrolls down, ``+dx`` scrolls right."""

        try:
            page = load_pyautogui()
            # PyAutoGUI/X11: positive clicks scroll *up* / *left* (buttons 4/6).
            if dy:
                page.vscroll(-int(dy), _pause=False)
            if dx:
                page.hscroll(int(dx), _pause=False)
        except Exception as exc:
            self._fail("scroll", exc)

    def buttons_down(self) -> set[MouseButton]:
        try:
            return self._x11_session().pressed_buttons()
        except DriverError:
            raise
        except Exception as exc:
            self._fail("buttons_down", exc)

    @staticmethod
    def _check_button(button: MouseButton) -> None:
        if button not in _MOUSE_BUTTONS:
            raise DriverError(
                f"unsupported mouse button {button!r}",
                driver="mouse",
                details={"supported": sorted(_MOUSE_BUTTONS)},
            )


# ---------------------------------------------------------------------------
# Keyboard driver
# ---------------------------------------------------------------------------
class PyAutoGuiKeyboardDriver:
    """Key/text primitives with X11 Unicode support."""

    def __init__(
        self,
        *,
        unicode_mode: str = "auto",
        xdotool_binary: str = "xdotool",
        timeout_s: float = 10.0,
    ) -> None:
        if unicode_mode not in {"auto", "xlib", "xdotool"}:
            raise DriverError(
                f"invalid unicode_mode {unicode_mode!r}", driver="keyboard"
            )
        self.unicode_mode = unicode_mode
        self._xdotool_binary = xdotool_binary
        self._timeout_s = timeout_s
        self._x11: _X11Keyboard | None = None
        self._x11_error: DriverError | None = None
        self._x11_lock = threading.Lock()
        self._pressed_chars: dict[str, list[tuple[int, bool, int | None]]] = {}
        #: Named keys currently held through the ``keyboard_key_down`` state
        #: primitive (or a direct ``key_down`` call), normalised names.
        self._held_keys: set[str] = set()
        #: How many held characters currently need Shift; used so a character
        #: typed while another Shift-requiring character is held does not press
        #: (and later release) Shift twice.
        self._char_shift_depth = 0
        self._press_lock = threading.RLock()
        self.last_unicode_backend: str | None = None

    # -- xlib plumbing -----------------------------------------------------
    def _x11_session(self) -> _X11Keyboard | None:
        """Return the Xlib session or ``None`` when unavailable (auto mode)."""

        if self._x11 is not None:
            return self._x11
        if self._x11_error is not None and self.unicode_mode == "auto":
            return None
        with self._x11_lock:
            if self._x11 is None and (self._x11_error is None or self.unicode_mode != "auto"):
                try:
                    self._x11 = _X11Keyboard()
                except DriverError as exc:
                    if self.unicode_mode == "xlib":
                        raise
                    self._x11_error = exc
                    LOGGER.warning("keyboard: Xlib Unicode injection unavailable: %s", exc)
        return self._x11

    def _require_unicode_backend(self) -> str:
        if self.unicode_mode == "xdotool":
            self._require_xdotool()
            return "xdotool"
        if self._x11_session() is not None:
            return "xlib"
        self._require_xdotool()
        return "xdotool"

    def _require_xdotool(self) -> None:
        if shutil.which(self._xdotool_binary) is None:
            raise DriverError(
                f"cannot type non-ASCII text: python-xlib injection is unavailable and "
                f"{self._xdotool_binary!r} is not installed",
                driver="keyboard",
                details={"unicode_mode": self.unicode_mode, "xdotool": self._xdotool_binary},
            )

    def _xdotool_type(self, text: str) -> None:
        self._require_xdotool()
        try:
            proc = subprocess.run(
                [
                    self._xdotool_binary,
                    "type",
                    "--clearmodifiers",
                    "--delay",
                    "0",
                    "--",
                    text,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DriverError(
                f"xdotool type failed: {type(exc).__name__}: {exc}", driver="keyboard"
            ) from exc
        if proc.returncode != 0:
            raise DriverError(
                "xdotool type exited with an error",
                driver="keyboard",
                details={"returncode": proc.returncode, "stderr": proc.stderr[-500:]},
            )
        self.last_unicode_backend = "xdotool"

    # -- capability --------------------------------------------------------
    def supports_unicode(self) -> bool:
        if self.unicode_mode == "xdotool":
            return shutil.which(self._xdotool_binary) is not None
        if self._x11_session() is not None:
            return True
        return shutil.which(self._xdotool_binary) is not None

    def unicode_backend(self) -> str | None:
        """Which backend would currently be used for non-ASCII characters."""

        if self.unicode_mode == "xdotool":
            return "xdotool" if shutil.which(self._xdotool_binary) else None
        if self._x11_session() is not None:
            return "xlib"
        return "xdotool" if shutil.which(self._xdotool_binary) else None

    # -- named keys --------------------------------------------------------
    def key_down(self, key: str) -> None:
        name = normalize_key_name(key)
        try:
            load_pyautogui().keyDown(name, _pause=False)
        except Exception as exc:
            raise DriverError(
                f"keyboard key_down({key!r}) failed: {type(exc).__name__}: {exc}",
                driver="keyboard",
            ) from exc
        self._held_keys.add(name)

    def key_up(self, key: str) -> None:
        name = normalize_key_name(key)
        try:
            load_pyautogui().keyUp(name, _pause=False)
        except Exception as exc:
            raise DriverError(
                f"keyboard key_up({key!r}) failed: {type(exc).__name__}: {exc}",
                driver="keyboard",
            ) from exc
        self._held_keys.discard(name)

    def _shift_is_held(self) -> bool:
        """Whether Shift is already down for this driver (state primitive)."""

        return "shift" in self._held_keys or self._char_shift_depth > 0

    # -- characters --------------------------------------------------------
    def char_down(self, char: str) -> None:
        self._char_event(char, press=True)

    def char_up(self, char: str) -> None:
        self._char_event(char, press=False)

    def _char_event(self, char: str, *, press: bool) -> None:
        if not isinstance(char, str) or len(char) != 1:
            raise DriverError(f"char must be one character, got {char!r}", driver="keyboard")
        if self.unicode_mode == "xdotool":
            if press:
                self._xdotool_type(char)
            return
        session = self._x11_session()
        if session is None:
            if press:
                self._xdotool_type(char)
            return
        with self._press_lock:
            if press:
                keycode, needs_shift, spare = session.resolve_char(char)
                pressed_shift = False
                if needs_shift and not self._shift_is_held():
                    session.press_shift()
                    self._char_shift_depth += 1
                    pressed_shift = True
                session.press(keycode)
                # Track whether *this* character pressed Shift: releasing a
                # Shift that was already held by the key_down state primitive
                # would leave the caller's modifier stuck-up.
                self._pressed_chars.setdefault(char, []).append(
                    (keycode, pressed_shift, spare)
                )
                self.last_unicode_backend = "xlib"
                return
            events = self._pressed_chars.get(char) or []
            if not events:
                LOGGER.debug("keyboard: char_up(%r) with no matching char_down", char)
                return
            keycode, pressed_shift, spare = events.pop()
            if not events:
                self._pressed_chars.pop(char, None)
            session.release(keycode)
            if pressed_shift:
                session.release_shift()
                self._char_shift_depth = max(0, self._char_shift_depth - 1)
            if spare is not None:
                session.release_spare(spare)

    # -- text --------------------------------------------------------------
    def type_text(self, text: str) -> None:
        if not isinstance(text, str):
            raise DriverError(f"type_text expects a string, got {type(text).__name__}", driver="keyboard")
        if not text:
            return
        page: Any = None
        keyboard_keys: set[str] = set()
        try:
            index = 0
            while index < len(text):
                char = text[index]
                if ord(char) < 128:
                    if page is None:
                        page = load_pyautogui()
                        keyboard_keys = set(page.KEYBOARD_KEYS)
                    if "A" <= char <= "Z":
                        # PyAutoGUI's KEYBOARD_KEYS only lists lowercase letters;
                        # uppercase must be produced by holding Shift explicitly.
                        if self._shift_is_held():
                            # Shift is already down (key_down state primitive or
                            # another held character): writing the lowercase key
                            # already produces the uppercase character, and
                            # pressing/releasing Shift here would release the
                            # caller's modifier.
                            page.write(char.lower(), interval=0, _pause=False)
                        else:
                            page.keyDown("shift")
                            try:
                                page.write(char.lower(), interval=0, _pause=False)
                            finally:
                                page.keyUp("shift")
                        index += 1
                        continue
                    if char not in keyboard_keys:
                        raise DriverError(
                            f"cannot type ASCII character {char!r}", driver="keyboard"
                        )
                    end = index + 1
                    while (
                        end < len(text)
                        and ord(text[end]) < 128
                        and text[end] in keyboard_keys
                    ):
                        end += 1
                    page.write(text[index:end], interval=0, _pause=False)
                    index = end
                    continue
                self._type_unicode_char(char)
                index += 1
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError(
                f"keyboard type_text failed: {type(exc).__name__}: {exc}",
                driver="keyboard",
            ) from exc

    def _type_unicode_char(self, char: str) -> None:
        backend = self._require_unicode_backend()
        if backend == "xdotool":
            self._xdotool_type(char)
            return
        session = self._x11_session()
        assert session is not None  # guaranteed by _require_unicode_backend
        keycode, needs_shift, spare = session.resolve_char(char)
        pressed_shift = False
        if needs_shift and not self._shift_is_held():
            session.press_shift()
            pressed_shift = True
        try:
            session.press(keycode)
            session.release(keycode)
        finally:
            if pressed_shift:
                session.release_shift()
            if spare is not None:
                session.release_spare(spare)
        self.last_unicode_backend = "xlib"

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """Release any characters still held and close the Xlib connection."""

        with self._press_lock:
            for char, events in list(self._pressed_chars.items()):
                for keycode, pressed_shift, spare in events:
                    if self._x11 is not None:
                        self._x11.release(keycode)
                        if pressed_shift:
                            self._x11.release_shift()
                        if spare is not None:
                            self._release_spare_quietly(spare)
                self._pressed_chars.pop(char, None)
            self._char_shift_depth = 0
            self._held_keys.clear()
        if self._x11 is not None:
            self._x11.close()
            self._x11 = None

    def _release_spare_quietly(self, spare: int) -> None:
        if self._x11 is not None:
            self._x11.release_spare(spare)


__all__ = [
    "SUPPORTED_KEY_NAMES",
    "PyAutoGuiKeyboardDriver",
    "PyAutoGuiMouseDriver",
    "buttons_from_mask",
    "char_to_keysym",
    "load_pyautogui",
    "normalize_key_name",
]
