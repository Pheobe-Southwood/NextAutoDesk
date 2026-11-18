"""HumanMoveMouse strategy provider (optional mouse-movement policy).

Adapters the *pure trajectory generator* of `HumanMoveMouse 1.2.0
<https://github.com/TomokotoKiyoshi/HumanMoveMouse>`_ (MIT) to the NextAutoDesk
driver layer. The upstream ``HumanMouseController`` is deliberately **not** used:
it talks to PyAutoGUI directly and configures global PyAutoGUI state at import
time. This provider calls
``humanmouse.models.generate_mouse_trajectory(...)`` (a PCA+GMM statistical
trajectory model with a minimum-jerk velocity profile) and paces the returned
``(xy, dt)`` samples through ``ctx.drivers.mouse.move_to`` — the same pacing the
upstream controller uses (``dt[i] / speed_factor``), only routed through our
strategy-agnostic driver bundle.

The optional package is imported lazily so this module stays importable when
HumanMoveMouse (and its heavy numpy/pandas/scipy/scikit-learn dependency chain)
is not installed; :meth:`HumanMoveMouseProvider.availability` then reports
``available=False`` with a reason instead of breaking application startup.
"""

from __future__ import annotations

import importlib
import logging
import math
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from ...errors import StrategyExecutionFailed
from ...models import ProviderAvailability
from ...operations import Operation
from ..base import OperationRequest, StrategyContext, StrategyOutcome, StrategyProvider

LOGGER = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "model_path": None,
    "num_points": 100,
    "jitter_amplitude": 0.3,
    "speed_factor": 1.0,
    "seed": None,
}

_SUPPORTED = frozenset({Operation.MOUSE_MOVE, Operation.MOUSE_MOVE_RELATIVE})


def _settings(params: Mapping[str, Any]) -> dict[str, Any]:
    """Merge caller params over provider defaults.

    Unknown keys are rejected: silently ignoring a typo would report the
    configuration as applied while the default is used instead.
    """

    unknown = sorted(set(params) - set(_DEFAULTS))
    if unknown:
        raise ValueError(f"unknown human_move_mouse parameters: {unknown}")
    settings = dict(_DEFAULTS)
    settings.update(params)
    return settings


