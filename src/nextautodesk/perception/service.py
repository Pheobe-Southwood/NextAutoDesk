"""Perception orchestration: screen capture, locator dispatch and persistence.

The service is the only perception component that talks to the drivers and the
storage layer:

* it captures the requested screen region once per lookup,
* resolves configuration + query overrides into locator parameters,
* runs the selected locator on the captured frame,
* translates frame-relative locator results into absolute screen coordinates,
* optionally persists the frame in the screenshot store.

Locators stay pure and transport-agnostic; the API layer only talks to this
service through the action layer.
"""

from __future__ import annotations

import logging
import threading
import time
from io import BytesIO
from typing import Any

from PIL import Image

from ..config import PerceptionSettings
from ..drivers.base import DriverBundle, DriverError
from ..errors import (
    LocatorNotSupported,
    NextAutoDeskError,
    PerceptionFailed,
    PerceptionUnavailable,
    ScreenshotNotFound,
    StorageError,
    TaskCancelled,
    ValidationFailed,
)
from ..models import Point, Rect, utc_now
from ..storage import DataStore
from .base import Frame, TextExtractor
from .models import (
    ElementMatch,
    ElementQuery,
    FrameInfo,
    LocateResult,
    LocatorAvailability,
    OcrLine,
    OcrResult,
    OcrWord,
)
from .registry import LocatorRegistry

LOGGER = logging.getLogger(__name__)

#: Query kind derived from the spec attached to an :class:`ElementQuery`.
_TEMPLATE_KIND = "template"
_TEXT_KIND = "text"


