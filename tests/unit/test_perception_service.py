"""Headless unit tests for :class:`PerceptionService` and the locator registry.

Real drivers, storage and the service are used together with fake locators and
fake screen drivers, so these tests need neither an X server nor Tesseract.
"""

from __future__ import annotations

import base64
import threading
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from test_engine_fakes import make_bundle

from nextautodesk.config import (
    AppConfig,
    LocatorSettings,
    PerceptionSettings,
    load_config,
)
from nextautodesk.drivers.base import DriverError
from nextautodesk.errors import (
    ConfigError,
    LocatorUnavailable,
    PerceptionFailed,
    PerceptionUnavailable,
    TaskCancelled,
    ValidationFailed,
)
from nextautodesk.models import Point, Rect
from nextautodesk.perception.base import Frame, Locator
from nextautodesk.perception.locators.template import OpenCVTemplateLocator
from nextautodesk.perception.models import (
    ElementMatch,
    ElementQuery,
    LocatorAvailability,
    OcrLine,
    OcrWord,
    TemplateSpec,
)
from nextautodesk.perception.registry import LocatorRegistry
from nextautodesk.perception.service import PerceptionService
from nextautodesk.storage import DataStore

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
SMALL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeTemplateLocator(Locator):
    name = "template"
    kind = "template"
    supported_queries = frozenset({"template"})

    def __init__(self, matches: list[ElementMatch] | None = None) -> None:
        self.matches = matches or []
        self.executions: list[dict[str, Any]] = []
        self.validated: list[dict[str, Any]] = []

    def availability(self) -> LocatorAvailability:
        return LocatorAvailability(
            name=self.name,
            kind=self.kind,
            available=True,
            supported_queries=sorted(self.supported_queries),
        )

    def validate_params(self, params: Any) -> None:
        self.validated.append(dict(params))

    def execute(
        self, query: ElementQuery, frame: Frame, params: dict[str, Any]
    ) -> list[ElementMatch]:
        self.executions.append({"query": query, "frame": frame, "params": dict(params)})
        return list(self.matches)


class FakeTextLocator(Locator):
    name = "text"
    kind = "text"
    supported_queries = frozenset({"text"})

    def __init__(self, lines: list[OcrLine] | None = None) -> None:
        self.lines = lines or []
        self.executions: list[dict[str, Any]] = []

    def availability(self) -> LocatorAvailability:
        return LocatorAvailability(
            name=self.name,
            kind=self.kind,
            available=True,
            supported_queries=sorted(self.supported_queries),
        )

    def validate_params(self, params: Any) -> None:
        return None

    def execute(self, query: ElementQuery, frame: Frame, params: dict[str, Any]) -> list[ElementMatch]:
        return []

    def extract_text(
        self,
        frame: Frame,
        params: dict[str, Any],
        *,
        languages: list[str] | None = None,
        psm: int | None = None,
        min_confidence: float | None = None,
    ) -> list[OcrLine]:
        self.executions.append(
            {
                "frame": frame,
                "params": dict(params),
                "languages": languages,
                "psm": psm,
                "min_confidence": min_confidence,
            }
        )
        return list(self.lines)


class BrokenTemplateLocator(FakeTemplateLocator):
    """A first-party locator whose construction fails."""

    def __init__(self) -> None:
        raise RuntimeError("broken native dependency")


class BrokenPluginLocator(Locator):
    name = "custom"
    kind = "template"
    supported_queries = frozenset({"template"})

    def __init__(self) -> None:
        raise RuntimeError("broken third-party locator")

    def execute(
        self, query: ElementQuery, frame: Frame, params: dict[str, Any]
    ) -> list[ElementMatch]:  # pragma: no cover - never constructed
        return []


