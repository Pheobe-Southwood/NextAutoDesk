"""Action registry and discovery."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable

from ..errors import ConfigError, UnknownAction
from .base import ActionHandler, ActionInfo

LOGGER = logging.getLogger(__name__)

#: Module names under ``nextautodesk.actions`` scanned for ``ACTIONS`` lists.
_ACTION_MODULES: tuple[str, ...] = (
    "mouse",
    "keyboard",
    "screen",
    "window",
    "desktop",
    "elements",
    "interactions",
)


class ActionRegistry:
    """Maps action names to handlers."""

    def __init__(self, handlers: Iterable[ActionHandler], logger: logging.Logger | None = None) -> None:
        self._handlers: dict[str, ActionHandler] = {}
        self._logger = logger or LOGGER
        for handler in handlers:
            self.register(handler)

    @classmethod
    def build_default(
        cls,
        *,
        logger: logging.Logger | None = None,
        module_names: Iterable[str] | None = None,
    ) -> ActionRegistry:
        """Load built-in action modules.

        The modules shipped with the package are mandatory: a broken install must
        fail fast at startup instead of silently exposing a partial catalogue.
        """

        handlers: list[ActionHandler] = []
        for module_name in module_names or _ACTION_MODULES:
            full_name = f"{__package__ or 'nextautodesk.actions'}.{module_name}"
            try:
                module = importlib.import_module(full_name)
            except ImportError as exc:
                raise ConfigError(
                    f"built-in action module {full_name!r} failed to import: {exc}"
                ) from exc
            module_actions = getattr(module, "ACTIONS", None)
            if module_actions is None:
                LOGGER.warning("Action module %s has no ACTIONS list", full_name)
                continue
            for handler in module_actions:
                if not isinstance(handler, ActionHandler):
                    raise TypeError(f"{full_name}.ACTIONS contains a non-ActionHandler: {handler!r}")
                handlers.append(handler)
        return cls(handlers, logger=logger)

    def register(self, handler: ActionHandler) -> None:
        if handler.name in self._handlers:
            raise ValueError(f"Duplicate action handler: {handler.name!r}")
        self._handlers[handler.name] = handler

    # -- access ------------------------------------------------------------
    def get(self, name: str) -> ActionHandler:
        try:
            return self._handlers[name]
        except KeyError as exc:
            raise UnknownAction(
                f"unknown action {name!r}",
                details={"action": name, "available": sorted(self._handlers)},
            ) from exc

    def has(self, name: str) -> bool:
        return name in self._handlers

    @property
    def names(self) -> list[str]:
        return sorted(self._handlers)

    def catalog(self) -> list[ActionInfo]:
        return [self._handlers[name].info() for name in sorted(self._handlers)]

    def __len__(self) -> int:
        return len(self._handlers)


__all__ = ["ActionRegistry"]
