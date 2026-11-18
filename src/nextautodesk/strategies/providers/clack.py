"""Clack strategy provider (optional text-typing policy).

Drives the vendored `Clack <https://github.com/ThisWasAryan/clack>`_ CLI
(v1.0.0, MIT, Rust) as an external rhythm generator for text input:

* the provider spawns the CLI, feeds the requested text on stdin and reads the
  CLI's stdout **as it is flushed** — one byte per keystroke;
* printable UTF-8 characters are injected with
  ``ctx.drivers.keyboard.type_text(char)`` (one character at a time);
* ``0x08`` becomes the ``backspace`` key, ``0x0a`` the ``enter`` key and
  ``0x09`` the ``tab`` key (key down + key up);
* other control bytes are skipped and recorded in the outcome.

Clack owns the human timing model (WPM, fatigue, jitter, thinking pauses, typo
generation and correction); NextAutoDesk only translates its output stream into
driver calls. That keeps the strategy independent from the execution engine.

Clack 1.0.0 emits one byte per character and its CLI does not support real
Unicode input (non-ASCII input is mangled/truncated by the CLI), so this
provider currently accepts ASCII text only and reports a clear error otherwise.

The binary path defaults to ``/usr/local/bin/clack`` (Docker image location) and
falls back to the repository-vendored ``third_party/clack/clack`` so development
checkouts work without installing anything.
"""

from __future__ import annotations

import codecs
import contextlib
import logging
import math
import os
import select
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from ...errors import InvalidArgument, StrategyExecutionFailed
from ...models import ProviderAvailability
from ...operations import Operation
from ..base import OperationRequest, StrategyContext, StrategyOutcome, StrategyProvider

LOGGER = logging.getLogger(__name__)

_DEFAULT_BINARY = "/usr/local/bin/clack"
_VALID_LAYOUTS = frozenset({"qwerty", "dvorak", "colemak", "azerty"})
#: Clack output byte -> (driver key name, outcome counter name).
_KEY_BYTES: dict[int, tuple[str, str]] = {
    0x08: ("backspace", "backspaces"),
    0x09: ("tab", "tabs"),
    0x0A: ("enter", "enters"),
}
_DEFAULT_PARAMS: dict[str, Any] = {
    "binary": _DEFAULT_BINARY,
    "wpm": 80.0,
    "jitter": 0.15,
    "error_rate": 0.0,
    "correction_rate": 0.85,
    "layout": "qwerty",
    "seed": None,
    "no_fatigue": True,
    "max_pause_ms": 5000,
    "thinking_pause_prob": 0.0,
    "code_mode": False,
    "startup_timeout_s": 5.0,
    "char_timeout_s": 10.0,
}
_SUPPORTED = frozenset({Operation.KEYBOARD_TYPE_TEXT})

#: Cap on the raw stdout bytes echoed into the outcome; Clack emits one byte per
#: keystroke and task outcomes are persisted, so an unbounded hex dump would
#: amplify a large text request into a multi-megabyte stored record.
_RAW_OUTPUT_LIMIT = 512


# ---------------------------------------------------------------------------
# helpers (module-level so unit tests can exercise them without an X server)
# ---------------------------------------------------------------------------
def _settings(params: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(params) - set(_DEFAULT_PARAMS))
    if unknown:
        # Silently ignoring a typo (``wmp`` instead of ``wpm``) would make the
        # configuration look applied while the default is actually used.
        raise ValueError(f"unknown clack parameters: {unknown}")
    settings = dict(_DEFAULT_PARAMS)
    settings.update(params)
    return settings


def _effective_char_timeout(settings: Mapping[str, Any]) -> float:
    """Per-byte idle timeout, floored above Clack's own ``--max-pause``.

    Clack may legitimately pause for up to ``max_pause_ms`` between two output
    bytes; a shorter configured ``char_timeout_s`` would otherwise kill a valid
    run mid-text.
    """

    max_pause_s = float(settings["max_pause_ms"]) / 1000.0
    return max(float(settings["char_timeout_s"]), max_pause_s + 1.0)


def _raw_output_details(raw: bytes) -> dict[str, Any]:
    """Bounded representation of the CLI stdout for the outcome details."""

    return {
        "raw_output_hex": raw[:_RAW_OUTPUT_LIMIT].hex(),
        "raw_output_truncated": len(raw) > _RAW_OUTPUT_LIMIT,
    }


