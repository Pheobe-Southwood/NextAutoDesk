"""Desktop service facade.

:class:`DesktopService` is the integration point between the transport layer
(FastAPI / tasks) and the automation layers (drivers, strategies, actions,
serial executor). It owns the runtime objects and provides the small, stable
surface the HTTP layer needs.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from . import API_VERSION, __version__
from .actions.base import ActionContext, ActionHandler, ActionInfo
from .actions.registry import ActionRegistry
from .config import AppConfig
from .drivers.base import DriverBundle, DriverError
from .errors import (
    ActionFailed,
    ActionTimeout,
    DesktopUnavailable,
    InvalidArgument,
    NextAutoDeskError,
    ReloadError,
    StrategyUnavailable,
)
from .execution import SerialExecutor
from .models import ActionResult, OperationStrategyView, Point, Size, utc_now
from .perception.service import PerceptionService
from .storage import DataStore
from .strategies.registry import StrategyRegistry

LOGGER = logging.getLogger(__name__)


class DesktopService:
    """Owns the runtime state of one virtual desktop node."""

    def __init__(
        self,
        config: AppConfig,
        *,
        store: DataStore,
        drivers: DriverBundle,
        strategies: StrategyRegistry,
        actions: ActionRegistry,
        executor: SerialExecutor,
        perception: PerceptionService | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.drivers = drivers
        self.strategies = strategies
        self.actions = actions
        self.executor = executor
        self.logger = logger or LOGGER
        self.perception = perception or PerceptionService(
            config.perception,
            drivers=drivers,
            store=store,
            logger=self.logger,
        )
        self.started_at = time.monotonic()
        self.started_at_utc = utc_now()
        self._started = False
        self._lock = threading.Lock()
        # Probed once during startup; /info must not spawn subprocesses per call.
        self._screen_size: Size | None = None
        self._window_manager: str | None = None

    # -- construction ------------------------------------------------------
    @classmethod
    def build(cls, config: AppConfig, *, logger: logging.Logger | None = None) -> DesktopService:
        from .actions import default_action_registry
        from .drivers.factory import create_driver_bundle

        logger = logger or LOGGER
        store = DataStore(config)
        store.ensure_directories()
        drivers = create_driver_bundle(config)
        strategies = StrategyRegistry.build(config.strategies, logger=logger)
        actions = default_action_registry()
        executor = SerialExecutor(
            max_queue_size=config.tasks.max_queue_size,
            name="desktop",
            logger=logger,
        )
        return cls(
            config,
            store=store,
            drivers=drivers,
            strategies=strategies,
            actions=actions,
            executor=executor,
            logger=logger,
        )

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._probe_desktop()
            self.executor.start()
            self._started = True
            self.logger.info(
                "desktop service ready (display=%s screen=%s actions=%d providers=%d)",
                self.config.desktop.display,
                self.screen_size().model_dump(),
                len(self.actions),
                len(self.strategies.providers()),
            )

    def shutdown(self, grace_s: float | None = None) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
        grace = self.config.tasks.shutdown_grace_s if grace_s is None else grace_s
        self.executor.shutdown(grace_s=grace)
        self.logger.info("desktop service stopped")

    @property
    def started(self) -> bool:
        return self._started

    # -- desktop probes ----------------------------------------------------
    def _probe_desktop(self) -> None:
        try:
            size = self.drivers.screen.size()
            self.drivers.mouse.position()
        except DriverError as exc:
            raise DesktopUnavailable(
                f"X11 desktop is not reachable (display {self.config.desktop.display!r}): {exc}",
                details={"display": self.config.desktop.display, "driver": exc.driver},
            ) from exc
        except Exception as exc:
            raise DesktopUnavailable(
                f"X11 desktop probe failed (display {self.config.desktop.display!r}): "
                f"{type(exc).__name__}: {exc}",
                details={"display": self.config.desktop.display},
            ) from exc
        self._screen_size = size
        if size.width != self.config.desktop.width or size.height != self.config.desktop.height:
            self.logger.warning(
                "configured desktop size %sx%s differs from X screen %sx%s",
                self.config.desktop.width,
                self.config.desktop.height,
                size.width,
                size.height,
            )
        if self.config.desktop.require_window_manager:
            try:
                wm = self.drivers.window.window_manager_name()
            except DriverError as exc:
                raise DesktopUnavailable(
                    f"window manager probe failed: {exc}",
                    details={"display": self.config.desktop.display},
                ) from exc
            if not wm:
                raise DesktopUnavailable(
                    "no EWMH window manager detected on the X display "
                    "(window operations would be unreliable)",
                    details={"display": self.config.desktop.display},
                )
            self._window_manager = wm
            self.logger.info("window manager detected: %s", wm)

    def screen_size(self) -> Size:
        return self.drivers.screen.size()

    def mouse_position(self) -> Point:
        return self.drivers.mouse.position()

    # -- action execution --------------------------------------------------
    def _prepare_action(
        self,
        action: str,
        params: Mapping[str, Any] | Any | None,
        strategy: str | None,
    ) -> tuple[ActionHandler, Any]:
        """Resolve one action, validate parameters and strategy overrides.

        HTTP requests and task steps share this path so they enforce exactly the
        same rules ("strategy overrides disabled", unknown provider, invalid
        params) instead of the task runner silently ignoring an override.
        """

        handler = self.actions.get(action)
        if strategy is not None:
            if not self.config.server.allow_strategy_override:
                raise InvalidArgument(
                    "strategy override is disabled by configuration",
                    details={"action": action},
                )
            if strategy not in self.strategies.providers():
                raise StrategyUnavailable(
                    f"strategy provider {strategy!r} is not installed",
                    details={"action": action, "provider": strategy},
                )
        return handler, handler.parse_params(params)

    def execute_action(
        self,
        action: str,
        params: Mapping[str, Any] | Any | None = None,
        *,
        strategy: str | None = None,
        timeout_ms: int | None = None,
        request_id: str | None = None,
        task_id: str | None = None,
        step_index: int | None = None,
        cancel: threading.Event | None = None,
    ) -> ActionResult:
        """Execute one action.

        Mutating actions are routed through the serial executor (waiting for
        their turn). Read-only actions run immediately.
        """

        handler, parsed = self._prepare_action(action, params, strategy)
        effective_timeout = timeout_ms or self.config.tasks.step_timeout_ms
        if not handler.mutating:
            return self._run_action(
                handler,
                parsed,
                strategy=strategy,
                request_id=request_id,
                task_id=task_id,
                step_index=step_index,
                cancel=cancel or threading.Event(),
            )
        job = self.executor.submit(
            f"action:{action}",
            lambda cancel_event: self._run_action(
                handler,
                parsed,
                strategy=strategy,
                request_id=request_id,
                task_id=task_id,
                step_index=step_index,
                cancel=cancel_event,
            ),
            timeout_ms=effective_timeout,
        )
        wait_s = self.config.server.sync_timeout_ms / 1000.0 + 10.0
        try:
            return job.future.result(timeout=wait_s)
        except FutureTimeoutError as exc:
            self.executor.cancel(job.id)
            raise ActionTimeout(
                f"action {action!r} did not finish within {self.config.server.sync_timeout_ms}ms",
                details={"action": action, "job_id": job.id},
            ) from exc

    def execute_action_inline(
        self,
        action: str,
        params: Mapping[str, Any] | Any | None = None,
        *,
        strategy: str | None = None,
        request_id: str | None = None,
        task_id: str | None = None,
        step_index: int | None = None,
        cancel: threading.Event | None = None,
        timeout_ms: int | None = None,
    ) -> ActionResult:
        """Execute an action without re-entering the serial queue.

        Used by the task runner, which already runs inside a serial executor job.
        """

        handler, parsed = self._prepare_action(action, params, strategy)
        return self._run_action(
            handler,
            parsed,
            strategy=strategy,
            request_id=request_id,
            task_id=task_id,
            step_index=step_index,
            cancel=cancel or threading.Event(),
        )

    def _run_action(
        self,
        handler: ActionHandler,
        parsed: Any,
        *,
        strategy: str | None,
        request_id: str | None,
        task_id: str | None,
        step_index: int | None,
        cancel: threading.Event,
    ) -> ActionResult:
        started_at = utc_now()
        started = time.monotonic()
        ctx = ActionContext(
            config=self.config,
            drivers=self.drivers,
            strategies=self.strategies,
            store=self.store,
            logger=self.logger,
            cancel=cancel,
            request_id=request_id,
            task_id=task_id,
            step_index=step_index,
            strategy_override=strategy,
            actions=self.actions,
            perception=self.perception,
        )
        try:
            output = handler.execute(parsed, ctx) or {}
        except NextAutoDeskError:
            raise
        except Exception as exc:
            self.logger.exception(
                "action %s crashed (request_id=%s task_id=%s)", handler.name, request_id, task_id
            )
            raise ActionFailed(
                f"action {handler.name!r} failed: {type(exc).__name__}: {exc}",
                details={
                    "action": handler.name,
                    "exception": type(exc).__name__,
                    "message": str(exc),
                },
            ) from exc
        finished = time.monotonic()
        result = ActionResult(
            action=handler.name,
            success=True,
            started_at=started_at,
            finished_at=utc_now(),
            duration_ms=round((finished - started) * 1000.0, 3),
            output=output,
            strategies=list(ctx.outcomes),
        )
        self.logger.debug(
            "action %s done in %.1fms (request_id=%s task_id=%s step=%s)",
            handler.name,
            result.duration_ms,
            request_id,
            task_id,
            step_index,
        )
        return result

    # -- introspection -----------------------------------------------------
    def action_catalog(self) -> list[ActionInfo]:
        return self.actions.catalog()

    def strategy_views(self) -> dict[str, OperationStrategyView]:
        return self.strategies.operation_views()

    def capabilities(self) -> dict[str, Any]:
        screen = self._screen_size or self.screen_size()
        wm = self._window_manager
        if wm is None:
            try:
                wm = self.drivers.window.window_manager_name()
            except DriverError:
                wm = None
            self._window_manager = wm
        if self.perception is None:  # pragma: no cover - always wired by the constructor
            perception_payload: dict[str, Any] = {"enabled": False, "locators": []}
        else:
            perception_payload = {
                "enabled": bool(self.perception.enabled),
                "locators": [
                    item.model_dump(mode="json")
                    for item in self.perception.locator_availability()
                ],
            }
        return {
            "name": "nextautodesk",
            "version": __version__,
            "api_version": API_VERSION,
            "display": self.config.desktop.display,
            "screen": {"width": screen.width, "height": screen.height},
            "desktop": {
                "resolution": f"{screen.width}x{screen.height}x{self.config.desktop.depth}",
                "window_manager": wm,
            },
            "actions": self.actions.names,
            "providers": [
                item.model_dump(mode="json") for item in self.strategies.provider_availability()
            ],
            "perception": perception_payload,
            "features": {
                "human_like_default": True,
                "per_operation_strategy_config": True,
                "tasks": True,
                "persistence": True,
                "authentication": self.config.auth.enabled,
                "screen_element_perception": True,
                "api_tiers": ["primitive", "perception", "composite", "workflow"],
                "vnc": "reserved",
            },
            "executor": self.executor.stats().model_dump(mode="json"),
        }

    @property
    def uptime_s(self) -> float:
        return time.monotonic() - self.started_at

    # -- configuration reload ---------------------------------------------
    def reload_config(self, new_config: AppConfig) -> AppConfig:
        """Apply a validated configuration without an API restart.

        Only *behavioural* settings can be hot-reloaded. Changing the desktop
        geometry/display or driver backend requires restarting the container.
        """

        old = self.config
        if new_config.desktop.model_dump() != old.desktop.model_dump():
            raise ReloadError(
                "desktop settings cannot be hot-reloaded; restart the container to change them",
                details={"changed": "desktop"},
            )
        if new_config.driver.model_dump() != old.driver.model_dump():
            raise ReloadError(
                "driver settings cannot be hot-reloaded; restart the container to change them",
                details={"changed": "driver"},
            )
        if new_config.storage.model_dump() != old.storage.model_dump():
            raise ReloadError(
                "storage settings cannot be hot-reloaded; restart the container to change them",
                details={"changed": "storage"},
            )
        try:
            new_registry = StrategyRegistry.build(new_config.strategies, logger=self.logger)
        except Exception as exc:
            raise ReloadError(f"invalid strategy configuration: {exc}") from exc
        new_perception = self.perception
        if new_config.perception.model_dump() != old.perception.model_dump():
            try:
                new_perception = PerceptionService(
                    new_config.perception,
                    drivers=self.drivers,
                    store=self.store,
                    logger=self.logger,
                )
            except Exception as exc:
                raise ReloadError(f"invalid perception configuration: {exc}") from exc
        with self._lock:
            self.config = new_config
            self.strategies = new_registry
            self.perception = new_perception
        self.logger.info("configuration hot-reloaded")
        return new_config


__all__ = ["DesktopService"]
