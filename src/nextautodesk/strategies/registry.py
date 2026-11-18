"""Provider registry: discovery, configuration merging and resolution."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from ..config import StrategiesSettings
from ..errors import StrategyUnavailable
from ..models import OperationStrategyView, ProviderAvailability
from ..operations import ALL_OPERATIONS, Operation
from .base import ResolvedStrategy, StrategyProvider

LOGGER = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "nextautodesk.strategies"


def discover_provider_classes() -> list[type[StrategyProvider]]:
    """Built-in providers plus providers registered via package entry points."""

    from .providers import builtin_provider_classes

    classes: list[type[StrategyProvider]] = list(builtin_provider_classes())
    try:
        from importlib.metadata import entry_points

        for entry_point in entry_points(group=ENTRY_POINT_GROUP):
            try:
                loaded = entry_point.load()
            except Exception:  # pragma: no cover - defensive: a broken plugin must not kill startup
                LOGGER.exception("Failed to load strategy provider entry point %s", entry_point.name)
                continue
            if isinstance(loaded, type) and issubclass(loaded, StrategyProvider):
                classes.append(loaded)
            else:
                LOGGER.warning(
                    "Ignoring entry point %s: %r is not a StrategyProvider subclass",
                    entry_point.name,
                    loaded,
                )
    except Exception:  # pragma: no cover - importlib.metadata itself
        LOGGER.exception("Failed to enumerate strategy provider entry points")
    return classes


def merge_operation_params(
    settings: StrategiesSettings,
    provider_name: str,
    operation_params: dict[str, Any],
) -> dict[str, Any]:
    """Merge provider defaults with the operation-level overrides."""

    merged = dict(settings.provider_params(provider_name))
    merged.update(operation_params)
    return merged


class StrategyRegistry:
    """Runtime registry resolving operation -> provider -> merged parameters."""

    def __init__(
        self,
        settings: StrategiesSettings,
        providers: dict[str, StrategyProvider],
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._providers = providers
        self._logger = logger or LOGGER

    # -- construction ------------------------------------------------------
    @classmethod
    def build(
        cls,
        settings: StrategiesSettings,
        *,
        logger: logging.Logger | None = None,
        provider_classes: Iterable[type[StrategyProvider]] | None = None,
    ) -> StrategyRegistry:
        classes = list(provider_classes) if provider_classes is not None else discover_provider_classes()
        providers: dict[str, StrategyProvider] = {}
        for provider_cls in classes:
            name = getattr(provider_cls, "name", "")
            if not name:
                raise ValueError(f"Strategy provider class {provider_cls!r} has no name")
            if name in providers:
                raise ValueError(f"Duplicate strategy provider name: {name!r}")
            providers[name] = provider_cls()

        # Fail fast when configuration references an unknown provider.
        unknown = {
            cfg.provider
            for cfg in settings.operations.values()
            if cfg.provider not in providers
        }
        if unknown:
            available = sorted(providers)
            raise ValueError(
                f"Configuration references unknown strategy providers {sorted(unknown)}; "
                f"available providers: {available}"
            )
        # Fail fast on invalid per-operation parameters. Without this check a typo
        # in ``strategies.operations.<op>.params`` is accepted at startup/reload
        # and only surfaces as a StrategyUnavailable error on the first request
        # using that operation (the effective params are validated lazily by
        # ``resolve``). Providers validate structurally without importing their
        # optional dependencies, so this is cheap and headless-safe.
        invalid: list[str] = []
        for operation in ALL_OPERATIONS:
            operation_cfg = settings.operation(operation)
            if not operation_cfg.enabled:
                continue
            provider = providers[operation_cfg.provider]
            merged = merge_operation_params(settings, operation_cfg.provider, operation_cfg.params)
            try:
                provider.validate_params(merged)
            except Exception as exc:  # both ValueError and plugin surprises
                invalid.append(f"{operation.value}: {exc}")
        if invalid:
            raise ValueError(
                "invalid strategy parameters in configuration: " + "; ".join(invalid)
            )
        return cls(settings, providers, logger=logger)

    # -- introspection -----------------------------------------------------
    @property
    def settings(self) -> StrategiesSettings:
        return self._settings

    def providers(self) -> dict[str, StrategyProvider]:
        return dict(self._providers)

    def provider_availability(self) -> list[ProviderAvailability]:
        result: list[ProviderAvailability] = []
        for name, provider in sorted(self._providers.items()):
            try:
                availability = provider.availability()
            except Exception as exc:  # pragma: no cover - defensive
                availability = ProviderAvailability(
                    name=name,
                    available=False,
                    reason=f"availability probe failed: {exc}",
                )
            configured = self._settings.providers.get(name)
            if configured is not None and not configured.enabled:
                availability.available = False
                availability.reason = "disabled in configuration"
            result.append(availability)
        return result

    def operation_views(self) -> dict[str, OperationStrategyView]:
        availability = {item.name: item for item in self.provider_availability()}
        views: dict[str, OperationStrategyView] = {}
        for operation in ALL_OPERATIONS:
            cfg = self._settings.operation(operation)
            candidates = [
                provider.name
                for provider in self._providers.values()
                if operation in provider.supported and availability[provider.name].available
            ]
            views[operation.value] = OperationStrategyView(
                operation=operation,
                provider=cfg.provider,
                enabled=cfg.enabled,
                available=availability.get(cfg.provider, ProviderAvailability(
                    name=cfg.provider, available=False
                )).available,
                params=self._merged_params(cfg.provider, cfg.params),
                candidates=sorted(candidates),
            )
        return views

    # -- resolution --------------------------------------------------------
    def resolve(self, operation: Operation, override: str | None = None) -> ResolvedStrategy:
        """Resolve the provider (and merged params) for ``operation``."""

        cfg = self._settings.operation(operation)
        if not cfg.enabled:
            raise StrategyUnavailable(
                f"strategy for operation {operation.value!r} is disabled in configuration",
                details={"operation": operation.value},
            )
        provider_name = override or cfg.provider
        provider = self._providers.get(provider_name)
        if provider is None:
            raise StrategyUnavailable(
                f"strategy provider {provider_name!r} is not installed",
                details={"operation": operation.value, "provider": provider_name},
            )
        configured = self._settings.providers.get(provider_name)
        if configured is not None and not configured.enabled:
            raise StrategyUnavailable(
                f"strategy provider {provider_name!r} is disabled in configuration",
                details={"operation": operation.value, "provider": provider_name},
            )
        availability = provider.availability()
        if not availability.available:
            raise StrategyUnavailable(
                f"strategy provider {provider_name!r} is unavailable: {availability.reason}",
                details={
                    "operation": operation.value,
                    "provider": provider_name,
                    "reason": availability.reason,
                },
            )
        if operation not in provider.supported:
            raise StrategyUnavailable(
                f"strategy provider {provider_name!r} does not support {operation.value!r}",
                details={
                    "operation": operation.value,
                    "provider": provider_name,
                    "supported": sorted(op.value for op in provider.supported),
                },
            )
        params = self._merged_params(provider_name, cfg.params)
        try:
            provider.validate_params(params)
        except ValueError as exc:
            raise StrategyUnavailable(
                f"invalid parameters for provider {provider_name!r}: {exc}",
                details={"operation": operation.value, "provider": provider_name},
            ) from exc
        return ResolvedStrategy(provider=provider, operation=operation, params=params)

    def _merged_params(self, provider_name: str, operation_params: dict[str, Any]) -> dict[str, Any]:
        return merge_operation_params(self._settings, provider_name, operation_params)


__all__ = [
    "ENTRY_POINT_GROUP",
    "StrategyRegistry",
    "discover_provider_classes",
    "merge_operation_params",
]
