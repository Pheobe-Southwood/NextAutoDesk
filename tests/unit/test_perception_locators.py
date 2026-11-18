"""Headless unit tests for the perception locators.

Synthetic frames are built with Pillow only: no X server, no network. OCR tests
are skipped when Tesseract (with English data) or a DejaVu font is unavailable;
they must pass inside the project container and in CI.
"""

from __future__ import annotations

import base64
import importlib.util
import shutil
import sys
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageFont

from nextautodesk.errors import ValidationFailed
from nextautodesk.models import Rect, Size
from nextautodesk.perception.base import Frame
from nextautodesk.perception.locators import text as text_locator_module
from nextautodesk.perception.locators.template import OpenCVTemplateLocator
from nextautodesk.perception.locators.text import TesseractTextLocator
from nextautodesk.perception.models import ElementQuery, TemplateSpec, TextSpec

_HAS_CV2 = importlib.util.find_spec("cv2") is not None
requires_cv2 = pytest.mark.skipif(not _HAS_CV2, reason="OpenCV (cv2) is not installed")

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/local/share/fonts/DejaVuSans.ttf",
)


def _font_path() -> str | None:
    return next((path for path in _FONT_CANDIDATES if Path(path).is_file()), None)


def _tesseract_ready() -> bool:
    if shutil.which("tesseract") is None:
        return False
    if importlib.util.find_spec("pytesseract") is None:
        return False
    try:
        import pytesseract

        return "eng" in pytesseract.get_languages(config="")
    except Exception:  # pragma: no cover - environment probe
        return False


