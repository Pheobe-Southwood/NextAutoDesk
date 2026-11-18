"""WindMouse strategy provider (optional mouse-movement policy).

Wraps the pure path generator of `windmouse 1.0.2
<https://github.com/AsfhtgkDavid/windmouse>`_ (GPL-3.0-only), a Python port
of Ben Land's WindMouse algorithm
(https://ben.land/post/2021/04/25/windmouse-human-mouse-movement/).

The upstream controller paces itself with ``pyautogui.moveTo(x, y,
duration=step_duration)`` — i.e. the interpolation is delegated to the driver,
not emulated point-by-point in the strategy. This provider keeps the exact same
pacing: every yielded point is forwarded to
``ctx.drivers.mouse.move_to(x, y, duration_s=step_duration_s)`` so the driver
engine's own tween produces the motion. The generator has no timing model of its
own; ``step_duration_s`` is the only pacing knob.

``windmouse`` is GPL-3.0-only and therefore an *optional* extra: it is
imported lazily and reported unavailable (with a reason) when not installed.
"""

from __future__ import annotations

import importlib
import logging
import math
import time
from collections.abc import Mapping
from typing import Any, ClassVar

from ...errors import StrategyExecutionFailed, TaskCancelled
from ...models import ProviderAvailability
from ...operations import Operation
from ..base import OperationRequest, StrategyContext, StrategyOutcome, StrategyProvider

LOGGER = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "gravity": 9.0,
    "wind": 3.0,
    "max_step": 15.0,
    "damped_distance": 12.0,
    "step_duration_s": 0.1,
    "seed": None,
}

_SUPPORTED = frozenset({Operation.MOUSE_MOVE, Operation.MOUSE_MOVE_RELATIVE})


def _settings(params: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(params) - set(_DEFAULTS))
    if unknown:
        raise ValueError(f"unknown windmouse parameters: {unknown}")
    settings = dict(_DEFAULTS)
    settings.update(params)
    return settings


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
    if not 0 <= value < 2**32:
        raise ValueError("seed must be in [0, 2**32)")
    return value


