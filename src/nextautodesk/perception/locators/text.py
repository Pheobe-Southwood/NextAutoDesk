"""Tesseract OCR locator and text extractor.

The locator runs Tesseract through ``pytesseract`` on the frame handed in by the
service, groups the returned words into lines and matches the query text against
line or word candidates. All boxes are frame-relative; the service translates
them to absolute screen coordinates.

Both backends are imported lazily so the API can start (and report
``available=false``) on hosts without Tesseract installed.
"""

from __future__ import annotations

import re
import shutil
from typing import Any, ClassVar, Literal

from ...errors import PerceptionFailed, ValidationFailed
from ...models import Point, Rect
from ..base import Frame, Locator
from ..models import (
    ElementMatch,
    ElementQuery,
    LocatorAvailability,
    OcrLine,
    OcrWord,
    TextSpec,
)

_NAME = "text"
_DEFAULT_LANGUAGES = ("eng",)
_DEFAULT_PSM = 6
_DEFAULT_MIN_CONFIDENCE = 0.0
_DEFAULT_GRANULARITY = "line"
_DEFAULT_MAX_RESULTS = 100
_OCR_CONFIDENCE_SCALE = 100.0
_ALLOWED_PARAMS = frozenset(
    {"languages", "psm", "min_confidence", "granularity"}
)


def _center(box: Rect) -> Point:
    return Point(x=box.x + box.width / 2.0, y=box.y + box.height / 2.0)


def _bounding_box(words: list[OcrWord]) -> Rect:
    left = min(word.box.x for word in words)
    top = min(word.box.y for word in words)
    right = max(word.box.x + word.box.width for word in words)
    bottom = max(word.box.y + word.box.height for word in words)
    return Rect(x=left, y=top, width=right - left, height=bottom - top)


def _group_lines(data: dict[str, Any], min_confidence: float) -> list[OcrLine]:
    """Group ``image_to_data`` words into lines with bounding boxes."""

    texts = data.get("text") or []
    grouped: dict[tuple[int, int, int], list[OcrWord]] = {}
    for index, raw_text in enumerate(texts):
        text = (raw_text or "").strip()
        if not text:
            continue
        try:
            confidence = float(data["conf"][index])
            left = int(data["left"][index])
            top = int(data["top"][index])
            width = int(data["width"][index])
            height = int(data["height"][index])
            key = (
                int(data["block_num"][index]),
                int(data["par_num"][index]),
                int(data["line_num"][index]),
            )
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if confidence < 0 or confidence < min_confidence:
            continue
        if width <= 0 or height <= 0:
            continue
        box = Rect(x=left, y=top, width=width, height=height)
        grouped.setdefault(key, []).append(
            OcrWord(
                text=text,
                box=box,
                center=_center(box),
                confidence=min(confidence, _OCR_CONFIDENCE_SCALE),
            )
        )

    lines: list[OcrLine] = []
    for words in grouped.values():
        if not words:
            continue
        confidences = [word.confidence for word in words if word.confidence is not None]
        box = _bounding_box(words)
        lines.append(
            OcrLine(
                text=" ".join(word.text for word in words),
                box=box,
                center=_center(box),
                confidence=sum(confidences) / len(confidences) if confidences else None,
                words=words,
            )
        )
    return lines


