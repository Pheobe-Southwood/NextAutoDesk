"""Headless unit tests for the perception actions and composite interactions."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from test_engine_fakes import make_bundle

from nextautodesk.actions import elements, interactions
from nextautodesk.actions.base import ActionContext, ActionHandler
from nextautodesk.actions.registry import ActionRegistry
from nextautodesk.config import load_config
from nextautodesk.errors import (
    ActionFailed,
    ElementNotFound,
    PerceptionUnavailable,
    TaskCancelled,
)
from nextautodesk.models import Point, Rect
from nextautodesk.perception.models import (
    ElementMatch,
    ElementQuery,
    FrameInfo,
    LocateResult,
    OcrLine,
    OcrResult,
    OcrWord,
    TemplateSpec,
    TextSpec,
)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _AnyParams(BaseModel):
    model_config = ConfigDict(extra="allow")


class RecordingHandler(ActionHandler[_AnyParams]):
    """Action handler that records parsed parameters and returns canned output."""

    category = "test"
    params_model = _AnyParams

    def __init__(self, name: str, output: dict[str, Any] | None = None) -> None:
        self.name = name
        self.output = output or {}
        self.calls: list[dict[str, Any]] = []

    def execute(self, params: _AnyParams, ctx: ActionContext) -> dict[str, Any]:
        del ctx
        self.calls.append(params.model_dump(mode="python"))
        return dict(self.output)


class FakePerception:
    """Stands in for ``PerceptionService`` in action-layer tests."""

    def __init__(
        self,
        results: list[LocateResult] | LocateResult,
        *,
        ocr_result: OcrResult | None = None,
    ) -> None:
        self.results = list(results) if isinstance(results, list) else [results]
        self.ocr_result = ocr_result
        self.locate_calls: list[ElementQuery] = []
        self.ocr_calls: list[dict[str, Any]] = []

    def locate(
        self, query: ElementQuery, *, cancel: threading.Event | None = None
    ) -> LocateResult:
        del cancel
        self.locate_calls.append(query)
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]

    def ocr(self, **kwargs: Any) -> OcrResult:
        self.ocr_calls.append(kwargs)
        assert self.ocr_result is not None
        return self.ocr_result


def match_at(x: float = 100.0, y: float = 40.0) -> ElementMatch:
    return ElementMatch(
        box=Rect(x=int(x) - 10, y=int(y) - 5, width=20, height=10),
        center=Point(x=x, y=y),
        confidence=0.95,
        locator="text",
        text="Submit",
    )


def frame_info() -> FrameInfo:
    return FrameInfo(
        region=Rect(x=0, y=0, width=100, height=100),
        width=100,
        height=100,
        captured_at=datetime.now(UTC),
    )


def locate_result(
    matches: list[ElementMatch] | None = None, *, attempts: int = 1
) -> LocateResult:
    return LocateResult(
        locator="text",
        matches=matches or [],
        frame=frame_info(),
        elapsed_ms=1.5,
        attempts=attempts,
    )


def ocr_result() -> OcrResult:
    box = Rect(x=1, y=2, width=30, height=10)
    word = OcrWord(text="HELLO", box=box, center=Point(x=16.0, y=7.0), confidence=88.0)
    line = OcrLine(
        text="HELLO",
        box=box,
        center=Point(x=16.0, y=7.0),
        confidence=88.0,
        words=[word],
    )
    return OcrResult(lines=[line], text="HELLO", frame=frame_info(), elapsed_ms=3.0)


def text_query(text: str = "Submit") -> ElementQuery:
    return ElementQuery(locator="text", text=TextSpec(text=text, match="contains"))


def _load_test_config(tmp_path: Any):
    """Load the packaged defaults with authentication disabled.

    The action layer only needs a valid AppConfig; loading the real defaults keeps
    the full 19-operation strategy table without requiring deployment API keys.
    """

    overlay = tmp_path / "nextautodesk-test.yml"
    overlay.write_text("auth:\n  enabled: false\n", encoding="utf-8")
    config = load_config(overlay)
    config.storage.data_dir = str(tmp_path / "data")
    return config


def make_context(
    tmp_path: Any,
    *,
    actions: ActionRegistry | None = None,
    perception: Any = None,
    cancel: threading.Event | None = None,
) -> ActionContext:
    bundle, *_ = make_bundle()
    config = _load_test_config(tmp_path)
    return ActionContext(
        config=config,
        drivers=bundle,
        strategies=SimpleNamespace(),  # type: ignore[arg-type]
        store=SimpleNamespace(),
        logger=logging.getLogger("nextautodesk.tests"),
        cancel=cancel or threading.Event(),
        actions=actions,
        perception=perception,
    )


def handler_by_name(module: Any, name: str) -> Any:
    return next(handler for handler in module.ACTIONS if handler.name == name)


def execute(handler: Any, ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    return handler.execute(handler.parse_params(params), ctx)


# ---------------------------------------------------------------------------
# catalogue metadata
# ---------------------------------------------------------------------------
def test_element_actions_metadata() -> None:
    assert [handler.name for handler in elements.ACTIONS] == [
        "element.find",
        "element.wait",
        "element.ocr",
    ]
    for handler in elements.ACTIONS:
        assert handler.tier == "perception"
        assert handler.category == "elements"
        assert handler.mutating is False
        assert handler.summary


def test_interaction_actions_metadata() -> None:
    assert [handler.name for handler in interactions.ACTIONS] == [
        "interaction.click",
        "interaction.type",
    ]
    for handler in interactions.ACTIONS:
        assert handler.tier == "composite"
        assert handler.category == "interactions"
        assert handler.mutating is True
        assert handler.summary


# ---------------------------------------------------------------------------
# element.find / element.wait / element.ocr
# ---------------------------------------------------------------------------
def test_element_find_returns_locate_payload(tmp_path: Any) -> None:
    perception = FakePerception(locate_result([match_at()]))
    ctx = make_context(tmp_path, perception=perception)
    handler = handler_by_name(elements, "element.find")

    output = execute(handler, ctx, {"query": text_query().model_dump()})

    assert output["locator"] == "text"
    assert output["matches"][0]["center"] == {"x": 100.0, "y": 40.0}
    assert output["attempts"] == 1
    assert len(perception.locate_calls) == 1
    assert ctx.cancel is not None


def test_element_find_without_perception_is_unavailable(tmp_path: Any) -> None:
    ctx = make_context(tmp_path, perception=None)
    handler = handler_by_name(elements, "element.find")
    with pytest.raises(PerceptionUnavailable):
        execute(handler, ctx, {"query": text_query().model_dump()})


def test_element_find_respects_cancellation(tmp_path: Any) -> None:
    cancel = threading.Event()
    cancel.set()
    perception = FakePerception(locate_result([match_at()]))
    ctx = make_context(tmp_path, perception=perception, cancel=cancel)
    handler = handler_by_name(elements, "element.find")
    with pytest.raises(TaskCancelled):
        execute(handler, ctx, {"query": text_query().model_dump()})
    assert perception.locate_calls == []


def test_element_wait_polls_until_found(tmp_path: Any) -> None:
    perception = FakePerception(
        [locate_result(), locate_result([match_at()])]
    )
    ctx = make_context(tmp_path, perception=perception)
    handler = handler_by_name(elements, "element.wait")

    output = execute(
        handler,
        ctx,
        {"query": text_query().model_dump(), "timeout_ms": 1000, "poll_interval_ms": 10},
    )

    assert output["matches"][0]["center"] == {"x": 100.0, "y": 40.0}
    assert output["attempts"] == 2
    assert output["elapsed_ms"] >= 0
    assert len(perception.locate_calls) == 2


def test_element_wait_times_out(tmp_path: Any) -> None:
    perception = FakePerception(locate_result())
    ctx = make_context(tmp_path, perception=perception)
    handler = handler_by_name(elements, "element.wait")

    with pytest.raises(ElementNotFound) as excinfo:
        execute(
            handler,
            ctx,
            {
                "query": text_query().model_dump(),
                "timeout_ms": 40,
                "poll_interval_ms": 10,
            },
        )
    assert excinfo.value.code.value == "ELEMENT_NOT_FOUND"
    assert excinfo.value.details["attempts"] >= 2
    assert len(perception.locate_calls) >= 2


def test_element_wait_respects_cancellation(tmp_path: Any) -> None:
    cancel = threading.Event()
    cancel.set()
    perception = FakePerception(locate_result())
    ctx = make_context(tmp_path, perception=perception, cancel=cancel)
    handler = handler_by_name(elements, "element.wait")
    with pytest.raises(TaskCancelled):
        execute(handler, ctx, {"query": text_query().model_dump(), "timeout_ms": 100})
    assert perception.locate_calls == []


def test_element_ocr_forwards_parameters(tmp_path: Any) -> None:
    perception = FakePerception(locate_result(), ocr_result=ocr_result())
    ctx = make_context(tmp_path, perception=perception)
    handler = handler_by_name(elements, "element.ocr")
    region = {"x": 10, "y": 20, "width": 100, "height": 50}

    output = execute(
        handler,
        ctx,
        {
            "region": region,
            "languages": ["eng"],
            "psm": 7,
            "min_confidence": 40,
            "save_screenshot": True,
        },
    )

    assert output["text"] == "HELLO"
    assert output["lines"][0]["text"] == "HELLO"
    call = perception.ocr_calls[0]
    assert call["region"] == Rect(x=10, y=20, width=100, height=50)
    assert call["languages"] == ["eng"]
    assert call["psm"] == 7
    assert call["min_confidence"] == 40
    assert call["save_screenshot"] is True
    assert call["cancel"] is ctx.cancel


# ---------------------------------------------------------------------------
# interaction.click / interaction.type
# ---------------------------------------------------------------------------
def build_registry(
    *,
    wait_output: dict[str, Any] | None = None,
    find_output: dict[str, Any] | None = None,
    click_output: dict[str, Any] | None = None,
    typed_output: dict[str, Any] | None = None,
) -> tuple[ActionRegistry, dict[str, RecordingHandler]]:
    handlers = {
        "element.wait": RecordingHandler(
            "element.wait", wait_output or locate_result([match_at()], attempts=3).model_dump(mode="json")
        ),
        "element.find": RecordingHandler(
            "element.find", find_output or locate_result([match_at()]).model_dump(mode="json")
        ),
        "mouse.click": RecordingHandler(
            "mouse.click", click_output or {"position": {"x": 105.0, "y": 35.0}}
        ),
        "keyboard.hotkey": RecordingHandler("keyboard.hotkey", {"keys": ["ctrl", "a"]}),
        "keyboard.type": RecordingHandler("keyboard.type", typed_output or {"typed": "hello"}),
        "keyboard.press": RecordingHandler("keyboard.press", {"key": "enter"}),
    }
    return ActionRegistry(handlers.values()), handlers


def test_interaction_click_locates_and_delegates_click(tmp_path: Any) -> None:
    registry, handlers = build_registry()
    ctx = make_context(tmp_path, actions=registry)
    handler = handler_by_name(interactions, "interaction.click")

    output = execute(
        handler,
        ctx,
        {
            "query": text_query().model_dump(),
            "button": "right",
            "clicks": 2,
            "interval_ms": 50,
            "offset_x": 5,
            "offset_y": -5,
            "wait_timeout_ms": 1500,
            "strategy": "direct",
        },
    )

    wait_params = handlers["element.wait"].calls[0]
    assert wait_params["timeout_ms"] == 1500
    assert wait_params["query"]["text"]["text"] == "Submit"
    click_params = handlers["mouse.click"].calls[0]
    assert click_params == {
        "x": 105.0,
        "y": 35.0,
        "button": "right",
        "clicks": 2,
        "interval_ms": 50,
        "strategy": "direct",
    }
    assert output["position"] == {"x": 105.0, "y": 35.0}
    assert output["target"]["center"] == {"x": 100.0, "y": 40.0}
    assert output["click"] == {"position": {"x": 105.0, "y": 35.0}}
    assert output["attempts"] == 3
    assert set(handlers["mouse.click"].calls[0]) >= {"x", "y", "button", "clicks"}


def test_interaction_click_uses_find_without_wait(tmp_path: Any) -> None:
    registry, handlers = build_registry()
    ctx = make_context(tmp_path, actions=registry)
    handler = handler_by_name(interactions, "interaction.click")

    output = execute(
        handler,
        ctx,
        {"query": text_query().model_dump(), "offset_x": 1, "wait_timeout_ms": 0},
    )

    assert handlers["element.wait"].calls == []
    assert len(handlers["element.find"].calls) == 1
    assert handlers["mouse.click"].calls[0]["x"] == 101.0
    assert output["attempts"] == 1


def test_interaction_click_without_matches_raises_not_found(tmp_path: Any) -> None:
    registry, handlers = build_registry(
        wait_output=locate_result([], attempts=2).model_dump(mode="json")
    )
    ctx = make_context(tmp_path, actions=registry)
    handler = handler_by_name(interactions, "interaction.click")

    with pytest.raises(ElementNotFound):
        execute(handler, ctx, {"query": text_query().model_dump()})
    assert handlers["mouse.click"].calls == []


def test_interaction_click_without_action_registry(tmp_path: Any) -> None:
    ctx = make_context(tmp_path, actions=None)
    handler = handler_by_name(interactions, "interaction.click")
    with pytest.raises(ActionFailed):
        execute(handler, ctx, {"query": text_query().model_dump()})


def test_interaction_type_delegates_keyboard_sequence(tmp_path: Any) -> None:
    registry, handlers = build_registry()
    ctx = make_context(tmp_path, actions=registry)
    handler = handler_by_name(interactions, "interaction.type")

    output = execute(
        handler,
        ctx,
        {
            "query": text_query("Username").model_dump(),
            "text": "hello",
            "clear": True,
            "submit": True,
            "strategy": "direct",
        },
    )

    invoked = [
        name
        for name in (
            "element.wait",
            "mouse.click",
            "keyboard.hotkey",
            "keyboard.type",
            "keyboard.press",
        )
        if handlers[name].calls
    ]
    assert invoked == [
        "element.wait",
        "mouse.click",
        "keyboard.hotkey",
        "keyboard.type",
        "keyboard.press",
    ]
    assert handlers["keyboard.hotkey"].calls[0] == {
        "keys": ["ctrl", "a"],
        "strategy": "direct",
    }
    assert handlers["keyboard.type"].calls[0] == {"text": "hello", "strategy": "direct"}
    assert handlers["keyboard.press"].calls[0] == {"key": "enter", "strategy": "direct"}
    assert output["typed"] == {"typed": "hello"}
    assert output["cleared"] is True
    assert output["submitted"] is True
    assert output["target"]["center"] == {"x": 100.0, "y": 40.0}


def test_interaction_type_without_clear_or_submit(tmp_path: Any) -> None:
    registry, handlers = build_registry()
    ctx = make_context(tmp_path, actions=registry)
    handler = handler_by_name(interactions, "interaction.type")

    output = execute(
        handler,
        ctx,
        {"query": text_query("Username").model_dump(), "text": "hello"},
    )

    assert handlers["keyboard.hotkey"].calls == []
    assert handlers["keyboard.press"].calls == []
    assert output["cleared"] is False
    assert output["submitted"] is False


def test_composite_reuses_real_perception_action(tmp_path: Any) -> None:
    perception = FakePerception(locate_result([match_at()]))
    registry = ActionRegistry(
        [
            elements.ElementFind(),
            RecordingHandler("mouse.click", {"clicked": True}),
        ]
    )
    ctx = make_context(tmp_path, actions=registry, perception=perception)
    handler = handler_by_name(interactions, "interaction.click")

    output = execute(
        handler,
        ctx,
        {"query": text_query().model_dump(), "wait_timeout_ms": 0},
    )

    assert len(perception.locate_calls) == 1
    assert output["click"] == {"clicked": True}
    assert output["target"]["text"] == "Submit"


def test_composite_query_models_are_validated(tmp_path: Any) -> None:
    registry, _handlers = build_registry()
    ctx = make_context(tmp_path, actions=registry)
    handler = handler_by_name(interactions, "interaction.click")
    from nextautodesk.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        execute(handler, ctx, {"query": {"locator": "auto"}})


def test_element_wait_persists_screenshot_only_on_success(tmp_path: Any) -> None:
    query = text_query("Submit").model_copy(update={"save_screenshot": True})
    perception = FakePerception(
        [locate_result(), locate_result([match_at()]), locate_result([match_at()])]
    )
    ctx = make_context(tmp_path, perception=perception)
    handler = handler_by_name(elements, "element.wait")

    output = execute(
        handler,
        ctx,
        {"query": query.model_dump(), "timeout_ms": 1000, "poll_interval_ms": 10},
    )

    assert [call.save_screenshot for call in perception.locate_calls] == [False, False, True]
    assert output["attempts"] == 2


def test_element_wait_error_redacts_template_bytes(tmp_path: Any) -> None:
    image = "A" * 4096
    query = ElementQuery(
        locator="template",
        template=TemplateSpec(image_base64=image, threshold=0.8),
    )
    perception = FakePerception(locate_result())
    ctx = make_context(tmp_path, perception=perception)
    handler = handler_by_name(elements, "element.wait")

    with pytest.raises(ElementNotFound) as excinfo:
        execute(
            handler,
            ctx,
            {"query": query.model_dump(), "timeout_ms": 30, "poll_interval_ms": 10},
        )

    details = excinfo.value.details
    placeholder = details["query"]["template"]["image_base64"]
    assert placeholder == "<base64 omitted: 4096 bytes>"
    assert image not in str(details)
    assert details["query"]["template"]["threshold"] == 0.8
    assert details["query"]["locator"] == "template"
    assert details["timeout_ms"] == 30


def test_interaction_error_redacts_query_payload(tmp_path: Any) -> None:
    registry, handlers = build_registry(
        wait_output=locate_result([], attempts=2).model_dump(mode="json")
    )
    ctx = make_context(tmp_path, actions=registry)
    handler = handler_by_name(interactions, "interaction.click")
    query = ElementQuery(
        locator="template",
        template=TemplateSpec(image_base64="B" * 2048),
    )

    with pytest.raises(ElementNotFound) as excinfo:
        execute(handler, ctx, {"query": query.model_dump()})
    details = excinfo.value.details
    assert details["action"] == "interaction.click"
    assert details["query"]["template"]["image_base64"] == (
        "<base64 omitted: 2048 bytes>"
    )
    assert "B" * 2048 not in str(details)
    assert handlers["mouse.click"].calls == []
