"""Screen probe and capture for an X11 desktop.

Pillow's ``ImageGrab`` is the primary capture path: on Linux it grabs the X11
root window directly (XCB) and crops regions itself, without pyscreeze's
environment sniffing (``gnome-screenshot``/``scrot``/``XDG_SESSION_TYPE``).
PyAutoGUI's screenshot is kept as a fallback for unusual environments.

The driver only deals in encoded bytes so callers can persist the image or
stream it over HTTP without touching Pillow.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Any

from ..models import Rect, ScreenshotFormat, Size
from .base import DriverError
from .pyautogui_x11 import load_pyautogui

LOGGER = logging.getLogger(__name__)

_FORMATS: frozenset[str] = frozenset({"png", "jpeg"})


class X11ScreenDriver:
    """Screen size and screenshot capture (Pillow first, PyAutoGUI fallback)."""

    def __init__(self, *, jpeg_quality: int = 85) -> None:
        self.jpeg_quality = int(jpeg_quality)

    def size(self) -> Size:
        try:
            width, height = load_pyautogui().size()
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError(
                f"screen size probe failed: {type(exc).__name__}: {exc}", driver="screen"
            ) from exc
        return Size(width=int(width), height=int(height))

    def screenshot(
        self,
        region: Rect | None = None,
        fmt: ScreenshotFormat = "png",
    ) -> bytes:
        """Capture the whole screen or a region and return encoded image bytes."""

        if fmt not in _FORMATS:
            raise DriverError(
                f"invalid screenshot format {fmt!r}",
                driver="screen",
                details={"supported": sorted(_FORMATS)},
            )
        image = self._grab(region, fmt)
        try:
            buffer = BytesIO()
            if fmt == "jpeg":
                image.convert("RGB").save(
                    buffer, format="JPEG", quality=self.jpeg_quality
                )
            else:
                image.save(buffer, format="PNG")
            return buffer.getvalue()
        except Exception as exc:
            raise DriverError(
                f"screen capture encoding failed: {type(exc).__name__}: {exc}",
                driver="screen",
                details={"format": fmt},
            ) from exc

    def _grab(self, region: Rect | None, fmt: str) -> Any:
        """Return a PIL image, preferring Pillow's native ImageGrab."""

        errors: dict[str, str] = {}
        bbox = None
        if region is not None:
            left, top = int(region.x), int(region.y)
            bbox = (left, top, left + int(region.width), top + int(region.height))
        try:
            from PIL import ImageGrab

            image = ImageGrab.grab(bbox=bbox)
            if image is not None and image.width > 0 and image.height > 0:
                return image
            raise OSError("Pillow ImageGrab returned an empty image")
        except Exception as exc:
            errors["pillow_imagegrab"] = f"{type(exc).__name__}: {exc}"
            LOGGER.warning(
                "screen: Pillow ImageGrab unavailable (%s: %s); falling back to PyAutoGUI",
                type(exc).__name__,
                exc,
            )
        try:
            page = load_pyautogui()
            if region is None:
                image = page.screenshot()
            else:
                image = page.screenshot(
                    region=(
                        int(region.x),
                        int(region.y),
                        int(region.width),
                        int(region.height),
                    )
                )
            if image is None:
                raise OSError("PyAutoGUI returned no image")
            return image
        except Exception as exc:
            errors["pyautogui_screenshot"] = f"{type(exc).__name__}: {exc}"
            raise DriverError(
                f"screen capture failed: {type(exc).__name__}: {exc}",
                driver="screen",
                details={
                    "format": fmt,
                    "region": None if region is None else region.model_dump(),
                    "errors": errors,
                },
            ) from exc


__all__ = ["X11ScreenDriver"]