def _number(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float,
    inclusive_minimum: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be finite")
    if inclusive_minimum:
        if numeric < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
    elif numeric <= minimum:
        raise ValueError(f"{name} must be greater than {minimum}")
    if numeric > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return numeric


def _seed(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("seed must be an integer or null")
    if not 0 <= value < 2**64:
        raise ValueError("seed must be in [0, 2**64)")
    return value


def _format_number(value: Any) -> str:
    """Format a numeric CLI flag without a trailing ``.0`` (clap accepts both)."""

    return format(float(value), "g")


def _vendored_binary() -> Path:
    """``<repo>/third_party/clack/clack`` for source checkouts."""

    return Path(__file__).resolve().parents[4] / "third_party" / "clack" / "clack"


def _resolve_binary(settings: Mapping[str, Any]) -> str | None:
    """Return the first existing executable Clack binary, or ``None``."""

    configured = str(settings.get("binary") or _DEFAULT_BINARY)
    candidates = [Path(configured).expanduser(), _vendored_binary()]
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
        except OSError:  # pragma: no cover - permission/IO race
            continue
    return None


def _build_command(binary: str, settings: Mapping[str, Any]) -> list[str]:
    """Translate provider parameters into Clack 1.0.0 CLI flags."""

    command = [
        binary,
        "--wpm",
        _format_number(settings["wpm"]),
        "--jitter",
        _format_number(settings["jitter"]),
        "--correction-rate",
        _format_number(settings["correction_rate"]),
        "--layout",
        str(settings["layout"]),
        "--max-pause",
        str(int(settings["max_pause_ms"])),
        "--thinking-pause-prob",
        _format_number(settings["thinking_pause_prob"]),
    ]
    error_rate = float(settings["error_rate"])
    if error_rate > 0:
        command.extend(["--error-rate", _format_number(error_rate)])
    else:
        command.append("--no-errors")
    if bool(settings["no_fatigue"]):
        command.append("--no-fatigue")
    if bool(settings["code_mode"]):
        command.append("--code-mode")
    seed = _seed(settings["seed"])
    if seed is not None:
        command.extend(["--seed", str(seed)])
    return command


class _ByteStreamTranslator:
    """Incrementally translate Clack stdout bytes into keyboard actions.

    The translator is deliberately driver-agnostic: :meth:`feed` returns a list
    of ``("char", value)`` / ``("key", key_name)`` tuples which the provider
    applies through the keyboard driver. This also makes the byte-stream
    interpretation unit-testable without spawning a process.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.chars_typed = 0
        self.backspaces = 0
        self.enters = 0
        self.tabs = 0
        self.invalid_utf8_bytes = 0
        self.skipped_control_bytes: list[int] = []

    def feed(self, byte: int) -> list[tuple[str, str]]:
        key_event = _KEY_BYTES.get(byte)
        if key_event is not None:
            key, counter = key_event
            setattr(self, counter, getattr(self, counter) + 1)
            return [("key", key)]
        if byte < 0x20 or byte == 0x7F:
            self.skipped_control_bytes.append(byte)
            return []
        actions: list[tuple[str, str]] = []
        for char in self._decoder.decode(bytes([byte])):
            if char == "\ufffd":
                self.invalid_utf8_bytes += 1
            elif char.isprintable():
                self.chars_typed += 1
                actions.append(("char", char))
            else:  # pragma: no cover - unicode control/format code points
                self.skipped_control_bytes.append(byte)
        return actions

    def flush(self) -> None:
        """Account for a truncated multi-byte sequence at the end of the stream."""

        for char in self._decoder.decode(b"", final=True):
            if char == "\ufffd":
                self.invalid_utf8_bytes += 1
            elif char.isprintable():  # pragma: no cover - malformed trailing bytes
                self.chars_typed += 1


class ClackProvider(StrategyProvider):
    """Clack CLI driven text-typing strategy (only ``keyboard_type_text``)."""

    name: ClassVar[str] = "clack"
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str] = (
        "Clack CLI v1.0.0 text-typing rhythm (MIT, Rust; subprocess byte stream "
        "translated into driver injection; keyboard_type_text only, ASCII)"
    )
    supported: ClassVar[frozenset[Operation]] = _SUPPORTED
    license: ClassVar[str] = "MIT"

    # -- introspection -----------------------------------------------------
    def availability(self) -> ProviderAvailability:
        base: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "supported_operations": sorted(self.supported, key=lambda op: op.value),
        }
        binary = _resolve_binary(_settings({}))
        if binary is None:
            return ProviderAvailability(
                **base,
                available=False,
                reason=(
                    "clack binary not found or not executable (looked for "
                    f"{_DEFAULT_BINARY} and {_vendored_binary()})"
                ),
                details={"license": self.license},
            )
        return ProviderAvailability(
            **base,
            available=True,
            details={"binary": binary, "license": self.license},
        )

    def validate_params(self, params: Mapping[str, Any]) -> None:
        settings = _settings(params)

        binary = settings["binary"]
        if not isinstance(binary, (str, os.PathLike)) or not str(binary).strip():
            raise ValueError("binary must be a non-empty path string")

        _number(settings["wpm"], "wpm", minimum=0.0, maximum=1000.0)
        _number(settings["jitter"], "jitter", minimum=0.0, maximum=10.0, inclusive_minimum=True)
        _number(
            settings["error_rate"], "error_rate", minimum=0.0, maximum=1.0, inclusive_minimum=True
        )
        _number(
            settings["correction_rate"],
            "correction_rate",
            minimum=0.0,
            maximum=1.0,
            inclusive_minimum=True,
        )
        if settings["layout"] not in _VALID_LAYOUTS:
            raise ValueError(
                f"layout must be one of {sorted(_VALID_LAYOUTS)}, got {settings['layout']!r}"
            )
        _seed(settings["seed"])
        for flag_name in ("no_fatigue", "code_mode"):
            if not isinstance(settings[flag_name], bool):
                raise ValueError(f"{flag_name} must be a boolean")
        _number(
            settings["max_pause_ms"],
            "max_pause_ms",
            minimum=0.0,
            maximum=600000.0,
            inclusive_minimum=True,
        )
        _number(
            settings["thinking_pause_prob"],
            "thinking_pause_prob",
            minimum=0.0,
            maximum=1.0,
            inclusive_minimum=True,
        )
        _number(settings["startup_timeout_s"], "startup_timeout_s", minimum=0.0, maximum=600.0)
        _number(settings["char_timeout_s"], "char_timeout_s", minimum=0.0, maximum=600.0)

    # -- execution ---------------------------------------------------------
    def execute(
        self,
        request: OperationRequest,
        params: Mapping[str, Any],
        ctx: StrategyContext,
    ) -> StrategyOutcome:
        self._ensure_supported(request.operation)
        self.validate_params(params)
        settings = _settings(params)
        started = time.monotonic()
        request.require("text")
        text = request.text or ""

        if not text:
            return self._outcome(
                request,
                started=started,
                engine="clack-cli",
                details={"chars_typed": 0, "backspaces": 0, "enters": 0, "tabs": 0},
            )
        if not text.isascii():
            raise InvalidArgument(
                "clack provider only supports ASCII text; use human_movement for Unicode "
                "(the Clack 1.0.0 CLI mangles non-ASCII input)",
                details={"provider": self.name, "text_length": len(text)},
            )

        binary = _resolve_binary(settings)
        if binary is None:
            raise StrategyExecutionFailed(
                f"clack binary not found or not executable ({_DEFAULT_BINARY}, {_vendored_binary()})",
                details={"provider": self.name},
            )
        command = _build_command(binary, settings)
        try:
            process = subprocess.Popen(  # operator-configured, vendored binary
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
        except OSError as exc:
            raise StrategyExecutionFailed(
                f"failed to start clack ({binary}): {exc}",
                details={"provider": self.name, "command": command},
            ) from exc

        # Drain stderr concurrently: Clack may write warnings/errors to stderr
        # while we are blocked reading stdout, and a full stderr pipe would
        # deadlock the child and the provider until the timeouts fire.
        stderr_chunks: list[bytes] = []
        stderr_reader = threading.Thread(
            target=self._drain_stderr,
            args=(process, stderr_chunks),
            name="clack-stderr",
            daemon=True,
        )
        stderr_reader.start()

        translator = _ByteStreamTranslator()
        try:
            raw_output = self._drain_and_inject(process, text, translator, settings, ctx)
            try:
                returncode = process.wait(timeout=10.0)
            except subprocess.TimeoutExpired as exc:
                raise StrategyExecutionFailed(
                    "clack did not exit after its stdout reached EOF",
                    details={"provider": self.name, "command": command},
                ) from exc
            stderr_reader.join(timeout=2.0)
            stderr_bytes = b"".join(stderr_chunks)
            stderr_text = stderr_bytes.decode("utf-8", "replace").strip()
            if returncode != 0 or stderr_text:
                raise StrategyExecutionFailed(
                    f"clack exited with code {returncode}"
                    + (f" and wrote to stderr: {stderr_text[:500]}" if stderr_text else ""),
                    details={
                        "provider": self.name,
                        "command": command,
                        "exit_code": returncode,
                        "stderr": stderr_text[:2000],
                    },
                )
        finally:
            self._terminate(process)
            stderr_reader.join(timeout=1.0)
            self._close_pipes(process)

        seed = _seed(settings["seed"])
        return self._outcome(
            request,
            started=started,
            engine="clack-cli",
            details={
                "binary": binary,
                "command": command,
                "text_length": len(text),
                "chars_typed": translator.chars_typed,
                "backspaces": translator.backspaces,
                "enters": translator.enters,
                "tabs": translator.tabs,
                "skipped_control_count": len(translator.skipped_control_bytes),
                "skipped_control_bytes": translator.skipped_control_bytes[:64],
                "invalid_utf8_bytes": translator.invalid_utf8_bytes,
                "output_bytes": len(raw_output),
                **_raw_output_details(raw_output),
                "exit_code": returncode,
                "wpm": float(settings["wpm"]),
                "jitter": float(settings["jitter"]),
                "error_rate": float(settings["error_rate"]),
                "correction_rate": float(settings["correction_rate"]),
                "layout": str(settings["layout"]),
                "seed": seed,
                "license": self.license,
            },
        )

    # -- subprocess plumbing ----------------------------------------------
    def _drain_and_inject(
        self,
        process: subprocess.Popen[bytes],
        text: str,
        translator: _ByteStreamTranslator,
        settings: Mapping[str, Any],
        ctx: StrategyContext,
    ) -> bytes:
        """Read Clack stdout byte-by-byte and inject translated keystrokes."""

        if process.stdout is None:  # pragma: no cover - we always create a pipe
            raise StrategyExecutionFailed("clack stdout pipe is not available")
        writer = threading.Thread(
            target=self._feed_stdin,
            args=(process, text),
            name="clack-stdin",
            daemon=True,
        )
        writer.start()

        stdout_fd = process.stdout.fileno()
        keyboard = ctx.drivers.keyboard
        startup_timeout = float(settings["startup_timeout_s"])
        char_timeout = _effective_char_timeout(settings)
        raw_output = bytearray()
        received_any = False
        deadline = time.monotonic() + startup_timeout

        while True:
            if ctx.cancel.is_set():
                self._terminate(process)
                ctx.check_cancelled()  # raises TaskCancelled
            now = time.monotonic()
            if now >= deadline:
                timeout_s = char_timeout if received_any else startup_timeout
                phase = "next character" if received_any else "first byte"
                self._terminate(process)
                raise StrategyExecutionFailed(
                    f"clack timed out waiting for {phase} ({timeout_s}s, "
                    f"wpm={settings['wpm']}, char_timeout_s={char_timeout}, "
                    f"startup_timeout_s={startup_timeout})",
                    details={
                        "provider": self.name,
                        "phase": phase,
                        "output_bytes": len(raw_output),
                    },
                )
            ready, _, _ = select.select([stdout_fd], [], [], min(deadline - now, 0.05))
            if not ready:
                continue
            chunk = os.read(stdout_fd, 1)
            if not chunk:
                break
            received_any = True
            deadline = time.monotonic() + char_timeout
            raw_output.append(chunk[0])
            for action, value in translator.feed(chunk[0]):
                ctx.check_cancelled()
                if action == "char":
                    keyboard.type_text(value)
                else:  # key
                    keyboard.key_down(value)
                    keyboard.key_up(value)

        translator.flush()
        return bytes(raw_output)

    @staticmethod
    def _drain_stderr(process: subprocess.Popen[bytes], chunks: list[bytes]) -> None:
        if process.stderr is None:  # pragma: no cover - we always create a pipe
            return
        try:
            while True:
                chunk = process.stderr.read(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except (OSError, ValueError):  # pragma: no cover - pipe closed on terminate
            return

    @staticmethod
    def _feed_stdin(process: subprocess.Popen[bytes], text: str) -> None:
        if process.stdin is None:  # pragma: no cover - we always create a pipe
            return
        try:
            process.stdin.write(text.encode("ascii"))
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            LOGGER.debug("clack stopped reading stdin early; ignoring", exc_info=True)
        finally:
            with contextlib.suppress(OSError, ValueError):
                process.stdin.close()

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:  # pragma: no cover - race with child exit
            return
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - child ignores SIGTERM
            try:
                process.kill()
                process.wait(timeout=2.0)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass

    @staticmethod
    def _close_pipes(process: subprocess.Popen[bytes]) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()


__all__ = ["ClackProvider"]