class TesseractTextLocator(Locator):
    """Locate text on screen and extract OCR lines with Tesseract."""

    name: ClassVar[str] = _NAME
    kind: ClassVar[Literal["template", "text"]] = "text"
    supported_queries: ClassVar[frozenset[str]] = frozenset({"text"})

    # -- availability ------------------------------------------------------
    def availability(self) -> LocatorAvailability:
        try:
            import pytesseract
        except ImportError:
            return self._unavailable("pytesseract is not installed")
        if shutil.which("tesseract") is None:
            return self._unavailable(
                "Tesseract OCR binary ('tesseract') was not found on PATH"
            )
        version: str | None = None
        try:
            version = str(pytesseract.get_tesseract_version())
        except Exception:  # pragma: no cover - version probe is best effort
            version = None
        return LocatorAvailability(
            name=self.name,
            kind=self.kind,
            available=True,
            version=version,
            supported_queries=sorted(self.supported_queries),
        )

    def _unavailable(self, reason: str) -> LocatorAvailability:
        return LocatorAvailability(
            name=self.name,
            kind=self.kind,
            available=False,
            reason=reason,
            supported_queries=sorted(self.supported_queries),
        )

    # -- validation --------------------------------------------------------
    def validate_params(self, params: dict[str, Any]) -> None:
        unknown = sorted(set(params) - _ALLOWED_PARAMS)
        if unknown:
            raise ValueError(f"unknown text locator parameter(s): {unknown}")
        languages = params.get("languages")
        if languages is not None:
            if not isinstance(languages, (list, tuple)) or not languages:
                raise ValueError("text.languages must be a non-empty list of Tesseract languages")
            for language in languages:
                if not isinstance(language, str) or not re.fullmatch(r"[a-z_]+", language):
                    raise ValueError(
                        "text.languages entries must match [a-z_]+ (e.g. 'eng', 'chi_sim')"
                    )
        psm = params.get("psm")
        if psm is not None and (
            isinstance(psm, bool) or not isinstance(psm, int) or not 0 <= psm <= 13
        ):
            raise ValueError("text.psm must be an integer within 0..13")
        min_confidence = params.get("min_confidence")
        if min_confidence is not None:
            if isinstance(min_confidence, bool) or not isinstance(min_confidence, (int, float)):
                raise ValueError("text.min_confidence must be a number")
            if not 0.0 <= float(min_confidence) <= 100.0:
                raise ValueError("text.min_confidence must be within 0..100")
        granularity = params.get("granularity")
        if granularity is not None and granularity not in {"line", "word"}:
            raise ValueError("text.granularity must be 'line' or 'word'")

    # -- OCR ---------------------------------------------------------------
    def extract_text(
        self,
        frame: Frame,
        params: dict[str, Any],
        *,
        languages: list[str] | None = None,
        psm: int | None = None,
        min_confidence: float | None = None,
    ) -> list[OcrLine]:
        pytesseract = self._import_pytesseract()
        effective_languages = list(languages or params.get("languages") or _DEFAULT_LANGUAGES)
        effective_psm = psm if psm is not None else params.get("psm", _DEFAULT_PSM)
        effective_min = (
            min_confidence
            if min_confidence is not None
            else params.get("min_confidence", _DEFAULT_MIN_CONFIDENCE)
        )
        image = frame.image
        if image.mode != "RGB":
            image = image.convert("RGB")
        # ``image_to_data`` needs a fully loaded image, never a lazy file handle.
        image.load()
        config_parts: list[str] = []
        if effective_psm is not None:
            config_parts.append(f"--psm {int(effective_psm)}")
        try:
            data = pytesseract.image_to_data(
                image,
                lang="+".join(effective_languages),
                config=" ".join(config_parts),
                output_type=pytesseract.Output.DICT,
            )
        except Exception as exc:
            raise PerceptionFailed(
                f"Tesseract OCR failed: {type(exc).__name__}: {exc}",
                details={"languages": effective_languages, "psm": effective_psm},
            ) from exc
        return _group_lines(data, float(effective_min or 0.0))

    @staticmethod
    def _import_pytesseract() -> Any:
        try:
            import pytesseract
        except ImportError as exc:
            raise PerceptionFailed(
                "pytesseract is not installed; OCR is unavailable", details={"locator": _NAME}
            ) from exc
        return pytesseract

    # -- execution ---------------------------------------------------------
    def execute(
        self,
        query: ElementQuery,
        frame: Frame,
        params: dict[str, Any],
    ) -> list[ElementMatch]:
        spec = query.text
        if spec is None:
            raise ValidationFailed("text matching requires a text spec")
        lines = self.extract_text(
            frame,
            params,
            languages=spec.languages,
            psm=spec.psm,
            min_confidence=spec.min_confidence,
        )
        granularity = spec.granularity or params.get("granularity") or _DEFAULT_GRANULARITY
        candidates: list[tuple[str, Rect, float | None]] = []
        if granularity == "word":
            for line in lines:
                candidates.extend(
                    (word.text, word.box, word.confidence) for word in line.words
                )
        else:
            candidates.extend((line.text, line.box, line.confidence) for line in lines)

        pattern = self._pattern(spec)
        matches: list[ElementMatch] = []
        for candidate_text, box, confidence in candidates:
            if not pattern(candidate_text):
                continue
            raw_confidence = confidence if confidence is not None else 0.0
            matches.append(
                ElementMatch(
                    box=box,
                    center=_center(box),
                    confidence=min(max(raw_confidence / _OCR_CONFIDENCE_SCALE, 0.0), 1.0),
                    locator=self.name,
                    text=candidate_text,
                    metadata={
                        "granularity": granularity,
                        "ocr_confidence": confidence,
                    },
                )
            )
        matches.sort(key=lambda match: (match.confidence, -match.box.y), reverse=True)
        cap = _DEFAULT_MAX_RESULTS if query.max_results is None else int(query.max_results)
        return matches[:cap]

    @staticmethod
    def _pattern(spec: TextSpec) -> Any:
        """Build the predicate used to match OCR candidate texts."""

        needle = spec.text
        if spec.match == "regex":
            flags = 0 if spec.case_sensitive else re.IGNORECASE
            try:
                compiled = re.compile(needle, flags)
            except re.error as exc:
                raise ValidationFailed(
                    f"invalid regular expression: {exc}", details={"pattern": needle}
                ) from exc
            return lambda candidate: compiled.search(candidate) is not None
        if spec.match == "exact":
            if spec.case_sensitive:
                return lambda candidate: candidate == needle
            folded = needle.casefold()
            return lambda candidate: candidate.casefold() == folded
        if spec.case_sensitive:
            return lambda candidate: needle in candidate
        folded = needle.casefold()
        return lambda candidate: folded in candidate.casefold()


__all__ = ["TesseractTextLocator"]
