"""Action layer contracts.

An *action* is a semantic desktop operation exposed to callers (HTTP or task
steps), for example ``mouse.click`` or ``window.activate``.  Actions:

* validate their parameters with a Pydantic model,
* resolve the right strategy provider for the operation(s) they perform,
* call the drivers through the strategy layer for input operations, or directly
  for read-only desktop queries,
* return a JSON-serialisable output dictionary.

Action names are stable API identifiers. Adding a new action never changes the
existing ones.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Literal, TypeVar

from pydantic import BaseModel

from ..config import AppConfig
from ..drivers.base import DriverBundle
from ..errors import (
    ActionFailed,
    InvalidArgument,
    StrategyNotSupported,
    StrategyUnavailable,
    ValidationFailed,
)
from ..models import ActionInfo, StrategyOutcome
from ..operations import Operation
from ..storage import DataStore
from ..strategies.base import OperationRequest, StrategyContext
from ..strategies.registry import StrategyRegistry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..perception.service import PerceptionService
    from .registry import ActionRegistry

ParamsT = TypeVar("ParamsT", bound=BaseModel)


@dataclass(slots=True)
class ActionContext:
    """Everything an action needs at execution time."""

    config: AppConfig
    drivers: DriverBundle
    strategies: StrategyRegistry
    store: DataStore
    logger: logging.Logger
    cancel: threading.Event = field(default_factory=threading.Event)
    request_id: str | None = None
    task_id: str | None = None
    step_index: int | None = None
    strategy_override: str | None = None
    outcomes: list[StrategyOutcome] = field(default_factory=list)
    actions: ActionRegistry | None = None
    perception: PerceptionService | None = None

    # -- strategy helper ---------------------------------------------------
    def strategy_context(self, seed: int | None = None) -> StrategyContext:
        return StrategyContext(
            drivers=self.drivers,
            cancel=self.cancel,
            logger=self.logger,
            request_id=self.request_id,
            task_id=self.task_id,
            step_index=self.step_index,
            seed=seed,
        )

    def run_strategy(
        self,
        operation: Operation,
        request: OperationRequest,
        *,
        strategy_override: str | None = None,
    ) -> StrategyOutcome:
        """Resolve the configured provider and execute one operation."""

        override = strategy_override if strategy_override is not None else self.strategy_override
        if not self.config.server.allow_strategy_override:
            override = None
        resolved = self.strategies.resolve(operation, override=override)
        self.check_cancelled()
        ctx = self.strategy_context()
        outcome = resolved.provider.execute(request, resolved.params, ctx)
        self.outcomes.append(outcome)
        return outcome

    def invoke(
        self,
        action: str,
        params: Mapping[str, Any] | BaseModel | None = None,
    ) -> dict[str, Any]:
        """Execute another action inline and return its output mapping.

        Composite actions use this to build on existing primitives and
        perception actions instead of duplicating their logic. The invoked
        handler runs on the current thread (never re-entering the serial
        executor) and shares this context, so cancellation and logging stay
        consistent.
        """

        if self.actions is None:
            raise ActionFailed(
                f"action {action!r} cannot be invoked: the action registry is not available",
                details={"action": action},
            )
        handler = self.actions.get(action)
        parsed = handler.parse_params(params)
        return handler.execute(parsed, self) or {}

    # -- cancellation ------------------------------------------------------
    def check_cancelled(self) -> None:
        from ..errors import TaskCancelled

        if self.cancel.is_set():
            raise TaskCancelled("execution cancelled")

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        while True:
            self.check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.cancel.wait(min(remaining, 0.05))

    # -- convenience -------------------------------------------------------
    def require_supported(self, operation: Operation, *, strategy_override: str | None = None) -> None:
        """Validate that the configured/overridden provider supports ``operation``."""

        override = strategy_override if strategy_override is not None else self.strategy_override
        if not self.config.server.allow_strategy_override:
            override = None
        try:
            self.strategies.resolve(operation, override=override)
        except (StrategyNotSupported, StrategyUnavailable) as exc:
            raise InvalidArgument(str(exc)) from exc


class ActionHandler(ABC, Generic[ParamsT]):
    """Base class for all actions."""

    name: ClassVar[str]
    summary: ClassVar[str] = ""
    category: ClassVar[str] = "desktop"
    #: API tier: "primitive" (device primitives), "perception" (read-only screen
    #: understanding) or "composite" (intent-level operation built from others).
    tier: ClassVar[Literal["primitive", "perception", "composite"]] = "primitive"
    #: Actions that change mouse/keyboard/window state are executed on the serial
    #: desktop executor to guarantee they never contend with each other.
    mutating: ClassVar[bool] = True
    params_model: ClassVar[type[BaseModel]]
    example: ClassVar[dict[str, Any]] = {}

    def parse_params(self, raw: dict[str, Any] | BaseModel | None) -> ParamsT:
        raw = raw or {}
        if isinstance(raw, self.params_model):
            return raw  # type: ignore[return-value]
        if not isinstance(raw, dict):
            raise ValidationFailed(
                f"{self.name}: params must be an object",
                details={"action": self.name, "received": type(raw).__name__},
            )
        try:
            return self.params_model.model_validate(raw)  # type: ignore[return-value]
        except Exception as exc:  # pydantic ValidationError
            errors = getattr(exc, "errors", None)
            details: list[dict[str, Any]] = []
            if callable(errors):
                for item in errors():
                    details.append(
                        {
                            "loc": list(item.get("loc", ())),
                            "msg": item.get("msg", ""),
                            "type": item.get("type", ""),
                        }
                    )
            raise ValidationFailed(
                f"{self.name}: invalid parameters",
                details={"action": self.name, "errors": details} if details else {"action": self.name},
            ) from exc

    def info(self) -> ActionInfo:
        return ActionInfo(
            name=self.name,
            summary=self.summary,
            category=self.category,
            tier=self.tier,
            mutating=self.mutating,
            params_schema=self.params_model.model_json_schema(),
            example=self.example,
        )

    @abstractmethod
    def execute(self, params: ParamsT, ctx: ActionContext) -> dict[str, Any]:
        """Run the action and return a JSON-serialisable output mapping."""


__all__ = ["ActionContext", "ActionHandler"]
