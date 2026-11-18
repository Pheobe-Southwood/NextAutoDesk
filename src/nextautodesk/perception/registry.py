"""Locator discovery, configuration and lookup.

The registry owns one locator instance per known name. Built-ins are
``template`` (OpenCV) and ``text`` (Tesseract); additional locators are loaded
from the ``nextautodesk.locators`` entry-point group. A broken third-party
entry point is logged and ignored so it can never prevent the API from starting.

The registry also owns configuration validation: unknown locator names and
invalid locator parameters are configuration errors (:class:`ConfigError`),
while request-time parameter problems are reported by the service as
``ValidationFailed``.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Iterable
from typing import Any

from ..config import LocatorSettings, PerceptionSettings
from ..errors import ConfigError, LocatorUnavailable
from .base import Locator, TextExtractor
from .models import LocatorAvailability

LOGGER = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "nextautodesk.locators"


def _builtin_locator_classes() -> tuple[type[Locator], ...]:
    from .locators import builtin_locator_classes

    return builtin_locator_classes()


def discover_locator_classes() -> list[type[Locator]]:
    """Built-in locator classes plus locators registered via entry points."""

    classes: list[type[Locator]] = list(_builtin_locator_classes())
    try:
        from importlib.metadata import entry_points

        for entry_point in entry_points(group=ENTRY_POINT_GROUP):
            try:
                loaded = entry_point.load()
            except Exception:
                LOGGER.exception(
                    "Failed to load locator entry point %s; it is ignored", entry_point.name
                )
                continue
            candidate = loaded if isinstance(loaded, type) else type(loaded)
            if isinstance(candidate, type) and issubclass(candidate, Locator):
                classes.append(candidate)
            else:
                LOGGER.warning(
                    "Ignoring locator entry point %s: %r is not a Locator subclass",
                    entry_point.name,
                    loaded,
                )
    except Exception:  # pragma: no cover - importlib.metadata itself
        LOGGER.exception("Failed to enumerate locator entry points")
    return classes


class LocatorRegistry:
    """Maps locator names to instances and reports their availability."""

    def __init__(
        self,
        settings: PerceptionSettings,
        locators: dict[str, Locator],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._locators = locators
        self._logger = logger or LOGGER
        self._availability: list[LocatorAvailability] | None = None

    # -- construction ------------------------------------------------------
    @classmethod
    def build(
        cls,
        settings: PerceptionSettings,
        *,
        logger: logging.Logger | None = None,
        locator_classes: Iterable[type[Locator]] | None = None,
    ) -> LocatorRegistry:
        log = logger or LOGGER
        classes = (
            list(locator_classes)
            if locator_classes is not None
            else discover_locator_classes()
        )
        builtin_names = {
            locator_cls.name for locator_cls in _builtin_locator_classes()
        }
        locators: dict[str, Locator] = {}
        for locator_cls in classes:
            name = getattr(locator_cls, "name", "")
            if not name:
                raise ConfigError(f"locator class {locator_cls!r} does not declare a name")
            if name in locators:
                raise ConfigError(f"duplicate locator name: {name!r}")
            try:
                locators[name] = locator_cls()
            except Exception as exc:
                # A broken first-party/configured locator must fail startup; only
                # optional third-party entry points are allowed to be skipped.
                if name in builtin_names or name in settings.locators:
                    raise ConfigError(
                        f"locator {name!r} failed to initialise: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                log.exception("Optional locator %s failed to initialise; it is ignored", name)

        unknown = sorted(set(settings.locators) - set(locators))
        if unknown:
            raise ConfigError(
                f"perception.locators references unknown locators {unknown}; "
                f"available locators: {sorted(locators)}"
            )
        for name, cfg in settings.locators.items():
            if not cfg.enabled:
                continue
            try:
                locators[name].validate_params(dict(cfg.params))
            except ConfigError:
                raise
            except Exception as exc:
                raise ConfigError(
                    f"invalid parameters for locator {name!r}: {exc}"
                ) from exc
        return cls(settings, locators, logger=log)

    # -- introspection -----------------------------------------------------
    @property
    def names(self) -> list[str]:
        return sorted(self._locators)

    def availability(self) -> list[LocatorAvailability]:
        return [item.model_copy(deep=True) for item in self._ensure_availability()]

    def _ensure_availability(self) -> list[LocatorAvailability]:
        if self._availability is None:
            self._availability = self._compute_availability()
        return self._availability

    def _compute_availability(self) -> list[LocatorAvailability]:
        result: list[LocatorAvailability] = []
        for name in sorted(self._locators):
            locator = self._locators[name]
            try:
                availability = locator.availability()
            except Exception as exc:  # pragma: no cover - defensive
                availability = LocatorAvailability(
                    name=name,
                    kind=locator.kind,
                    available=False,
                    reason=f"availability probe failed: {type(exc).__name__}: {exc}",
                    supported_queries=sorted(locator.supported_queries),
                )
            if not self._configured_enabled(name):
                availability = availability.model_copy(
                    update={"available": False, "reason": "disabled in configuration"}
                )
            result.append(availability)
        return result

    def _configured_enabled(self, name: str) -> bool:
        cfg: LocatorSettings | None = self._settings.locators.get(name)
        return True if cfg is None else cfg.enabled

    def _availability_for(self, name: str) -> LocatorAvailability | None:
        for item in self._ensure_availability():
            if item.name == name:
                return item
        return None

    # -- lookup ------------------------------------------------------------
    def get(self, name: str) -> Locator:
        """Return an enabled, available locator or raise ``LocatorUnavailable``."""

        locator = self._locators.get(name)
        if locator is None:
            raise LocatorUnavailable(
                f"unknown locator {name!r}",
                details={"locator": name, "available": self.names},
            )
        if not self._configured_enabled(name):
            raise LocatorUnavailable(
                f"locator {name!r} is disabled in configuration",
                details={"locator": name},
            )
        availability = self._availability_for(name)
        if availability is None or not availability.available:
            reason = "availability is unknown" if availability is None else availability.reason
            raise LocatorUnavailable(
                f"locator {name!r} is unavailable: {reason}",
                details={"locator": name, "reason": reason},
            )
        return locator

    def params_for(self, name: str) -> dict[str, Any]:
        """Return a deep copy of the configured parameters for ``name``."""

        cfg = self._settings.locators.get(name)
        return copy.deepcopy(dict(cfg.params)) if cfg is not None else {}

    def find_text_extractor(self) -> tuple[str, TextExtractor]:
        """Return the first enabled, available locator that can extract OCR text."""

        reasons: list[str] = []
        for name in sorted(self._locators):
            locator = self._locators[name]
            if locator.kind != "text":
                continue
            if not isinstance(locator, TextExtractor):
                reasons.append(f"{name}: does not implement the TextExtractor protocol")
                continue
            try:
                self.get(name)
            except LocatorUnavailable as exc:
                reasons.append(f"{name}: {exc.message}")
                continue
            return name, locator
        raise LocatorUnavailable(
            "no text locator is available",
            details={"reasons": reasons},
        )

    def __len__(self) -> int:
        return len(self._locators)


__all__ = ["ENTRY_POINT_GROUP", "LocatorRegistry", "discover_locator_classes"]
