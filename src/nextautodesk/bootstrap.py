"""Application bootstrap: build logging, desktop service and task manager."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .config import AppConfig, load_config
from .service import DesktopService

LOGGER = logging.getLogger(__name__)


@dataclass
class AppContext:
    """Long-lived runtime objects shared by all HTTP requests."""

    config: AppConfig
    service: DesktopService
    task_manager: Any = None
    started_at: float = field(default_factory=time.monotonic)
    extras: dict[str, Any] = field(default_factory=dict)

    def start(self) -> None:
        self.service.start()
        if self.task_manager is not None:
            recovered = self.task_manager.recover()
            self.task_manager.start()
            if recovered:
                LOGGER.warning("marked %d interrupted task(s) after restart", recovered)

    def shutdown(self) -> None:
        if self.task_manager is not None:
            self.task_manager.shutdown()
        self.service.shutdown()

    @property
    def uptime_s(self) -> float:
        return time.monotonic() - self.started_at


def build_context(config: AppConfig | None = None) -> AppContext:
    """Build a fully wired application context (logging included)."""

    from .logging_setup import setup_logging

    cfg = config or load_config()
    setup_logging(cfg)
    LOGGER.info("starting NextAutoDesk (config validated)")
    service = DesktopService.build(cfg)

    from .tasks.manager import TaskManager
    from .tasks.store import TaskStore

    store = TaskStore(cfg)
    store.initialize()
    task_manager = TaskManager(service=service, store=store, config=cfg)

    return AppContext(config=cfg, service=service, task_manager=task_manager)


__all__ = ["AppContext", "build_context"]