def _number(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float | None = None,
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
    if maximum is not None and numeric > maximum:
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


class HumanMoveMouseProvider(StrategyProvider):
    """Pre-trained statistical mouse-trajectory strategy (mouse moves only)."""

    name: ClassVar[str] = "human_move_mouse"
    version: ClassVar[str] = "1.2.0"
    description: ClassVar[str] = (
        "Pre-trained PCA+GMM statistical trajectory mouse-movement strategy "
        "(HumanMoveMouse 1.2.0, MIT; generation only plus driver-level pacing; "
        "mouse_move/mouse_move_relative only)"
    )
    supported: ClassVar[frozenset[Operation]] = _SUPPORTED
    license: ClassVar[str] = "MIT"
    # ``availability()`` runs on every /health and /strategies probe and imports
    # humanmouse (numpy/pandas/scikit-learn); cache the environment answer.
    # Availability only depends on installed packages/the bundled model file,
    # which cannot change while the service is running.
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
            models = importlib.import_module("humanmouse.models")
            model_path = models.get_default_model_path()
        except Exception as exc:  # optional dependency: any failure means unavailable
            return ProviderAvailability(
                **base,
                available=False,
                reason=f"HumanMoveMouse is not usable in this environment: {exc}",
                details={"package": "HumanMoveMouse", "version": self.version},
            )
        if not os.path.isfile(model_path):
            return ProviderAvailability(
                **base,
                available=False,
                reason=f"bundled HumanMoveMouse model file is missing: {model_path}",
            )
        return ProviderAvailability(
            **base,
            available=True,
            details={"model_path": model_path, "license": self.license},
        )

    def validate_params(self, params: Mapping[str, Any]) -> None:
        settings = _settings(params)

        num_points = settings["num_points"]
        if isinstance(num_points, bool) or not isinstance(num_points, int):
            raise ValueError("num_points must be an integer")
        if not 2 <= num_points <= 1000:
            raise ValueError("num_points must be between 2 and 1000")

        _number(
            settings["jitter_amplitude"],
            "jitter_amplitude",
            minimum=0.0,
            maximum=50.0,
            inclusive_minimum=True,
        )
        _number(settings["speed_factor"], "speed_factor", minimum=0.0, maximum=100.0)
        _seed(settings["seed"])

        model_path = settings["model_path"]
        if model_path is not None:
            if not isinstance(model_path, (str, os.PathLike)):
                raise ValueError("model_path must be a string path or null")
            resolved = Path(os.path.expanduser(os.fspath(model_path)))
            if not resolved.is_file():
                raise ValueError(f"model_path does not exist: {resolved}")

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

        start = ctx.drivers.mouse.position()
        if request.operation is Operation.MOUSE_MOVE:
            request.require("x", "y")
            target = (float(request.x), float(request.y))  # require() guarantees both are set
        else:
            request.require("dx", "dy")
            target = (  # require() guarantees both are set
                float(start.x) + float(request.dx),
                float(start.y) + float(request.dy),
            )

        model_path = settings["model_path"]
        try:
            models = importlib.import_module("humanmouse.models")
            if model_path is None:
                model_path = models.get_default_model_path()
            generate = models.generate_mouse_trajectory
        except Exception as exc:  # optional dependency surface
            raise StrategyExecutionFailed(
                f"HumanMoveMouse trajectory generator is unavailable: {exc}",
                details={"provider": self.name},
            ) from exc

        num_points = int(settings["num_points"])
        jitter = float(settings["jitter_amplitude"])
        seed = _seed(settings["seed"])
        speed_factor = float(settings["speed_factor"])
        try:
            xy, dt = generate(
                model_path=os.fspath(model_path),
                start_point=(float(start.x), float(start.y)),
                end_point=target,
                num_points=num_points,
                jitter_amplitude=jitter,
                seed=seed,
            )
        except Exception as exc:  # upstream may raise anything (bad model, sklearn, ...)
            raise StrategyExecutionFailed(
                f"HumanMoveMouse trajectory generation failed: {exc}",
                details={"provider": self.name, "model_path": str(model_path)},
            ) from exc

        points = [(float(sample[0]), float(sample[1])) for sample in xy]
        delays = [float(value) for value in dt]
        if len(points) < 2:
            raise StrategyExecutionFailed(
                "HumanMoveMouse returned a degenerate trajectory "
                f"({len(points)} points, requested {num_points})",
                details={"provider": self.name, "model_path": str(model_path)},
            )

        mouse = ctx.drivers.mouse
        path_length_px = 0.0
        previous = (float(start.x), float(start.y))
        points_moved = 0
        for index, (px, py) in enumerate(points):
            ctx.check_cancelled()
            mouse.move_to(px, py)
            points_moved += 1
            path_length_px += math.hypot(px - previous[0], py - previous[1])
            previous = (px, py)
            if index < len(delays) and delays[index] > 0:
                ctx.sleep(delays[index] / speed_factor)
        ctx.check_cancelled()
        # The statistical generator aims at the requested endpoint but jitter
        # can leave the pointer a pixel or two short; the operation contract is
        # an exact final position, so align it explicitly.
        final = mouse.position()
        if abs(float(final.x) - target[0]) > 0.5 or abs(float(final.y) - target[1]) > 0.5:
            mouse.move_to(target[0], target[1])
            points_moved += 1
            path_length_px += math.hypot(target[0] - previous[0], target[1] - previous[1])
            final = mouse.position()
        return self._outcome(
            request,
            started=started,
            engine="humanmouse.generate_mouse_trajectory (PCA+GMM + minimum-jerk)",
            details={
                "model_path": os.fspath(model_path),
                "num_points": len(points),
                "requested_num_points": num_points,
                "jitter_amplitude": jitter,
                "speed_factor": speed_factor,
                "seed": seed,
                "start": [float(start.x), float(start.y)],
                "target": [target[0], target[1]],
                "final_position": [float(final.x), float(final.y)],
                "points_moved": points_moved,
                "path_length_px": round(path_length_px, 3),
                "license": self.license,
            },
        )


__all__ = ["HumanMoveMouseProvider"]