class FakeRegistry:
    """Minimal locator-registry stand-in used for service tests."""

    def __init__(self, locators: dict[str, Locator]) -> None:
        self.locators = locators
        self.params: dict[str, dict[str, Any]] = {}
        self.availability_calls = 0

    def get(self, name: str) -> Locator:
        locator = self.locators.get(name)
        if locator is None:
            raise LocatorUnavailable(f"unknown locator {name!r}")
        return locator

    def params_for(self, name: str) -> dict[str, Any]:
        return dict(self.params.get(name, {}))

    def availability(self) -> list[LocatorAvailability]:
        self.availability_calls += 1
        return [locator.availability() for locator in self.locators.values()]

    def find_text_extractor(self) -> tuple[str, Any]:
        for name, locator in self.locators.items():
            if locator.kind == "text":
                return name, locator
        raise LocatorUnavailable("no text locator is available")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_config(
    tmp_path: Path, perception: PerceptionSettings | None = None
) -> AppConfig:
    overlay = tmp_path / "nextautodesk-test.yml"
    overlay.write_text("auth:\n  enabled: false\n", encoding="utf-8")
    config = load_config(overlay)
    config.storage.data_dir = str(tmp_path / "data")
    config.perception = perception or PerceptionSettings()
    return config


def make_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    registry: FakeRegistry,
    *,
    settings: PerceptionSettings | None = None,
) -> tuple[PerceptionService, Any, DataStore]:
    drivers, _, _, screen, _ = make_bundle()
    config = make_config(tmp_path, settings)
    store = DataStore(config)
    store.ensure_directories()
    monkeypatch.setattr(
        PerceptionService,
        "_build_registry",
        lambda self, _settings: registry,
    )
    service = PerceptionService(
        config.perception,
        drivers=drivers,
        store=store,
        logger=None,
    )
    return service, screen, store


def relative_match(
    x: int = 2, y: int = 3, *, confidence: float = 0.9
) -> ElementMatch:
    return ElementMatch(
        box=Rect(x=x, y=y, width=4, height=5),
        center=Point(x=x + 2.0, y=y + 2.5),
        confidence=confidence,
        locator="template",
    )


def template_query(
    *,
    threshold: float | None = None,
    scales: list[float] | None = None,
    region: Rect | None = None,
    save_screenshot: bool = False,
    max_results: int | None = None,
    screenshot: str | None = None,
) -> ElementQuery:
    template = TemplateSpec(
        image_base64=None if screenshot else base64.b64encode(SMALL_PNG).decode("ascii"),
        screenshot=screenshot,
        threshold=threshold,
        scales=scales,
    )
    return ElementQuery(
        locator="template",
        region=region,
        template=template,
        save_screenshot=save_screenshot,
        max_results=max_results,
    )


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
def test_registry_instantiates_builtin_locators() -> None:
    registry = LocatorRegistry.build(PerceptionSettings())
    assert registry.names == ["template", "text"]
    availability = registry.availability()
    assert [item.name for item in availability] == ["template", "text"]
    assert {item.kind for item in availability} == {"template", "text"}


def test_registry_rejects_unknown_configured_locator() -> None:
    settings = PerceptionSettings(locators={"nope": LocatorSettings()})
    with pytest.raises(ConfigError) as excinfo:
        LocatorRegistry.build(settings, locator_classes=[OpenCVTemplateLocator])
    assert "unknown locators" in str(excinfo.value)


def test_registry_rejects_invalid_and_unknown_parameters() -> None:
    with pytest.raises(ConfigError):
        LocatorRegistry.build(
            PerceptionSettings(
                locators={"template": LocatorSettings(params={"threshold": 2.0})}
            ),
            locator_classes=[OpenCVTemplateLocator],
        )
    with pytest.raises(ConfigError):
        LocatorRegistry.build(
            PerceptionSettings(
                locators={"template": LocatorSettings(params={"treshold": 0.8})}
            ),
            locator_classes=[OpenCVTemplateLocator],
        )


def test_registry_fails_for_broken_builtin_locator() -> None:
    with pytest.raises(ConfigError) as excinfo:
        LocatorRegistry.build(PerceptionSettings(), locator_classes=[BrokenTemplateLocator])
    assert "failed to initialise" in str(excinfo.value)


