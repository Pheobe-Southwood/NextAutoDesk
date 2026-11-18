"""Strategy layer contracts and provider registry.

The strategy layer is deliberately *independent* from the driver layer: a
provider produces and paces input through the :class:`DriverBundle` (via its own
adapter/backend); the rest of the system only sees ``StrategyOutcome`` metadata.

Every operation has its own configuration entry (see ``strategies.operations``
in the configuration file). The registry merges provider defaults with
per-operation overrides and resolves request-time provider overrides.

New providers can be added without changing any existing operation:

1. Implement :class:`StrategyProvider` in ``nextautodesk/strategies/providers/``.
2. Export it from ``nextautodesk/strategies/providers/__init__.py`` (built-ins)
   or publish a package exposing the ``nextautodesk.strategies`` entry-point
   group.
3. Reference it from configuration only.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..drivers.base import DriverBundle
from ..errors import StrategyNotSupported, StrategyUnavailable, TaskCancelled
from ..models import MouseButton, ProviderAvailability, StrategyOutcome
from ..operations import Operation


@dataclass(frozen=True, slots=True)
class OperationRequest:
    """Canonical, strategy-agnostic description of one operation invocation.

    Action handlers translate HTTP/task parameters into this object; providers
    interpret it according to ``operation``.
    """

    operation: Operation
    # mouse
    x: float | None = None
    y: float | None = None
    dx: float | None = None
    dy: float | None = None
    button: MouseButton | None = None
    clicks: int = 1
    duration_ms: float | None = None
    hold_ms: float | None = None
    # keyboard
    key: str | None = None
    keys: tuple[str, ...] = ()
    text: str | None = None

    def require(self, *names: str) -> None:
        missing = [name for name in names if getattr(self, name) is None]
        if missing:
            raise ValueError(f"operation {self.operation.value} requires fields: {missing}")


@dataclass(slots=True)
class StrategyContext:
    """Runtime context handed to a provider's ``execute`` method."""

    drivers: DriverBundle
    cancel: threading.Event
    logger: logging.Logger
    request_id: str | None = None
    task_id: str | None = None
    step_index: int | None = None
    seed: int | None = None
    started_monotonic: float = field(default_factory=time.monotonic)

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise TaskCancelled("execution cancelled")

    def sleep(self, seconds: float) -> None:
        """Cancel-aware sleep used for holds / inter-key pauses."""

        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        while True:
            self.check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.cancel.wait(min(remaining, 0.05))


class StrategyProvider(ABC):
    """A behaviour policy implementation.

    Providers must be stateless with respect to a single call (a fresh execution
    context is created for every invocation), so the same instance can be reused
    from the serial executor thread.
    """

    name: ClassVar[str]
    version: ClassVar[str] = "0.0.0"
    description: ClassVar[str] = ""
    #: Operations this provider can execute. Operations not listed here are
    #: rejected before execution.
    supported: ClassVar[frozenset[Operation]] = frozenset()

    def availability(self) -> ProviderAvailability:
        """Report whether the provider can run in this environment."""

        return ProviderAvailability(
            name=self.name,
            available=True,
            version=self.version,
            description=self.description,
            supported_operations=sorted(self.supported, key=lambda op: op.value),
        )

    def validate_params(self, params: Mapping[str, Any]) -> None:
        """Hook for provider-specific parameter validation (raise ``ValueError``)."""

    @abstractmethod
    def execute(
        self,
        request: OperationRequest,
        params: Mapping[str, Any],
        ctx: StrategyContext,
    ) -> StrategyOutcome:
        """Execute one operation and return metadata about what happened."""

    # -- helpers shared by providers --------------------------------------
    def _ensure_supported(self, operation: Operation) -> None:
        if operation not in self.supported:
            raise StrategyNotSupported(
                f"strategy provider {self.name!r} does not support operation {operation.value!r}",
                details={
                    "provider": self.name,
                    "operation": operation.value,
                    "supported": sorted(op.value for op in self.supported),
                },
            )

    def _outcome(
        self,
        request: OperationRequest,
        *,
        started: float,
        engine: str | None = None,
        fallback_reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> StrategyOutcome:
        return StrategyOutcome(
            operation=request.operation,
            provider=self.name,
            provider_version=self.version,
            duration_ms=(time.monotonic() - started) * 1000.0,
            engine=engine,
            fallback_reason=fallback_reason,
            details=details or {},
        )


@dataclass(frozen=True, slots=True)
class ResolvedStrategy:
    """A provider bound to merged parameters for one operation."""

    provider: StrategyProvider
    operation: Operation
    params: dict[str, Any]


__all__ = [
    "OperationRequest",
    "ResolvedStrategy",
    "StrategyContext",
    "StrategyProvider",
    "StrategyUnavailable",
]
