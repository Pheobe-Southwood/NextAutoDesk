"""Screen actions: capture (optionally persisted) and screen information.

Screenshot capture is read-only with respect to the desktop, so it bypasses the
serial executor (``mutating = False``) and stays responsive even while a long
task is running. Persisting the file is delegated to the storage layer.
"""

from __future__ import annotations

import base64
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ..models import Rect, ScreenshotFormat
from .base import ActionContext, ActionHandler


class ScreenScreenshotParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region: Rect | None = Field(
        default=None, description="Optional capture region; whole screen when omitted"
    )
    format: ScreenshotFormat = Field(default="png", description="Image format: png or jpeg")
    save: bool = Field(
        default=False, description="Persist the capture in the screenshot store"
    )
    name: str | None = Field(
        default=None, description="Optional file name when save=true"
    )


def _image_size(data: bytes) -> tuple[int | None, int | None]:
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            return int(image.width), int(image.height)
    except Exception:  # pragma: no cover - Pillow is a hard dependency
        return None, None


class ScreenScreenshot(ActionHandler[ScreenScreenshotParams]):
    name: ClassVar[str] = "screen.screenshot"
    summary: ClassVar[str] = "Capture the screen or a region (optionally saved)"
    category: ClassVar[str] = "screen"
    params_model: ClassVar[type[BaseModel]] = ScreenScreenshotParams
    mutating: ClassVar[bool] = False
    example: ClassVar[dict[str, Any]] = {"save": True, "format": "png"}

    def execute(self, params: ScreenScreenshotParams, ctx: ActionContext) -> dict[str, Any]:
        ctx.check_cancelled()
        data = ctx.drivers.screen.screenshot(params.region, params.format)
        width, height = _image_size(data)
        output: dict[str, Any] = {
            "format": params.format,
            "width": width,
            "height": height,
            "size_bytes": len(data),
        }
        if params.save:
            info = ctx.store.screenshots.save(
                data,
                fmt=params.format,
                name=params.name,
                width=width,
                height=height,
            )
            output["screenshot"] = info.model_dump(mode="json")
        else:
            output["image_base64"] = base64.b64encode(data).decode("ascii")
            output["mime_type"] = (
                "image/jpeg" if params.format == "jpeg" else "image/png"
            )
        return output


class ScreenInfoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScreenInfo(ActionHandler[ScreenInfoParams]):
    name: ClassVar[str] = "screen.info"
    summary: ClassVar[str] = "Get the desktop screen resolution"
    category: ClassVar[str] = "screen"
    params_model: ClassVar[type[BaseModel]] = ScreenInfoParams
    mutating: ClassVar[bool] = False

    def execute(self, params: ScreenInfoParams, ctx: ActionContext) -> dict[str, Any]:
        del params
        size = ctx.drivers.screen.size()
        return {
            "width": int(size.width),
            "height": int(size.height),
            "display": ctx.config.desktop.display,
        }


ACTIONS: list[ActionHandler] = [ScreenScreenshot(), ScreenInfo()]

__all__ = ["ACTIONS"]