def test_registry_ignores_unconfigured_broken_plugin() -> None:
    registry = LocatorRegistry.build(
        PerceptionSettings(), locator_classes=[OpenCVTemplateLocator, BrokenPluginLocator]
    )
    assert registry.names == ["template"]


def test_registry_fails_for_configured_broken_plugin() -> None:
    settings = PerceptionSettings(locators={"custom": LocatorSettings()})
    with pytest.raises(ConfigError):
        LocatorRegistry.build(settings, locator_classes=[BrokenPluginLocator])


def test_registry_disabled_locator_is_unavailable() -> None:
    settings = PerceptionSettings(
        locators={"template": LocatorSettings(enabled=False)}
    )
    registry = LocatorRegistry.build(settings, locator_classes=[FakeTemplateLocator])
    availability = registry.availability()
    assert availability[0].available is False
    assert availability[0].reason == "disabled in configuration"
    with pytest.raises(LocatorUnavailable):
        registry.get("template")


def test_registry_unknown_locator_lookup() -> None:
    registry = LocatorRegistry.build(PerceptionSettings(), locator_classes=[FakeTemplateLocator])
    with pytest.raises(LocatorUnavailable):
        registry.get("text")


# ---------------------------------------------------------------------------
# locate
# ---------------------------------------------------------------------------
def test_locate_translates_matches_to_absolute_coordinates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    locator = FakeTemplateLocator(matches=[relative_match()])
    registry = FakeRegistry({"template": locator})
    service, screen, _ = make_service(monkeypatch, tmp_path, registry)
    region = Rect(x=100, y=200, width=12, height=8)

    result = service.locate(template_query(region=region))

    assert result.locator == "template"
    assert result.attempts == 1
    assert result.elapsed_ms >= 0
    match = result.matches[0]
    assert match.box == Rect(x=102, y=203, width=4, height=5)
    assert match.center == Point(x=104.0, y=205.5)
    assert result.frame.region == region
    assert (result.frame.width, result.frame.height) == (12, 8)
    assert screen.screenshot_calls == [(region, "png")]
    # The locator sees frame-relative data only.
    assert locator.executions[0]["frame"].region == region


