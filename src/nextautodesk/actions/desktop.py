"""Desktop-level actions (timing and composition helpers)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .base import ActionContext, ActionHandler


class DesktopWaitParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ms: int = Field(ge=0, le=600_000, description="Wait duration in milliseconds")


class DesktopWait(ActionHandler[DesktopWaitParams]):
    name: ClassVar[str] = "desktop.wait"
    summary: ClassVar[str] = "Wait (cancel-aware) between composed steps"
    category: ClassVar[str] = "desktop"
    params_model: ClassVar[type[BaseModel]] = DesktopWaitParams
    # Waiting does not touch the desktop, so it runs inline and never occupies
    # the serial desktop executor.
    mutating: ClassVar[bool] = False
    example: ClassVar[dict[str, Any]] = {"ms": 500}

    def execute(self, params: DesktopWaitParams, ctx: ActionContext) -> dict[str, Any]:
        ctx.sleep(params.ms / 1000.0)
        return {"waited_ms": params.ms}


ACTIONS: list[ActionHandler] = [DesktopWait()]

__all__ = ["ACTIONS"]
