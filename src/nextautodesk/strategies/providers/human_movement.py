"""Default behaviour strategy: the ``human-movement`` SDK (PyPI, MIT).

``human_movement`` was designed for Windows native injection but explicitly
supports custom backends: it generates timestamped ``InputEvent`` sequences
(trajectory + motor timing from a real-input profile) and delivers them through
a :class:`human_movement.backends.base.Backend` implementation. NextAutoDesk
uses it purely as an *event generator*; the X11 driver bundle is the backend.

This provider covers 15 of the 19 operations:

* mouse: move/relative/click/double/hold/drag/scroll(vertical+horizontal)
* keyboard: type text/press/hold/hotkey/key sequence

The four cross-request *state primitives* (``mouse_down``, ``mouse_up``,
``keyboard_key_down``, ``keyboard_key_release``) are deliberately **not**
supported: every ``HumanInput`` execution releases the inputs it holds when it
finishes, so a button/key pressed by one request cannot stay held for a later
release request. Those primitives are routed to the ``direct`` provider instead
(see ``config/default.yml``).

Pacing ("realtime") is owned by the SDK; holds and inter-key gaps use the
cancel-aware ``StrategyContext.sleep`` so a task cancel can stop execution.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...drivers.base import DriverBundle, DriverError
from ...errors import (
    NextAutoDeskError,
    StrategyExecutionFailed,
    StrategyUnavailable,
    TaskCancelled,
)
from ...models import MouseButton, ProviderAvailability, StrategyOutcome
from ...operations import ALL_OPERATIONS, Operation
from ..base import OperationRequest, StrategyContext, StrategyProvider

LOGGER = logging.getLogger(__name__)

_ENGINES: frozenset[str] = frozenset(
    {"minimum_jerk", "sigma_lognormal", "feedback", "neural", "submovements"}
)
_MOUSE_BUTTONS: frozenset[str] = frozenset({"left", "right", "middle"})
_SCROLL_FLUSH_THRESHOLD = 0.5


def human_movement_version() -> str | None:
    """Installed ``human-movement`` version, if importable."""

    try:
        from importlib.metadata import version

        return version("human-movement")
    except Exception:  # pragma: no cover - metadata unavailable
        try:
            import human_movement

            return getattr(human_movement, "__version__", None)
        except Exception:
            return None


class HumanMovementParams(BaseModel):
    """Validated provider parameters (merged from configuration)."""

    model_config = ConfigDict(extra="forbid")

    profile: str = Field(
        default="standard",
        description="'standard', 'population' or a path to a profile JSON file",
    )
    engine: str | None = Field(
        default=None,
        description=f"Trajectory engine; one of {sorted(_ENGINES)} (default: profile engine)",
    )
    seed: int | None = Field(default=None, description="Deterministic RNG seed")
    realtime: bool = Field(
        default=True, description="Pace events with their generated timestamps"
    )
    stop_on_physical_input: bool = Field(
        default=False,
        description="Stop when the backend reports physical user activity",
    )
    target_width: float = Field(
        default=24.0, gt=0, le=2000, description="Fitts' law target width in pixels"
    )
    hold_default_ms: float = Field(
        default=120.0, ge=0, le=600_000, description="Default hold duration for mouse/key holds"
    )
    sequence_interval_ms: float = Field(
        default=150.0,
        ge=0,
        le=600_000,
        description="Gap between keys of a key sequence",
    )


class HumanMovementBackend:
    """Adapter mapping ``human_movement`` ``InputEvent``s onto the driver bundle."""

    def __init__(self, drivers: DriverBundle) -> None:
        from human_movement.types import BackendCapabilities  # lazy optional dependency

        self._drivers = drivers
        self.capabilities = BackendCapabilities(
            execution=True,
            recording=False,
            cursor_position=True,
            unicode_text=drivers.keyboard.supports_unicode(),
            physical_takeover=False,
            platform="x11",
        )
        self.events_sent = 0
        self.scroll_detents = 0
        self._scroll_x = 0.0
        self._scroll_y = 0.0
        self._closed = False

    # -- protocol ----------------------------------------------------------
    def position(self) -> Any:
        from human_movement.types import Point as MovementPoint

        current = self._drivers.mouse.position()
        return MovementPoint(current.x, current.y)

    def bounds(self) -> Any:
        from human_movement.types import Rect as MovementRect

        size = self._drivers.screen.size()
        return MovementRect(0.0, 0.0, float(size.width), float(size.height))

    def validate_event(self, event: Any) -> None:
        kind = event.kind
        if kind == "move":
            if event.x is None or event.y is None:
                raise ValueError("move events require x and y")
            bounds = self.bounds()
            from human_movement.types import Point as MovementPoint

            if not bounds.contains(MovementPoint(event.x, event.y)):
                raise ValueError(
                    f"move target ({event.x:.1f}, {event.y:.1f}) is outside the desktop "
                    f"({bounds.width:.0f}x{bounds.height:.0f})"
                )
        elif kind in {"button_down", "button_up"}:
            if event.button not in _MOUSE_BUTTONS:
                raise ValueError(f"unsupported mouse button {event.button!r}")
        elif kind in {"key_down", "key_up"}:
            if not event.key:
                raise ValueError("key events require a key name")
        elif kind in {"text_down", "text_up"}:
            if not event.text or len(event.text) != 1:
                raise ValueError("text events require exactly one code point")
        elif kind == "scroll":
            return
        else:  # pragma: no cover - SDK enum is closed
            raise ValueError(f"unknown event kind {kind!r}")

    def send(self, event: Any) -> None:
        self.events_sent += 1
        kind = event.kind
        if kind == "move":
            self._drivers.mouse.move_to(float(event.x or 0), float(event.y or 0))
        elif kind == "button_down":
            self._drivers.mouse.button_down(_as_button(event.button))
        elif kind == "button_up":
            self._drivers.mouse.button_up(_as_button(event.button))
        elif kind == "key_down":
            self._drivers.keyboard.key_down(str(event.key))
        elif kind == "key_up":
            self._drivers.keyboard.key_up(str(event.key))
        elif kind == "text_down":
            self._drivers.keyboard.char_down(str(event.text))
        elif kind == "text_up":
            self._drivers.keyboard.char_up(str(event.text))
        elif kind == "scroll":
            self._accumulate_scroll(float(event.dx), float(event.dy))
        else:  # pragma: no cover - validated earlier
            raise ValueError(f"unknown event kind {kind!r}")

    def activity(self) -> int:
        # Backend activity counters are only consulted when
        # stop_on_physical_input=True; the X11 backend does not track it.
        return 0

    def emergency_stop(self) -> bool:
        return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.flush_scroll()

    # -- scroll accumulation ----------------------------------------------
    def _accumulate_scroll(self, dx: float, dy: float) -> None:
        self._scroll_x += dx
        self._scroll_y += dy
        self._emit_scroll()

    def _emit_scroll(self) -> None:
        detents_x = int(self._scroll_x)
        detents_y = int(self._scroll_y)
        if detents_x or detents_y:
            self._drivers.mouse.scroll(detents_x, detents_y)
            self.scroll_detents += abs(detents_x) + abs(detents_y)
            self._scroll_x -= detents_x
            self._scroll_y -= detents_y

    def flush_scroll(self) -> None:
        """Emit the rounded remainder of a fractional scroll gesture."""

        detents_x = round(self._scroll_x) if abs(self._scroll_x) >= _SCROLL_FLUSH_THRESHOLD else 0
        detents_y = round(self._scroll_y) if abs(self._scroll_y) >= _SCROLL_FLUSH_THRESHOLD else 0
        if detents_x or detents_y:
            self._drivers.mouse.scroll(detents_x, detents_y)
            self.scroll_detents += abs(detents_x) + abs(detents_y)
            self._scroll_x -= detents_x
            self._scroll_y -= detents_y


def _as_button(button: str | None) -> MouseButton:
    if button not in _MOUSE_BUTTONS:  # pragma: no cover - validated earlier
        raise StrategyExecutionFailed(f"unsupported mouse button {button!r}")
    return button  # type: ignore[return-value]


class _CancelBridge(threading.Thread):
    """Bridge ``StrategyContext.cancel`` onto ``HumanInput.cancel()``."""

    def __init__(self, human_input: Any, cancel: threading.Event, poll_s: float = 0.02) -> None:
        super().__init__(name="human-movement-cancel", daemon=True)
        self._human_input = human_input
        self._cancel = cancel
        self._poll_s = poll_s
        self._stop_event = threading.Event()

    def run(self) -> None:  # pragma: no cover - timing dependent
        while not self._stop_event.wait(self._poll_s):
            if self._cancel.is_set():
                try:
                    self._human_input.cancel()
                except Exception:  # pragma: no cover - defensive
                    LOGGER.debug("human_movement: cancel bridge failed", exc_info=True)
                return

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=1.0)


class HumanMovementProvider(StrategyProvider):
    """``human-movement`` based provider (default for every operation)."""

    name: ClassVar[str] = "human_movement"
    version: ClassVar[str] = human_movement_version() or "0.1.6"
    description: ClassVar[str] = (
        "Default human-like strategy driven by the human-movement SDK "
        "(trajectory + motor timing from real-input profiles)"
    )
    # ``HumanInput`` releases everything it pressed when its context manager
    # closes (end of every execute()), so state primitives that must persist
    # across HTTP requests (hold/release a button or a key) are not supported;
    # ``direct`` serves them. Everything else runs through human-movement.
    supported: ClassVar[frozenset[Operation]] = frozenset(ALL_OPERATIONS) - {
        Operation.MOUSE_DOWN,
        Operation.MOUSE_UP,
        Operation.KEYBOARD_KEY_DOWN,
        Operation.KEYBOARD_KEY_RELEASE,
    }

    # -- availability / validation ----------------------------------------
    def availability(self) -> ProviderAvailability:
        availability = super().availability()
        try:
            import human_movement  # noqa: F401 - availability probe only
        except Exception as exc:
            availability.available = False
            availability.reason = (
                f"human-movement is not importable ({type(exc).__name__}: {exc})"
            )
            return availability
        availability.version = human_movement_version()
        return availability

    def validate_params(self, params: Mapping[str, Any]) -> None:
        try:
            parsed = HumanMovementParams.model_validate(dict(params))
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
                for error in exc.errors()
            )
            raise ValueError(f"invalid human_movement parameters: {details}") from exc
        if parsed.engine is not None and parsed.engine not in _ENGINES:
            raise ValueError(
                f"engine must be one of {sorted(_ENGINES)}, got {parsed.engine!r}"
            )
        if parsed.profile in {"standard", "population"}:
            return
        from pathlib import Path

        path = Path(parsed.profile).expanduser()
        if not path.is_file():
            raise ValueError(
                f"profile must be 'standard', 'population' or an existing profile file, "
                f"got {parsed.profile!r}"
            )

    # -- execution ---------------------------------------------------------
    def execute(
        self,
        request: OperationRequest,
        params: Mapping[str, Any],
        ctx: StrategyContext,
    ) -> StrategyOutcome:
        self._ensure_supported(request.operation)
        cfg = HumanMovementParams.model_validate(dict(params))
        parsed_profile = self._load_profile(cfg)
        backend = HumanMovementBackend(ctx.drivers)
        started = time.monotonic()
        human_input = self._create_human_input(cfg, parsed_profile, backend, ctx)
        bridge = _CancelBridge(human_input, ctx.cancel)
        bridge.start()
        engine: str | None = None
        fallback_reason: str | None = None
        try:
            result = self._dispatch(human_input, request, cfg, ctx)
            if result is not None:
                engine = getattr(result, "engine", None)
                fallback_reason = getattr(result, "fallback_reason", None)
                if fallback_reason is None:
                    fallback_reason = getattr(human_input, "last_fallback_reason", None)
        except TaskCancelled:
            raise
        except Exception as exc:
            self._raise_execution_error(exc, request, ctx)
        finally:
            bridge.stop()
            try:
                backend.close()
            except Exception:  # best-effort scroll flush: must not mask the outcome
                ctx.logger.exception("human_movement: failed to flush/close the backend")
            try:
                human_input.close()
            except Exception:  # pragma: no cover - best-effort input release
                ctx.logger.exception("human_movement: failed to release/close input")
        return self._outcome(
            request,
            started=started,
            engine=engine,
            fallback_reason=fallback_reason,
            details={
                "engine": engine,
                "fallback_reason": fallback_reason,
                "realtime": cfg.realtime,
                "profile": cfg.profile,
                "target_width": cfg.target_width,
                "events": backend.events_sent,
                "scroll_detents": backend.scroll_detents,
                "provider_version": human_movement_version(),
            },
        )

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _load_profile(cfg: HumanMovementParams) -> Any:
        """Return what ``HumanInput`` should receive for ``profile``."""

        if cfg.profile in {"standard", "population"}:
            return cfg.profile
        from human_movement import Profile  # lazy optional dependency

        try:
            return Profile.load(cfg.profile)
        except Exception as exc:
            raise StrategyUnavailable(
                f"cannot load human_movement profile {cfg.profile!r}: {exc}",
                details={"profile": cfg.profile},
            ) from exc

    def _create_human_input(
        self,
        cfg: HumanMovementParams,
        profile: Any,
        backend: HumanMovementBackend,
        ctx: StrategyContext,
    ) -> Any:
        from human_movement import HumanInput
        from human_movement.errors import CalibrationRequired, ProfileError

        seed = cfg.seed if cfg.seed is not None else ctx.seed
        try:
            return HumanInput(
                profile=profile,
                setup="never",
                backend=backend,
                seed=seed,
                engine=cfg.engine,
                realtime=cfg.realtime,
                stop_on_physical_input=cfg.stop_on_physical_input,
            )
        except CalibrationRequired as exc:
            raise StrategyUnavailable(
                f"human_movement profile {cfg.profile!r} requires calibration: {exc}",
                details={"profile": cfg.profile},
            ) from exc
        except (ProfileError, ValueError) as exc:
            raise StrategyUnavailable(
                f"human_movement configuration rejected: {exc}",
                details={"profile": cfg.profile, "engine": cfg.engine},
            ) from exc

    def _dispatch(
        self,
        human_input: Any,
        request: OperationRequest,
        cfg: HumanMovementParams,
        ctx: StrategyContext,
    ) -> Any:
        operation = request.operation
        target_width = cfg.target_width

        if operation is Operation.MOUSE_MOVE:
            self._require(request, "x", "y")
            return human_input.move_to(request.x, request.y, target_width=target_width)
        if operation is Operation.MOUSE_MOVE_RELATIVE:
            self._require(request, "dx", "dy")
            return human_input.move_by(request.dx, request.dy, target_width=target_width)
        if operation in {
            Operation.MOUSE_CLICK_LEFT,
            Operation.MOUSE_CLICK_RIGHT,
            Operation.MOUSE_CLICK_MIDDLE,
        }:
            button = {
                Operation.MOUSE_CLICK_LEFT: "left",
                Operation.MOUSE_CLICK_RIGHT: "right",
                Operation.MOUSE_CLICK_MIDDLE: "middle",
            }[operation]
            return human_input.click(button, clicks=max(1, int(request.clicks or 1)))
        if operation is Operation.MOUSE_DOUBLE_CLICK:
            return human_input.click(request.button or "left", clicks=2)
        if operation is Operation.MOUSE_HOLD:
            self._hold_mouse(human_input, request, cfg, ctx)
            return None
        if operation is Operation.MOUSE_DRAG:
            self._require(request, "x", "y")
            duration = (
                request.duration_ms / 1000.0 if request.duration_ms is not None else None
            )
            return human_input.drag_to(
                request.x,
                request.y,
                button=request.button or "left",
                duration=duration,
            )
        if operation is Operation.MOUSE_SCROLL_VERTICAL:
            self._require(request, "dy")
            return human_input.scroll(dy=request.dy)
        if operation is Operation.MOUSE_SCROLL_HORIZONTAL:
            self._require(request, "dx")
            return human_input.scroll(dx=request.dx)
        if operation is Operation.KEYBOARD_TYPE_TEXT:
            self._require(request, "text")
            return human_input.type_text(request.text)
        if operation is Operation.KEYBOARD_KEY_PRESS:
            self._require(request, "key")
            return human_input.press(request.key)
        if operation is Operation.KEYBOARD_KEY_HOLD:
            self._require(request, "key")
            self._hold_key(human_input, request, cfg, ctx)
            return None
        if operation is Operation.KEYBOARD_HOTKEY:
            if not request.keys:
                raise StrategyExecutionFailed(
                    "keyboard_hotkey requires at least one key",
                    details={"operation": operation.value},
                )
            return human_input.hotkey(*request.keys)
        if operation is Operation.KEYBOARD_KEY_SEQUENCE:
            if not request.keys:
                raise StrategyExecutionFailed(
                    "keyboard_key_sequence requires at least one key",
                    details={"operation": operation.value},
                )
            self._key_sequence(human_input, request, cfg, ctx)
            return None
        raise StrategyExecutionFailed(  # pragma: no cover - supported covers all
            f"human_movement cannot dispatch {operation.value!r}",
            details={"operation": operation.value},
        )

    def _hold_mouse(
        self,
        human_input: Any,
        request: OperationRequest,
        cfg: HumanMovementParams,
        ctx: StrategyContext,
    ) -> None:
        button = request.button or "left"
        hold_ms = request.hold_ms if request.hold_ms is not None else cfg.hold_default_ms
        human_input.button_down(button)
        try:
            ctx.sleep(max(0.0, hold_ms / 1000.0))
        except TaskCancelled:
            _release_all_quietly(human_input, ctx.logger)
            raise
        human_input.button_up(button)

    def _hold_key(
        self,
        human_input: Any,
        request: OperationRequest,
        cfg: HumanMovementParams,
        ctx: StrategyContext,
    ) -> None:
        self._require(request, "key")
        hold_ms = request.hold_ms if request.hold_ms is not None else cfg.hold_default_ms
        human_input.key_down(request.key)
        try:
            ctx.sleep(max(0.0, hold_ms / 1000.0))
        except TaskCancelled:
            _release_all_quietly(human_input, ctx.logger)
            raise
        human_input.key_up(request.key)

    def _key_sequence(
        self,
        human_input: Any,
        request: OperationRequest,
        cfg: HumanMovementParams,
        ctx: StrategyContext,
    ) -> None:
        interval_s = max(0.0, cfg.sequence_interval_ms / 1000.0)
        for index, key in enumerate(request.keys):
            if index:
                ctx.sleep(interval_s)
            human_input.press(key)

    @staticmethod
    def _require(request: OperationRequest, *names: str) -> None:
        missing = [name for name in names if getattr(request, name) is None]
        if missing:
            raise StrategyExecutionFailed(
                f"operation {request.operation.value} requires fields: {missing}",
                details={"operation": request.operation.value, "missing": missing},
            )

    def _raise_execution_error(
        self,
        exc: Exception,
        request: OperationRequest,
        ctx: StrategyContext,
    ) -> None:
        from human_movement.errors import CalibrationRequired, HumanMovementError, InputCancelled

        if isinstance(exc, InputCancelled):
            raise TaskCancelled("human_movement input cancelled") from exc
        if isinstance(exc, CalibrationRequired):
            raise StrategyUnavailable(
                f"human_movement profile is not calibrated: {exc}",
                details={"operation": request.operation.value},
            ) from exc
        if isinstance(exc, NextAutoDeskError):
            # Domain errors raised by the dispatch helpers keep their code.
            raise exc
        # The SDK also validates the generated events against the backend (for
        # example rejecting a target outside the desktop) with plain
        # ``ValueError``/``RuntimeError``. Normalise those too, so a bad target
        # never escapes the strategy layer as an unhandled exception.
        if not isinstance(exc, (DriverError, HumanMovementError)):
            ctx.logger.warning(
                "human_movement: unexpected %s for %s: %s",
                type(exc).__name__,
                request.operation.value,
                exc,
            )
        raise StrategyExecutionFailed(
            f"human_movement {request.operation.value} failed: "
            f"{type(exc).__name__}: {exc}",
            details={"operation": request.operation.value, "provider": self.name},
        ) from exc


def _release_all_quietly(human_input: Any, logger: logging.Logger) -> None:
    try:
        human_input.release_all()
    except Exception:  # pragma: no cover - best effort during cancellation
        logger.exception("human_movement: failed to release held input after cancel")


__all__ = [
    "HumanMovementBackend",
    "HumanMovementParams",
    "HumanMovementProvider",
    "human_movement_version",
]
