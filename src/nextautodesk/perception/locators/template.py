"""OpenCV template-matching locator.

The locator decodes a template image (inline base64 or a saved screenshot
resolved by the service), slides it over the captured frame with
``cv2.matchTemplate`` at the configured scales and reduces the raw correlation
peaks with a small deterministic greedy NMS.

Coordinates returned by :meth:`OpenCVTemplateLocator.execute` are
frame-relative; the service adds the capture region offset.
"""

from __future__ import annotations

import base64
import binascii
from io import BytesIO
from typing import Any, ClassVar, Literal

from ...errors import PerceptionFailed, ValidationFailed
from ...models import Point, Rect
from ..base import Frame, Locator
from ..models import (
    MAX_TEMPLATE_SCALE,
    MAX_TEMPLATE_SCALES,
    MIN_TEMPLATE_SCALE,
    ElementMatch,
    ElementQuery,
    LocatorAvailability,
)

_NAME = "template"
_DEFAULT_THRESHOLD = 0.8
_DEFAULT_SCALES = (1.0,)
_DEFAULT_MAX_RESULTS = 100
_ALLOWED_PARAMS = frozenset({"threshold", "grayscale", "scales"})
_NMS_IOU_THRESHOLD = 0.3
_MAX_MATCH_CANDIDATES = 512


def _decode_image_base64(value: str) -> Any:
    """Decode a base64 or ``data:`` URL image into a Pillow RGB image."""

    from PIL import Image

    payload = value.strip()
    if payload.startswith("data:"):
        header, separator, data = payload.partition(",")
        if not separator or ";base64" not in header:
            raise ValidationFailed(
                "template.image_base64 data URL must look like 'data:image/png;base64,...'"
            )
        payload = data
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationFailed(f"invalid base64 template image: {exc}") from exc
    if not raw:
        raise ValidationFailed("decoded template image is empty")
    try:
        with Image.open(BytesIO(raw)) as image:
            return image.convert("RGB")
    except Exception as exc:
        raise ValidationFailed(
            f"cannot decode template image: {type(exc).__name__}: {exc}"
        ) from exc


def _iou(first: Rect, second: Rect) -> float:
    """Intersection-over-union of two boxes (0..1)."""

    left = max(first.x, second.x)
    top = max(first.y, second.y)
    right = min(first.x + first.width, second.x + second.width)
    bottom = min(first.y + first.height, second.y + second.height)
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    union = first.width * first.height + second.width * second.height - intersection
    return intersection / union if union else 0.0


