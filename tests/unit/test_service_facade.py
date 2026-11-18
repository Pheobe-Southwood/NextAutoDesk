"""Unit tests for the DesktopService facade with fake drivers/actions."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from nextautodesk.actions.base import ActionHandler
from nextautodesk.actions.registry import ActionRegistry
from nextautodesk.config import LocatorSettings, load_config
from nextautodesk.errors import (
    ActionFailed,
    DesktopUnavailable,
    InvalidArgument,
    ReloadError,
    StrategyUnavailable,
)
from nextautodesk.execution import SerialExecutor
from nextautodesk.models import ActionResult, MouseButton, Point
from nextautodesk.service import DesktopService
from nextautodesk.storage import DataStore
from nextautodesk.strategies.base import OperationRequest, StrategyOutcome, StrategyProvider
from nextautodesk.strategies.registry import StrategyRegistry


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeScreen:
    def __init__(self) -> None:
        self.width, self.height = 1920, 1080
        self.screenshots = 0

    def size(self):
        from nextautodesk.models import Size

        return Size(width=self.width, height=self.height)

    def screenshot(self, region=None, fmt="png"):
        self.screenshots += 1
        return b"img"


class FakeMouse:
    def __init__(self) -> None:
        self.x, self.y = 0.0, 0.0
        self.calls: list[tuple] = []

    def position(self):
        return Point(x=self.x, y=self.y)

    def move_to(self, x, y, duration_s=0.0):
        self.calls.append(("move_to", x, y, duration_s))
        self.x, self.y = x, y

    def button_down(self, button: MouseButton):
        self.calls.append(("down", button))

    def button_up(self, button: MouseButton):
        self.calls.append(("up", button))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))

    def buttons_down(self):
        return set()


class FakeKeyboard:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def key_down(self, key):
        self.keys.append(f"down:{key}")

    def key_up(self, key):
        self.keys.append(f"up:{key}")

    def type_text(self, text):
        self.keys.append(f"text:{text}")

    def char_down(self, char):
        self.keys.append(f"cdown:{char}")

    def char_up(self, char):
        self.keys.append(f"cup:{char}")

    def supports_unicode(self):
        return True


class FakeWindow:
    def window_manager_name(self):
        return "fakewm"

    def list_windows(self):
        return []

    def active_window(self):
        return None


class FakeProvider(StrategyProvider):
    name = "human_movement"
    version = "test"
    from nextautodesk.operations import Operation as _Operation

    supported = frozenset(_Operation)

    def execute(self, request: OperationRequest, params, ctx) -> StrategyOutcome:
        return StrategyOutcome(operation=request.operation, provider=self.name, duration_ms=1.0)


class PingParams(BaseModel):
    value: int = 0
    block: bool = False


class PingAction(ActionHandler[PingParams]):
    name = "ping"
    summary = "test action"
    category = "test"
    mutating = True
    params_model = PingParams

    def __init__(self) -> None:
        self.runs: list[int] = []

    def execute(self, params: PingParams, ctx) -> dict[str, Any]:
        if params.block:
            ctx.sleep(0.2)
        self.runs.append(params.value)
        return {"value": params.value, "request_id": ctx.request_id}


class ReadOnlyAction(PingAction):
    name = "ping.ro"
    mutating = False


class FailingAction(PingAction):
    name = "ping.fail"

    def execute(self, params, ctx):
        raise ActionFailed("intentional", details={"why": "test"})


def _services(config, *, start_executor=True):
    drivers = type(
        "Drivers",
        (),
        {
            "mouse": FakeMouse(),
            "keyboard": FakeKeyboard(),
            "screen": FakeScreen(),
            "window": FakeWindow(),
        },
    )()
    from nextautodesk.drivers.base import DriverBundle

    bundle = DriverBundle(
        mouse=drivers.mouse, keyboard=drivers.keyboard, screen=drivers.screen, window=drivers.window
    )
    strategies = StrategyRegistry.build(
        config.strategies,
        provider_classes=[FakeProvider, _DirectProvider],
        logger=None,
    )
    actions = ActionRegistry([PingAction(), ReadOnlyAction(), FailingAction()])
    executor = SerialExecutor(max_queue_size=16, name="test")
    if start_executor:
        executor.start()
    service = DesktopService(
        config,
        store=DataStore(config),
        drivers=bundle,
        strategies=strategies,
        actions=actions,
        executor=executor,
    )
    return service


@pytest.fixture()
def config(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "k")
    monkeypatch.setenv("NEXT_AUTODESK_DATA_DIR", str(tmp_path / "data"))
    cfg = load_config()
    cfg.desktop.require_window_manager = True
    return cfg


def test_probe_desktop_ok(config):
    service = _services(config, start_executor=False)
    service._probe_desktop()
    assert service.screen_size().width == 1920


def test_probe_desktop_fails_when_screen_unavailable(config):
    service = _services(config, start_executor=False)

    def boom():
        raise RuntimeError("no display")

    service.drivers.screen.size = boom  # type: ignore[method-assign]
    with pytest.raises(DesktopUnavailable):
        service._probe_desktop()


def test_mutating_action_runs_on_executor(config):
    service = _services(config)
    try:
        result = service.execute_action("ping", {"value": 7}, request_id="req-1")
        assert isinstance(result, ActionResult)
        assert result.output["value"] == 7
        assert result.output["request_id"] == "req-1"
        assert service.executor.stats().completed == 1
    finally:
        service.executor.shutdown()


def test_read_only_action_bypasses_executor(config):
    service = _services(config, start_executor=False)
    result = service.execute_action("ping.ro", {"value": 3})
    assert result.output["value"] == 3
    assert service.executor.stats().completed == 0


def test_action_failure_propagates(config):
    service = _services(config)
    try:
        with pytest.raises(ActionFailed) as excinfo:
            service.execute_action("ping.fail", {})
        assert excinfo.value.code.value == "ACTION_FAILED"
    finally:
        service.executor.shutdown()


def test_unknown_action_rejected(config):
    service = _services(config, start_executor=False)
    from nextautodesk.errors import UnknownAction

    with pytest.raises(UnknownAction):
        service.execute_action("nope", {})


def test_invalid_params_rejected(config):
    from nextautodesk.errors import ValidationFailed

    service = _services(config, start_executor=False)
    with pytest.raises(ValidationFailed):
        service.execute_action("ping", {"value": "not-an-int"})


def test_strategy_override_disabled_rejected(config):
    config.server.allow_strategy_override = False
    service = _services(config, start_executor=False)
    with pytest.raises(InvalidArgument):
        service.execute_action("ping.ro", {}, strategy="direct")


def test_strategy_override_unknown_rejected(config):
    service = _services(config, start_executor=False)
    with pytest.raises(StrategyUnavailable):
        service.execute_action("ping.ro", {}, strategy="nope")


def test_capabilities_shape(config):
    service = _services(config, start_executor=False)
    caps = service.capabilities()
    assert caps["version"].startswith("0.1.0")
    assert caps["screen"] == {"width": 1920, "height": 1080}
    assert caps["desktop"]["window_manager"] == "fakewm"
    assert "ping" in caps["actions"]
    assert caps["features"]["vnc"] == "reserved"
    assert caps["perception"]["enabled"] is True
    assert [entry["name"] for entry in caps["perception"]["locators"]] == [
        "template",
        "text",
    ]
    assert caps["features"]["screen_element_perception"] is True
    assert caps["features"]["api_tiers"] == [
        "primitive",
        "perception",
        "composite",
        "workflow",
    ]


def test_reload_rejects_desktop_change(config):
    service = _services(config, start_executor=False)
    new = config.model_copy(deep=True)
    new.desktop.width = 1280
    with pytest.raises(ReloadError):
        service.reload_config(new)


def test_reload_applies_strategy_change(config, monkeypatch):
    service = _services(config, start_executor=False)
    new = config.model_copy(deep=True)
    new.strategies.operations["mouse_move"].provider = "direct"

    # patch registry build so the test does not depend on concrete provider modules
    import nextautodesk.strategies.registry as registry_module

    original = registry_module.StrategyRegistry.build.__func__

    def fake_build(cls, settings, **kwargs):
        return original(cls, settings, provider_classes=[FakeProvider, _DirectProvider])

    monkeypatch.setattr(registry_module.StrategyRegistry, "build", classmethod(fake_build))
    applied = service.reload_config(new)
    assert applied.strategies.operations["mouse_move"].provider == "direct"
    assert service.config is applied
    # The 19th operation (down-only keyboard state primitive) is configured on
    # the direct provider and must stay resolvable after a reload.
    from nextautodesk.operations import Operation

    resolved = service.strategies.resolve(Operation.KEYBOARD_KEY_DOWN)
    assert resolved.provider.name == "direct"


def test_reload_rejects_invalid_perception_config(config, monkeypatch):
    service = _services(config, start_executor=False)
    new = config.model_copy(deep=True)
    new.perception.locators["nope"] = LocatorSettings()

    import nextautodesk.strategies.registry as registry_module

    original = registry_module.StrategyRegistry.build.__func__

    def fake_build(cls, settings, **kwargs):
        return original(cls, settings, provider_classes=[FakeProvider, _DirectProvider])

    monkeypatch.setattr(registry_module.StrategyRegistry, "build", classmethod(fake_build))
    with pytest.raises(ReloadError) as excinfo:
        service.reload_config(new)
    assert "invalid perception configuration" in str(excinfo.value)
    assert service.config is config


class _DirectProvider(FakeProvider):
    name = "direct"


def test_health_report_built(config):

    service = _services(config, start_executor=False)
    # health module is owned by api-tasks; skip if not present yet
    import importlib.util

    if importlib.util.find_spec("nextautodesk.health") is None:  # pragma: no cover
        pytest.skip("health module not implemented yet")
    from nextautodesk.health import build_health_report

    report = build_health_report(service)
    assert report.status in {"ok", "degraded", "unhealthy"}