class WindMouseProvider(StrategyProvider):
    """WindMouse reference-implementation strategy (mouse moves only, GPL extra)."""

    name: ClassVar[str] = "windmouse"
    version: ClassVar[str] = "1.0.2"
    description: ClassVar[str] = (
        "WindMouse reference mouse-movement strategy (windmouse 1.0.2, GPL-3.0-only; "
        "library path generation plus driver-level step pacing; "
        "mouse_move/mouse_move_relative only)"
    )
    supported: ClassVar[frozenset[Operation]] = _SUPPORTED
    license: ClassVar[str] = "GPL-3.0-only"
    # See HumanMoveMouseProvider: probing imports numpy (via windmouse.core) and
    # runs on every health/strategies request, so cache the answer.
    _availability_cache: ProviderAvailability | None = None

    # -- introspection -----------------------------------------------------
    def availability(self) -> ProviderAvailability:
        if self._availability_cache is None:
            self._availability_cache = self._probe_availability()
        return self._availability_cache.model_copy(deep=True)

    def _probe_availability(self) -> ProviderAvailability:
        base: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "supported_operations": sorted(self.supported, key=lambda op: op.value),
        }
        try:
            module = importlib.import_module("windmouse.core")
            generator = module.wind_mouse
        except Exception as exc:  # optional dependency: any failure means unavailable
            return ProviderAvailability(
                **base,
                available=False,
                reason=f"windmouse is not installed/importable: {exc}",
                details={"package": "windmouse", "license": self.license},
            )
        if not callable(generator):
            return ProviderAvailability(
                **base,
                available=False,
                reason="windmouse.core.wind_mouse is missing or not callable",
            )
        return ProviderAvailability(
            **base,
            available=True,
            details={"module": "windmouse.core.wind_mouse", "license": self.license},
        )

    def validate_params(self, params: Mapping[str, Any]) -> None:
        settings = _settings(params)
        _number(settings["gravity"], "gravity", minimum=0.0, maximum=1000.0, inclusive_minimum=True)
        _number(settings["wind"], "wind", minimum=0.0, maximum=1000.0, inclusive_minimum=True)
        _number(settings["max_step"], "max_step", minimum=0.0, maximum=1000.0)
        _number(
            settings["damped_distance"],
            "damped_distance",
            minimum=0.0,
            maximum=100000.0,
            inclusive_minimum=True,
        )
        _number(settings["step_duration_s"], "step_duration_s", minimum=0.0, maximum=10.0)
        _seed(settings["seed"])

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

        mouse = ctx.drivers.mouse
        position = mouse.position()
        start_x = round(float(position.x))
        start_y = round(float(position.y))
        if request.operation is Operation.MOUSE_MOVE:
            request.require("x", "y")
            target_x = round(float(request.x))
            target_y = round(float(request.y))
        else:
            request.require("dx", "dy")
            target_x = round(start_x + float(request.dx))
            target_y = round(start_y + float(request.dy))

        try:
            from windmouse.core import wind_mouse
        except Exception as exc:  # optional dependency surface
            raise StrategyExecutionFailed(
                f"windmouse path generator is unavailable: {exc}",
                details={"provider": self.name},
            ) from exc

        seed = _seed(settings["seed"])
        if seed is not None:
            try:
                import numpy as np
            except Exception as exc:  # windmouse depends on numpy itself
                raise StrategyExecutionFailed(
                    f"numpy is required for a reproducible WindMouse seed: {exc}",
                    details={"provider": self.name},
                ) from exc
            np.random.seed(seed)

        step_duration_s = float(settings["step_duration_s"])
        path_length_px = 0.0
        previous = (float(start_x), float(start_y))
        points_yielded = 0
        try:
            generator = wind_mouse(
                start_x,
                start_y,
                target_x,
                target_y,
                gravity_magnitude=float(settings["gravity"]),
                wind_magnitude=float(settings["wind"]),
                max_step=float(settings["max_step"]),
                damped_distance=float(settings["damped_distance"]),
            )
            for point_x, point_y in generator:
                ctx.check_cancelled()
                mouse.move_to(float(point_x), float(point_y), duration_s=step_duration_s)
                points_yielded += 1
                path_length_px += math.hypot(
                    float(point_x) - previous[0], float(point_y) - previous[1]
                )
                previous = (float(point_x), float(point_y))
        except TaskCancelled:
            raise
        except StrategyExecutionFailed:
            raise
        except Exception as exc:  # upstream generator may raise anything
            raise StrategyExecutionFailed(
                f"windmouse execution failed (generator/driver pacing): {exc}",
                details={"provider": self.name, "step_duration_s": step_duration_s},
            ) from exc
        ctx.check_cancelled()
        # WindMouse stops once its remaining distance drops below the step
        # threshold, so the last generated point can be a pixel short of the
        # requested target; finish exactly on it.
        final = mouse.position()
        if (round(float(final.x)), round(float(final.y))) != (target_x, target_y):
            mouse.move_to(float(target_x), float(target_y), duration_s=step_duration_s)
            points_yielded += 1
            path_length_px += math.hypot(target_x - previous[0], target_y - previous[1])
            final = mouse.position()
        return self._outcome(
            request,
            started=started,
            engine="windmouse.core.wind_mouse",
            details={
                "start": [start_x, start_y],
                "target": [target_x, target_y],
                "final_position": [float(final.x), float(final.y)],
                "points_yielded": points_yielded,
                "path_length_px": round(path_length_px, 3),
                "step_duration_s": step_duration_s,
                "gravity": float(settings["gravity"]),
                "wind": float(settings["wind"]),
                "max_step": float(settings["max_step"]),
                "damped_distance": float(settings["damped_distance"]),
                "seed": seed,
                "license": self.license,
            },
        )


__all__ = ["WindMouseProvider"]