class OpenCVTemplateLocator(Locator):
    """Locate a template image on screen with OpenCV normalized correlation."""

    name: ClassVar[str] = _NAME
    kind: ClassVar[Literal["template", "text"]] = "template"
    supported_queries: ClassVar[frozenset[str]] = frozenset({"template"})

    # -- availability ------------------------------------------------------
    def availability(self) -> LocatorAvailability:
        try:
            import cv2
        except ImportError:
            return LocatorAvailability(
                name=self.name,
                kind=self.kind,
                available=False,
                reason="OpenCV (cv2) is not installed",
                supported_queries=sorted(self.supported_queries),
            )
        except Exception as exc:  # pragma: no cover - broken native install
            return LocatorAvailability(
                name=self.name,
                kind=self.kind,
                available=False,
                reason=f"OpenCV is unavailable: {type(exc).__name__}: {exc}",
                supported_queries=sorted(self.supported_queries),
            )
        version = getattr(cv2, "__version__", None)
        return LocatorAvailability(
            name=self.name,
            kind=self.kind,
            available=True,
            version=str(version) if version else None,
            supported_queries=sorted(self.supported_queries),
        )

    # -- validation --------------------------------------------------------
    def validate_params(self, params: Any) -> None:
        unknown = sorted(set(params) - _ALLOWED_PARAMS)
        if unknown:
            raise ValueError(f"unknown template locator parameter(s): {unknown}")
        threshold = params.get("threshold")
        if threshold is not None:
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
                raise ValueError("template.threshold must be a number")
            if not 0.0 <= float(threshold) <= 1.0:
                raise ValueError("template.threshold must be within 0..1")
        grayscale = params.get("grayscale")
        if grayscale is not None and not isinstance(grayscale, bool):
            raise ValueError("template.grayscale must be a boolean")
        scales = params.get("scales")
        if scales is not None:
            if not isinstance(scales, (list, tuple)):
                raise ValueError("template.scales must be a list of numbers")
            if len(scales) > MAX_TEMPLATE_SCALES:
                raise ValueError(f"template.scales accepts at most {MAX_TEMPLATE_SCALES} entries")
            for scale in scales:
                if isinstance(scale, bool) or not isinstance(scale, (int, float)):
                    raise ValueError("template.scales entries must be numbers")
                if not MIN_TEMPLATE_SCALE <= float(scale) <= MAX_TEMPLATE_SCALE:
                    raise ValueError(
                        f"template.scales entries must be within "
                        f"{MIN_TEMPLATE_SCALE}..{MAX_TEMPLATE_SCALE}"
                    )

    # -- execution ---------------------------------------------------------
    def execute(
        self,
        query: ElementQuery,
        frame: Frame,
        params: dict[str, Any],
    ) -> list[ElementMatch]:
        cv2, np = self._import_backends()
        template_image = self._resolve_template(query, params)
        spec = query.template
        threshold = (
            spec.threshold
            if spec is not None and spec.threshold is not None
            else float(params.get("threshold", _DEFAULT_THRESHOLD))
        )
        grayscale = (
            spec.grayscale
            if spec is not None and spec.grayscale is not None
            else bool(params.get("grayscale", True))
        )
        scales_value = (
            spec.scales
            if spec is not None and spec.scales is not None
            else params.get("scales", _DEFAULT_SCALES)
        )
        scales = [float(scale) for scale in (scales_value or _DEFAULT_SCALES)]
        if not scales:
            return []
        cap = _DEFAULT_MAX_RESULTS if query.max_results is None else int(query.max_results)

        frame_array = self._array(cv2, np, frame.image, grayscale=grayscale)
        template_array = self._array(cv2, np, template_image, grayscale=grayscale)
        if template_array.size == 0 or frame_array.size == 0:
            return []
        # A constant template has no unique location; normalized correlation
        # cannot localize it, so report no matches instead of a plateau of 1.0.
        if float(np.std(template_array)) == 0.0:
            return []

        candidates: list[tuple[float, int, int, int, int, float]] = []
        for scale in scales:
            candidates.extend(
                self._match_at_scale(
                    cv2,
                    np,
                    frame_array,
                    template_array,
                    scale=scale,
                    threshold=threshold,
                )
            )
        if not candidates:
            return []
        candidates.sort(key=lambda candidate: candidate[0], reverse=True)

        matches: list[ElementMatch] = []
        kept_boxes: list[Rect] = []
        for score, x, y, width, height, scale in candidates:
            box = Rect(x=x, y=y, width=width, height=height)
            if any(_iou(box, kept) > _NMS_IOU_THRESHOLD for kept in kept_boxes):
                continue
            kept_boxes.append(box)
            matches.append(
                ElementMatch(
                    box=box,
                    center=Point(x=x + width / 2.0, y=y + height / 2.0),
                    confidence=min(max(float(score), 0.0), 1.0),
                    locator=self.name,
                    metadata={"scale": scale},
                )
            )
            if len(matches) >= cap:
                break
        matches.sort(
            key=lambda match: (match.confidence, match.box.width * match.box.height),
            reverse=True,
        )
        return matches

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _import_backends() -> tuple[Any, Any]:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise PerceptionFailed(
                "OpenCV (cv2) is not installed; template matching is unavailable",
                details={"locator": _NAME},
            ) from exc
        return cv2, np

    @staticmethod
    def _array(cv2: Any, np: Any, image: Any, *, grayscale: bool) -> Any:
        array = np.asarray(image.convert("RGB"))
        if grayscale:
            array = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
        return array

    def _resolve_template(self, query: ElementQuery, params: dict[str, Any]) -> Any:
        template_image = params.get("template_image")
        if template_image is not None:
            return template_image.convert("RGB")
        if query.template is None or query.template.image_base64 is None:
            raise ValidationFailed(
                "template matching requires an image_base64 template or a stored screenshot"
            )
        return _decode_image_base64(query.template.image_base64)

    @staticmethod
    def _match_at_scale(
        cv2: Any,
        np: Any,
        frame_array: Any,
        template_array: Any,
        *,
        scale: float,
        threshold: float,
    ) -> list[tuple[float, int, int, int, int, float]]:
        frame_height, frame_width = frame_array.shape[:2]
        template_height, template_width = template_array.shape[:2]
        width = max(1, round(template_width * scale))
        height = max(1, round(template_height * scale))
        if width > frame_width or height > frame_height:
            return []
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        resized = cv2.resize(template_array, (width, height), interpolation=interpolation)
        result = cv2.matchTemplate(frame_array, resized, cv2.TM_CCOEFF_NORMED)
        if result.size == 0:
            return []
        scores = np.asarray(result, dtype="float64")
        finite = np.isfinite(scores)
        if not finite.any():
            return []
        # Non-finite correlation values (constant image windows, broken native
        # builds) are replaced with a value below any valid threshold and skipped.
        scores = np.where(finite, scores, -1.0)
        ys, xs = np.where(scores >= threshold)
        if xs.size == 0:
            return []
        values = scores[ys, xs]
        order = np.argsort(-values, kind="stable")[:_MAX_MATCH_CANDIDATES]
        return [
            (
                float(values[index]),
                int(xs[index]),
                int(ys[index]),
                width,
                height,
                scale,
            )
            for index in order
        ]


__all__ = ["OpenCVTemplateLocator"]