_FONT = _font_path()
_HAS_OCR = _tesseract_ready()
requires_ocr = pytest.mark.skipif(
    not _HAS_OCR or _FONT is None,
    reason="Tesseract with English data and a DejaVu font are required",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_frame(image: Image.Image, *, region: Rect | None = None) -> Frame:
    """Build a perception ``Frame`` from a Pillow image."""

    image = image.convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return Frame(
        image=image,
        png=buffer.getvalue(),
        region=region or Rect(x=0, y=0, width=image.width, height=image.height),
        screen=Size(width=image.width, height=image.height),
        captured_at=datetime.now(UTC),
    )


def pattern_image(size: tuple[int, int] = (40, 24)) -> Image.Image:
    """A high-contrast asymmetric pattern (non-constant, robust against resizing)."""

    width, height = size
    image = Image.new("RGB", size, (24, 32, 40))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width // 3, height // 2], fill=(230, 60, 40))
    draw.rectangle([width // 2, height // 3, width - 1, height - 1], fill=(30, 220, 120))
    draw.line([(0, height - 1), (width - 1, 0)], fill=(250, 250, 30), width=2)
    return image


def template_base64(image: Image.Image) -> str:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def scene(*patterns: tuple[Image.Image, tuple[int, int]], size: tuple[int, int] = (200, 140)) -> Image.Image:
    image = Image.new("RGB", size, (12, 12, 12))
    for pattern, position in patterns:
        image.paste(pattern, position)
    return image


def find_template(
    locator: OpenCVTemplateLocator,
    frame: Frame,
    template: Image.Image,
    *,
    threshold: float = 0.9,
    scales: list[float] | None = None,
    extra_params: dict[str, Any] | None = None,
) -> list[Any]:
    query = ElementQuery(
        locator="template",
        template=TemplateSpec(
            image_base64=template_base64(template),
            threshold=threshold,
            scales=scales or [1.0],
        ),
    )
    params: dict[str, Any] = {"threshold": threshold, "grayscale": True, "scales": [1.0]}
    params.update(extra_params or {})
    return locator.execute(query, frame, params)


def render_text(
    text: str = "HELLO 123",
    *,
    size: tuple[int, int] = (360, 120),
    font_size: int = 48,
) -> Image.Image:
    assert _FONT is not None
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(_FONT, font_size)
    draw.text((20, 24), text, fill=(0, 0, 0), font=font)
    return image


def text_query(
    text: str,
    *,
    match: str = "contains",
    granularity: str | None = None,
    case_sensitive: bool = False,
    languages: list[str] | None = None,
) -> ElementQuery:
    return ElementQuery(
        locator="text",
        text=TextSpec(
            text=text,
            match=match,  # type: ignore[arg-type]
            case_sensitive=case_sensitive,
            languages=languages or ["eng"],
            psm=6,
            min_confidence=0,
            granularity=granularity,  # type: ignore[arg-type]
        ),
    )


# ---------------------------------------------------------------------------
# template locator
# ---------------------------------------------------------------------------
@requires_cv2
def test_template_locator_finds_pasted_pattern() -> None:
    template = pattern_image()
    frame = make_frame(scene((template, (30, 20))))
    matches = find_template(OpenCVTemplateLocator(), frame, template, threshold=0.95)

    assert len(matches) == 1
    match = matches[0]
    assert match.locator == "template"
    assert match.box.x == 30 and match.box.y == 20
    assert (match.box.width, match.box.height) == template.size
    assert match.center.x == pytest.approx(30 + template.width / 2)
    assert match.center.y == pytest.approx(20 + template.height / 2)
    assert match.confidence >= 0.95
    assert match.metadata["scale"] == 1.0


@requires_cv2
def test_template_locator_multiscale_finds_scaled_copy() -> None:
    template = pattern_image((20, 12))
    scaled = template.resize((40, 24), Image.NEAREST)
    frame = make_frame(scene((scaled, (60, 30))))
    # cv2 resizes with INTER_LINEAR, so a NEAREST-upscaled ground-truth paste is
    # not bit-exact at scale 2.0; a high-but-not-perfect threshold is expected.
    matches = find_template(
        OpenCVTemplateLocator(),
        frame,
        template,
        threshold=0.9,
        scales=[1.0, 2.0],
    )

    scaled_matches = [match for match in matches if match.metadata["scale"] == 2.0]
    assert len(scaled_matches) == 1
    assert (scaled_matches[0].box.width, scaled_matches[0].box.height) == (40, 24)
    assert (scaled_matches[0].box.x, scaled_matches[0].box.y) == (60, 30)


@requires_cv2
def test_template_locator_suppresses_overlapping_duplicates() -> None:
    template = pattern_image()
    frame = make_frame(scene((template, (50, 40)), (template, (52, 42))))
    matches = find_template(OpenCVTemplateLocator(), frame, template, threshold=0.8)

    assert len(matches) == 1


@requires_cv2
def test_template_locator_reports_no_match() -> None:
    template = pattern_image()
    other = Image.new("RGB", (200, 140), (200, 200, 200))
    draw = ImageDraw.Draw(other)
    draw.ellipse([20, 20, 120, 100], fill=(0, 0, 0))
    matches = find_template(OpenCVTemplateLocator(), make_frame(other), template)
    assert matches == []


@requires_cv2
def test_template_locator_handles_template_larger_than_frame() -> None:
    template = pattern_image((300, 200))
    frame = make_frame(Image.new("RGB", (80, 60), (0, 0, 0)))
    matches = find_template(OpenCVTemplateLocator(), frame, template)
    assert matches == []


@requires_cv2
def test_template_locator_constant_template_does_not_crash() -> None:
    template = Image.new("RGB", (20, 10), (10, 200, 30))
    frame = make_frame(scene((pattern_image(), (10, 10))))
    matches = find_template(OpenCVTemplateLocator(), frame, template)
    assert matches == []


@requires_cv2
def test_template_locator_accepts_data_url_and_saved_template_image() -> None:
    template = pattern_image()
    source = scene((template, (15, 25)))
    frame = make_frame(source)
    locator = OpenCVTemplateLocator()

    data_url = "data:image/png;base64," + template_base64(template)
    query = ElementQuery(
        locator="template",
        template=TemplateSpec(image_base64=data_url, threshold=0.99, scales=[1.0]),
    )
    matches = locator.execute(query, frame, {"threshold": 0.99, "scales": [1.0]})
    assert len(matches) == 1

    # A stored screenshot is resolved by the service and passed as a Pillow image.
    screenshot_query = ElementQuery(
        locator="template",
        template=TemplateSpec(screenshot="login.png", threshold=0.99),
    )
    matches = locator.execute(
        screenshot_query,
        frame,
        {
            "threshold": 0.99,
            "grayscale": True,
            "scales": [1.0],
            "template_image": template,
            "template_name": "login.png",
        },
    )
    assert len(matches) == 1


def test_template_locator_validate_params() -> None:
    locator = OpenCVTemplateLocator()
    locator.validate_params({})
    locator.validate_params({"threshold": 0.5, "grayscale": False, "scales": [0.5, 2.0]})
    for bad in (
        {"threshold": 1.5},
        {"threshold": -0.1},
        {"threshold": "high"},
        {"grayscale": "yes"},
        {"scales": [0.0]},
        {"scales": [5.0]},
        {"scales": [1.0] * 9},
        {"scales": "1.0"},
    ):
        with pytest.raises(ValueError):
            locator.validate_params(bad)


def test_template_locator_availability_without_cv2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "cv2", None)
    availability = OpenCVTemplateLocator().availability()
    assert availability.available is False
    assert availability.reason is not None and "cv2" in availability.reason
    assert availability.supported_queries == ["template"]


@requires_cv2
def test_template_locator_availability_with_cv2() -> None:
    availability = OpenCVTemplateLocator().availability()
    assert availability.available is True
    assert availability.kind == "template"
    assert availability.version
    assert availability.supported_queries == ["template"]


# ---------------------------------------------------------------------------
# text locator
# ---------------------------------------------------------------------------
@requires_ocr
def test_text_locator_extracts_lines_and_words() -> None:
    locator = TesseractTextLocator()
    frame = make_frame(render_text())
    lines = locator.extract_text(frame, {"languages": ["eng"], "psm": 6, "min_confidence": 0})

    assert lines
    text = "\n".join(line.text for line in lines)
    assert "HELLO" in text
    line = next(item for item in lines if "HELLO" in item.text)
    assert line.words
    assert line.box.width > 0 and line.box.height > 0
    assert line.center.x == pytest.approx(line.box.x + line.box.width / 2)
    assert line.confidence is not None and line.confidence > 0
    assert all(word.confidence is None or word.confidence >= 0 for word in line.words)


@requires_ocr
def test_text_locator_contains_exact_and_regex_matches() -> None:
    locator = TesseractTextLocator()
    frame = make_frame(render_text())

    contains = locator.execute(
        text_query("HELLO"), frame, {"languages": ["eng"], "psm": 6, "min_confidence": 0}
    )
    assert contains
    assert contains[0].locator == "text"
    assert "HELLO" in (contains[0].text or "")
    assert 0 <= contains[0].confidence <= 1
    assert contains[0].metadata["granularity"] == "line"

    exact = locator.execute(
        text_query("HELLO", match="exact", granularity="word"),
        frame,
        {"languages": ["eng"], "psm": 6, "min_confidence": 0},
    )
    assert exact
    assert exact[0].text == "HELLO"

    digits = locator.execute(
        text_query(r"^\d{3}$", match="regex", granularity="word"),
        frame,
        {"languages": ["eng"], "psm": 6, "min_confidence": 0},
    )
    assert digits
    assert digits[0].text == "123"


@requires_ocr
def test_text_locator_case_sensitivity() -> None:
    locator = TesseractTextLocator()
    frame = make_frame(render_text())
    params = {"languages": ["eng"], "psm": 6, "min_confidence": 0}

    insensitive = locator.execute(
        text_query("hello", match="exact", granularity="word", case_sensitive=False),
        frame,
        params,
    )
    assert insensitive

    sensitive = locator.execute(
        text_query("hello", match="exact", granularity="word", case_sensitive=True),
        frame,
        params,
    )
    assert sensitive == []


@requires_ocr
def test_text_locator_word_granularity_returns_word_boxes() -> None:
    locator = TesseractTextLocator()
    frame = make_frame(render_text())
    params = {"languages": ["eng"], "psm": 6, "min_confidence": 0}

    line_matches = locator.execute(
        text_query("HELLO", granularity="line"), frame, params
    )
    word_matches = locator.execute(
        text_query("HELLO", granularity="word"), frame, params
    )

    assert line_matches and word_matches
    assert word_matches[0].metadata["granularity"] == "word"
    # A line box covers all words on the line, so it is at least as wide as
    # one word box even if Tesseract happens to split the line.
    assert word_matches[0].box.width <= line_matches[0].box.width


@requires_ocr
def test_text_locator_invalid_regex_is_rejected() -> None:
    locator = TesseractTextLocator()
    query = text_query("[unclosed", match="regex")
    with pytest.raises(ValidationFailed) as excinfo:
        locator.execute(query, make_frame(render_text()), {"languages": ["eng"], "psm": 6})
    assert "regular expression" in str(excinfo.value)


@requires_ocr
def test_text_locator_respects_min_confidence() -> None:
    locator = TesseractTextLocator()
    frame = make_frame(render_text())
    lines = locator.extract_text(
        frame, {"languages": ["eng"], "psm": 6, "min_confidence": 95}
    )
    confidences = [
        word.confidence for line in lines for word in line.words if word.confidence is not None
    ]
    assert all(confidence >= 95 for confidence in confidences)


def test_text_locator_validate_params() -> None:
    locator = TesseractTextLocator()
    locator.validate_params({})
    locator.validate_params(
        {"languages": ["eng", "chi_sim"], "psm": 6, "min_confidence": 40, "granularity": "line"}
    )
    for bad in (
        {"languages": []},
        {"languages": ["EN"]},
        {"psm": 14},
        {"psm": -1},
        {"min_confidence": 101},
        {"granularity": "paragraph"},
    ):
        with pytest.raises(ValueError):
            locator.validate_params(bad)


def test_text_locator_availability_without_pytesseract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    availability = TesseractTextLocator().availability()
    assert availability.available is False
    assert availability.reason is not None and "pytesseract" in availability.reason


def test_text_locator_availability_without_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    if importlib.util.find_spec("pytesseract") is None:  # pragma: no cover - dev tree
        pytest.skip("pytesseract is not installed")
    monkeypatch.setattr(text_locator_module.shutil, "which", lambda _name: None)
    availability = TesseractTextLocator().availability()
    assert availability.available is False
    assert availability.reason is not None and "tesseract" in availability.reason.lower()


@requires_ocr
def test_text_locator_availability_with_engine() -> None:
    availability = TesseractTextLocator().availability()
    assert availability.available is True
    assert availability.kind == "text"
    assert availability.version
    assert availability.supported_queries == ["text"]