def test_locate_uses_whole_screen_without_region(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    locator = FakeTemplateLocator(matches=[])
    registry = FakeRegistry({"template": locator})
    service, screen, _ = make_service(monkeypatch, tmp_path, registry)

    result = service.locate(template_query())

    assert result.frame.region == Rect(x=0, y=0, width=1920, height=1080)
    assert screen.screenshot_calls[0][0] == Rect(x=0, y=0, width=1920, height=1080)


def test_locate_caps_result_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    locator = FakeTemplateLocator(
        matches=[
            relative_match(x=0, confidence=0.95),
            relative_match(x=10, confidence=0.9),
            relative_match(x=20, confidence=0.85),
        ]
    )
    registry = FakeRegistry({"template": locator})
    service, _, _ = make_service(
        monkeypatch,
        tmp_path,
        registry,
        settings=PerceptionSettings(max_results=2),
    )

    assert len(service.locate(template_query()).matches) == 2
    assert len(service.locate(template_query(max_results=1)).matches) == 1


def test_locate_persists_frame_when_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    locator = FakeTemplateLocator(matches=[])
    registry = FakeRegistry({"template": locator})
    service, _, store = make_service(monkeypatch, tmp_path, registry)

    result = service.locate(template_query(save_screenshot=True))

    assert result.frame.screenshot is not None
    assert result.frame.screenshot.name.endswith(".png")
    assert (store.screenshots.directory / result.frame.screenshot.name).is_file()


def test_locate_merges_query_overrides_into_configured_params(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    locator = FakeTemplateLocator(matches=[])
    registry = FakeRegistry({"template": locator})
    registry.params = {"template": {"threshold": 0.9, "grayscale": False, "scales": [1.0]}}
    service, _, _ = make_service(monkeypatch, tmp_path, registry)

    service.locate(template_query(threshold=0.7, scales=[1.0, 1.5]))

    params = locator.executions[0]["params"]
    assert params["threshold"] == 0.7
    assert params["grayscale"] is False
    assert params["scales"] == [1.0, 1.5]
    assert locator.validated[-1] == params


def test_locate_resolves_saved_template_before_any_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    locator = FakeTemplateLocator(matches=[])
    registry = FakeRegistry({"template": locator})
    service, screen, store = make_service(monkeypatch, tmp_path, registry)

    with pytest.raises(ValidationFailed):
        service.locate(template_query(screenshot="missing.png"))
    assert screen.screenshot_calls == []

    image = Image.new("RGB", (40, 24), (200, 30, 30))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    store.screenshots.save(buffer.getvalue(), name="login.png", width=40, height=24)

    service.locate(template_query(screenshot="login.png"))
    params = locator.executions[-1]["params"]
    assert params["template_name"] == "login.png"
    assert isinstance(params["template_image"], Image.Image)


def test_locate_rejects_invalid_screenshot_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = FakeRegistry({"template": FakeTemplateLocator(matches=[])})
    service, screen, _ = make_service(monkeypatch, tmp_path, registry)
    with pytest.raises(ValidationFailed):
        service.locate(template_query(screenshot="../etc/passwd"))
    assert screen.screenshot_calls == []


def test_locate_disabled_perception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = FakeRegistry({"template": FakeTemplateLocator(matches=[])})
    service, _, _ = make_service(
        monkeypatch,
        tmp_path,
        registry,
        settings=PerceptionSettings(enabled=False),
    )
    with pytest.raises(PerceptionUnavailable):
        service.locate(template_query())
    with pytest.raises(PerceptionUnavailable):
        service.ocr()


def test_locate_reports_configured_locator_disabled(tmp_path: Path) -> None:
    settings = PerceptionSettings(locators={"template": LocatorSettings(enabled=False)})
    drivers, _, _, _, _ = make_bundle()
    config = make_config(tmp_path, settings)
    store = DataStore(config)
    store.ensure_directories()
    service = PerceptionService(settings, drivers=drivers, store=store)

    with pytest.raises(LocatorUnavailable) as excinfo:
        service.locate(template_query())
    assert "disabled" in str(excinfo.value)


def test_capture_failure_maps_to_perception_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = FakeRegistry({"template": FakeTemplateLocator(matches=[])})
    service, screen, _ = make_service(monkeypatch, tmp_path, registry)

    def fail_screenshot(region: Rect | None = None, fmt: str = "png") -> bytes:
        del region, fmt
        raise DriverError("display is gone", driver="fake")

    screen.screenshot = fail_screenshot  # type: ignore[method-assign]
    with pytest.raises(PerceptionFailed) as excinfo:
        service.locate(template_query())
    assert excinfo.value.code.value == "PERCEPTION_FAILED"


def test_cancel_is_respected_before_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = FakeRegistry({"template": FakeTemplateLocator(matches=[])})
    service, screen, _ = make_service(monkeypatch, tmp_path, registry)
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(TaskCancelled):
        service.locate(template_query(), cancel=cancel)
    assert screen.screenshot_calls == []


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------
def _ocr_line(text: str = "HELLO", x: int = 1, y: int = 2) -> OcrLine:
    box = Rect(x=x, y=y, width=30, height=10)
    word = OcrWord(text=text, box=box, center=Point(x=x + 15.0, y=y + 5.0), confidence=80.0)
    return OcrLine(
        text=text,
        box=box,
        center=Point(x=x + 15.0, y=y + 5.0),
        confidence=80.0,
        words=[word],
    )


def test_ocr_translates_lines_and_words_to_absolute_coordinates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    extractor = FakeTextLocator(lines=[_ocr_line()])
    registry = FakeRegistry({"text": extractor})
    service, screen, _ = make_service(monkeypatch, tmp_path, registry)
    region = Rect(x=50, y=60, width=100, height=40)

    result = service.ocr(region=region)

    assert result.text == "HELLO"
    assert result.frame.region == region
    line = result.lines[0]
    assert line.box == Rect(x=51, y=62, width=30, height=10)
    assert line.center == Point(x=66.0, y=67.0)
    assert line.words[0].box == Rect(x=51, y=62, width=30, height=10)
    assert screen.screenshot_calls == [(region, "png")]


def test_ocr_forwards_language_and_psm_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    extractor = FakeTextLocator(lines=[])
    registry = FakeRegistry({"text": extractor})
    registry.params = {"text": {"languages": ["eng"], "psm": 6, "min_confidence": 40}}
    service, _, _ = make_service(monkeypatch, tmp_path, registry)

    service.ocr(languages=["eng", "chi_sim"], psm=7, min_confidence=10)

    execution = extractor.executions[-1]
    assert execution["languages"] == ["eng", "chi_sim"]
    assert execution["psm"] == 7
    assert execution["min_confidence"] == 10
    assert execution["params"]["psm"] == 7
    assert execution["params"]["min_confidence"] == 10


def test_ocr_without_text_locator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = FakeRegistry({"template": FakeTemplateLocator(matches=[])})
    service, _, _ = make_service(monkeypatch, tmp_path, registry)
    with pytest.raises(LocatorUnavailable):
        service.ocr()


# ---------------------------------------------------------------------------
# reload
# ---------------------------------------------------------------------------
def test_reload_swaps_settings_and_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = FakeRegistry({"template": FakeTemplateLocator(matches=[])})
    second = FakeRegistry({"text": FakeTextLocator(lines=[])})
    registries = [first, second]
    drivers, _, _, _, _ = make_bundle()
    config = make_config(tmp_path)
    store = DataStore(config)
    store.ensure_directories()
    monkeypatch.setattr(
        PerceptionService, "_build_registry", lambda self, _settings: registries.pop(0)
    )
    service = PerceptionService(config.perception, drivers=drivers, store=store)
    reloaded = PerceptionSettings(max_results=5)

    service.reload(reloaded)

    assert service.settings is reloaded
    assert [item.name for item in service.locator_availability()] == ["text"]
    assert second.availability_calls >= 1


def test_reload_failure_keeps_running_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = FakeRegistry({"template": FakeTemplateLocator(matches=[])})
    drivers, _, _, _, _ = make_bundle()
    config = make_config(tmp_path)
    store = DataStore(config)
    store.ensure_directories()
    monkeypatch.setattr(
        PerceptionService, "_build_registry", lambda self, _settings: registry
    )
    service = PerceptionService(config.perception, drivers=drivers, store=store)
    original = service.settings

    monkeypatch.setattr(
        PerceptionService,
        "_build_registry",
        lambda self, _settings: (_ for _ in ()).throw(ConfigError("broken config")),
    )
    with pytest.raises(ConfigError):
        service.reload(PerceptionSettings(max_results=1))
    assert service.settings is original

    # The service still works with the old registry.
    locator = registry.locators["template"]
    assert isinstance(locator, FakeTemplateLocator)
    service.locate(template_query())
    assert locator.executions


def test_reload_rejects_invalid_locator_configuration(tmp_path: Path) -> None:
    drivers, _, _, _, _ = make_bundle()
    settings = PerceptionSettings()
    config = make_config(tmp_path, settings)
    store = DataStore(config)
    store.ensure_directories()
    service = PerceptionService(settings, drivers=drivers, store=store)

    with pytest.raises(ConfigError):
        service.reload(
            PerceptionSettings(locators={"nope": LocatorSettings()})
        )
    assert service.settings is settings


def test_text_locator_service_rejects_bad_ocr_params(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    extractor = FakeTextLocator(lines=[])

    def reject(params: Any) -> None:
        raise ValueError("bad psm")

    extractor.validate_params = reject  # type: ignore[method-assign]
    registry = FakeRegistry({"text": extractor})
    service, _, _ = make_service(monkeypatch, tmp_path, registry)
    with pytest.raises(ValidationFailed):
        service.ocr(psm=1)