class PerceptionService:
    """Capture frames and run configured locators against them."""

    def __init__(
        self,
        settings: PerceptionSettings,
        *,
        drivers: DriverBundle,
        store: DataStore,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._drivers = drivers
        self._store = store
        self._logger = logger or LOGGER
        self._lock = threading.Lock()
        self._registry = self._build_registry(settings)

    def _build_registry(self, settings: PerceptionSettings) -> LocatorRegistry:
        return LocatorRegistry.build(settings, logger=self._logger)

    # -- introspection -----------------------------------------------------
    @property
    def settings(self) -> PerceptionSettings:
        with self._lock:
            return self._settings

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._settings.enabled

    def locator_availability(self) -> list[LocatorAvailability]:
        with self._lock:
            registry = self._registry
        return registry.availability()

    def reload(self, settings: PerceptionSettings) -> None:
        """Rebuild all locators and swap settings+registry atomically.

        The new registry is built (and therefore validated) before the swap, so a
        failed reload leaves the running configuration untouched.
        """

        registry = self._build_registry(settings)
        with self._lock:
            self._settings = settings
            self._registry = registry

    def _snapshot(self) -> tuple[PerceptionSettings, LocatorRegistry]:
        with self._lock:
            return self._settings, self._registry

    # -- locate ------------------------------------------------------------
    def locate(
        self,
        query: ElementQuery,
        *,
        cancel: threading.Event | None = None,
    ) -> LocateResult:
        settings, registry = self._snapshot()
        if not settings.enabled:
            raise PerceptionUnavailable(
                "screen perception is disabled",
                details={"setting": "perception.enabled"},
            )
        started = time.monotonic()
        query_kind = _TEMPLATE_KIND if query.template is not None else _TEXT_KIND
        name = query.locator if query.locator != "auto" else query_kind
        locator = registry.get(name)
        if query_kind not in locator.supported_queries:
            raise LocatorNotSupported(
                f"locator {name!r} does not support {query_kind!r} queries",
                details={
                    "locator": name,
                    "query": query_kind,
                    "supported": sorted(locator.supported_queries),
                },
            )
        params = registry.params_for(name)
        self._merge_query_params(name, query, params)
        self._validate_params(locator, name, params)
        # Resolve a stored template screenshot before capturing the screen so a
        # bad/oversized request never pays for an unnecessary capture.
        template_source = self._prepare_template_source(name, query)
        frame = self._capture(query.region, cancel)
        params.update(template_source)
        try:
            raw_matches = locator.execute(query, frame, params)
        except NextAutoDeskError:
            raise
        except Exception as exc:
            self._logger.exception("locator %s failed", name)
            raise PerceptionFailed(
                f"locator {name!r} failed: {type(exc).__name__}: {exc}",
                details={"locator": name},
            ) from exc
        limit = min(query.max_results or settings.max_results, settings.max_results)
        matches = [self._absolute_match(match, frame.region) for match in raw_matches[:limit]]
        return LocateResult(
            locator=name,
            matches=matches,
            frame=self._frame_info(frame, query.save_screenshot),
            elapsed_ms=round((time.monotonic() - started) * 1000.0, 3),
            attempts=1,
        )

    # -- OCR ---------------------------------------------------------------
    def ocr(
        self,
        *,
        region: Rect | None = None,
        languages: list[str] | None = None,
        psm: int | None = None,
        min_confidence: float | None = None,
        save_screenshot: bool = False,
        cancel: threading.Event | None = None,
    ) -> OcrResult:
        settings, registry = self._snapshot()
        if not settings.enabled:
            raise PerceptionUnavailable(
                "screen perception is disabled",
                details={"setting": "perception.enabled"},
            )
        started = time.monotonic()
        name, extractor = registry.find_text_extractor()
        params = registry.params_for(name)
        if languages is not None:
            params["languages"] = list(languages)
        if psm is not None:
            params["psm"] = psm
        if min_confidence is not None:
            params["min_confidence"] = min_confidence
        frame = self._capture(region, cancel)
        try:
            lines = self._extract_text(
                extractor,
                name,
                frame,
                params,
                languages=languages,
                psm=psm,
                min_confidence=min_confidence,
            )
        except NextAutoDeskError:
            raise
        except Exception as exc:
            self._logger.exception("locator %s failed during OCR", name)
            raise PerceptionFailed(
                f"locator {name!r} failed during OCR: {type(exc).__name__}: {exc}",
                details={"locator": name},
            ) from exc
        # The OCR text is a bounded preview: keep the hard result cap (lines
        # are top-to-bottom as returned by the text locator).
        limited_lines = lines[: settings.max_results]
        absolute_lines = [
            self._absolute_line(line, frame.region) for line in limited_lines
        ]
        return OcrResult(
            lines=absolute_lines,
            text="\n".join(line.text for line in absolute_lines),
            frame=self._frame_info(frame, save_screenshot),
            elapsed_ms=round((time.monotonic() - started) * 1000.0, 3),
        )

    def _extract_text(
        self,
        extractor: TextExtractor,
        name: str,
        frame: Frame,
        params: dict[str, Any],
        *,
        languages: list[str] | None,
        psm: int | None,
        min_confidence: float | None,
    ) -> list[OcrLine]:
        validate = getattr(extractor, "validate_params", None)
        if validate is not None:
            try:
                validate(params)
            except NextAutoDeskError:
                raise
            except Exception as exc:
                raise ValidationFailed(
                    f"locator {name!r} rejected the OCR parameters: {exc}",
                    details={"locator": name},
                ) from exc
        return extractor.extract_text(
            frame,
            params,
            languages=languages,
            psm=psm,
            min_confidence=min_confidence,
        )

    # -- capture -----------------------------------------------------------
    def _capture(self, region: Rect | None, cancel: threading.Event | None) -> Frame:
        self._check_cancel(cancel)
        try:
            screen = self._drivers.screen.size()
        except DriverError as exc:
            raise PerceptionFailed(
                f"screen size probe failed: {exc}",
                details={"driver": exc.driver},
            ) from exc
        capture_region = region or Rect(
            x=0, y=0, width=int(screen.width), height=int(screen.height)
        )
        try:
            png = self._drivers.screen.screenshot(capture_region, "png")
        except DriverError as exc:
            raise PerceptionFailed(
                f"screen capture failed: {exc}",
                details={"driver": exc.driver, "region": capture_region.model_dump()},
            ) from exc
        self._check_cancel(cancel)
        try:
            with Image.open(BytesIO(png)) as raw:
                image = raw.convert("RGB")
        except Exception as exc:
            raise PerceptionFailed(
                f"captured screen image could not be decoded: {type(exc).__name__}: {exc}"
            ) from exc
        self._check_cancel(cancel)
        return Frame(
            image=image,
            png=png,
            region=capture_region,
            screen=screen,
            captured_at=utc_now(),
        )

    @staticmethod
    def _check_cancel(cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise TaskCancelled("execution cancelled")

    # -- parameters --------------------------------------------------------
    def _merge_query_params(
        self,
        name: str,
        query: ElementQuery,
        params: dict[str, Any],
    ) -> None:
        if name == _TEMPLATE_KIND and query.template is not None:
            for field in ("threshold", "grayscale", "scales"):
                value = getattr(query.template, field)
                if value is not None:
                    params[field] = value
        elif name == _TEXT_KIND and query.text is not None:
            for field in ("languages", "psm", "min_confidence", "granularity"):
                value = getattr(query.text, field)
                if value is not None:
                    params[field] = value

    def _prepare_template_source(
        self, name: str, query: ElementQuery
    ) -> dict[str, Any]:
        """Resolve a stored screenshot into a Pillow image *before* capturing.

        Inline ``template.image_base64`` payloads are decoded by the locator after
        capture (they are cheap to validate and never touch host storage).
        """

        if name != _TEMPLATE_KIND or query.template is None:
            return {}
        if query.template.screenshot is None:
            return {}
        image, stored_name = self._load_saved_template(query.template.screenshot)
        return {"template_image": image, "template_name": stored_name}

    def _load_saved_template(self, name: str) -> tuple[Any, str]:
        try:
            path, info = self._store.screenshots.get(name)
            data = path.read_bytes()
        except (ScreenshotNotFound, StorageError, OSError) as exc:
            raise ValidationFailed(
                f"template screenshot {name!r} is not available: {exc}",
                details={"screenshot": name},
            ) from exc
        try:
            with Image.open(BytesIO(data)) as raw:
                return raw.convert("RGB"), info.name
        except Exception as exc:
            raise ValidationFailed(
                f"template screenshot {name!r} is not a valid image: "
                f"{type(exc).__name__}: {exc}",
                details={"screenshot": name},
            ) from exc

    def _validate_params(self, locator: Any, name: str, params: dict[str, Any]) -> None:
        try:
            locator.validate_params(params)
        except NextAutoDeskError:
            raise
        except Exception as exc:
            raise ValidationFailed(
                f"locator {name!r} rejected the query parameters: {exc}",
                details={"locator": name},
            ) from exc

    # -- results -----------------------------------------------------------
    @staticmethod
    def _shift_rect(rect: Rect, region: Rect) -> Rect:
        return Rect(
            x=rect.x + region.x,
            y=rect.y + region.y,
            width=rect.width,
            height=rect.height,
        )

    @staticmethod
    def _shift_point(point: Point, region: Rect) -> Point:
        return Point(x=point.x + region.x, y=point.y + region.y)

    def _absolute_match(self, match: ElementMatch, region: Rect) -> ElementMatch:
        return match.model_copy(
            update={
                "box": self._shift_rect(match.box, region),
                "center": self._shift_point(match.center, region),
            }
        )

    def _absolute_line(self, line: OcrLine, region: Rect) -> OcrLine:
        words = [
            OcrWord(
                text=word.text,
                box=self._shift_rect(word.box, region),
                center=self._shift_point(word.center, region),
                confidence=word.confidence,
            )
            for word in line.words
        ]
        return OcrLine(
            text=line.text,
            box=self._shift_rect(line.box, region),
            center=self._shift_point(line.center, region),
            confidence=line.confidence,
            words=words,
        )

    def _frame_info(self, frame: Frame, save_screenshot: bool) -> FrameInfo:
        screenshot = None
        if save_screenshot:
            screenshot = self._store.screenshots.save(
                frame.png,
                fmt="png",
                name=None,
                width=frame.image.width,
                height=frame.image.height,
            )
        return FrameInfo(
            region=frame.region,
            width=frame.image.width,
            height=frame.image.height,
            captured_at=frame.captured_at,
            screenshot=screenshot,
        )


__all__ = ["PerceptionService"]
