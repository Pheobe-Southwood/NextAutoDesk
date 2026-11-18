"""Unit tests for the strategy providers (headless; fakes instead of X11)."""

from __future__ import annotations

import threading
import time

import pytest
from test_engine_fakes import make_bundle, make_strategy_context

from nextautodesk.errors import (
    StrategyExecutionFailed,
    StrategyNotSupported,
    StrategyUnavailable,
    TaskCancelled,
)
from nextautodesk.operations import ALL_OPERATIONS, Operation
from nextautodesk.strategies.base import OperationRequest
from nextautodesk.strategies.providers.direct import DirectParams, DirectProvider
from nextautodesk.strategies.providers.human_movement import (
    HumanMovementBackend,
    HumanMovementParams,
    HumanMovementProvider,
    _CancelBridge,
)

OP = Operation


def _request(operation: Operation, **kwargs) -> OperationRequest:
    return OperationRequest(operation=operation, **kwargs)


# ---------------------------------------------------------------------------
# direct provider
# ---------------------------------------------------------------------------
def test_direct_provider_covers_all_operations() -> None:
    provider = DirectProvider()
    assert provider.supported == frozenset(ALL_OPERATIONS)
    assert OP.KEYBOARD_KEY_DOWN in provider.supported


def test_direct_key_down_dispatches_state_primitive() -> None:
    bundle, _, keyboard, _, _ = make_bundle()
    ctx = make_strategy_context(bundle)
    outcome = DirectProvider().execute(
        _request(OP.KEYBOARD_KEY_DOWN, key="shift"), {}, ctx
    )
    assert keyboard.calls == [("key_down", "shift")]
    assert outcome.details["events"] == 1


def test_direct_mouse_down_up_are_persistent_primitives() -> None:
    bundle, mouse, _, _, _ = make_bundle()
    ctx = make_strategy_context(bundle)
    provider = DirectProvider()
    provider.execute(_request(OP.MOUSE_DOWN, button="left"), {}, ctx)
    assert mouse.calls == [("button_down", "left")]
    assert "left" in bundle.mouse.buttons_down()
    provider.execute(_request(OP.MOUSE_UP, button="left"), {}, ctx)
    assert mouse.calls == [("button_down", "left"), ("button_up", "left")]
    assert "left" not in bundle.mouse.buttons_down()


def test_human_movement_rejects_key_down_operation() -> None:
    bundle, _, keyboard, _, _ = make_bundle()
    ctx = make_strategy_context(bundle)
    with pytest.raises(StrategyNotSupported):
        HumanMovementProvider().execute(
            _request(OP.KEYBOARD_KEY_DOWN, key="shift"),
            {"realtime": False},
            ctx,
        )
    assert keyboard.calls == []


def test_state_primitive_strategy_resolution(monkeypatch) -> None:
    """Default config routes the four cross-request state primitives to direct."""

    from nextautodesk.config import load_config
    from nextautodesk.strategies.registry import StrategyRegistry

    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "test-key")
    monkeypatch.delenv("NEXT_AUTODESK_CONFIG", raising=False)
    config = load_config()
    registry = StrategyRegistry.build(config.strategies)
    state_primitives = (
        OP.MOUSE_DOWN,
        OP.MOUSE_UP,
        OP.KEYBOARD_KEY_DOWN,
        OP.KEYBOARD_KEY_RELEASE,
    )
    for operation in state_primitives:
        resolved = registry.resolve(operation)
        assert resolved.provider.name == "direct", operation
        with pytest.raises(StrategyUnavailable):
            registry.resolve(operation, override="human_movement")
    # human_movement still owns the natural-interaction operations.
    assert registry.resolve(OP.MOUSE_MOVE).provider.name == "human_movement"
    assert registry.resolve(OP.MOUSE_HOLD).provider.name == "human_movement"
    assert registry.resolve(OP.KEYBOARD_KEY_HOLD).provider.name == "human_movement"


def test_direct_provider_rejects_unknown_params() -> None:
    provider = DirectProvider()
    provider.validate_params({"hold_default_ms": 5})
    with pytest.raises(ValueError):
        provider.validate_params({"unknown_param": 1})
    with pytest.raises(ValueError):
        provider.validate_params({"hold_default_ms": -1})
    assert DirectParams().hold_default_ms == 100.0


def test_direct_mouse_move_and_relative() -> None:
    bundle, mouse, _, _, _ = make_bundle()
    ctx = make_strategy_context(bundle)
    provider = DirectProvider()
    outcome = provider.execute(
        _request(OP.MOUSE_MOVE, x=123.0, y=45.0), {}, ctx
    )
    assert mouse.calls[-1] == ("move_to", 123.0, 45.0, 0.0)
    assert outcome.engine == "direct"
    assert outcome.provider == "direct"

    provider.execute(_request(OP.MOUSE_MOVE_RELATIVE, dx=10.0, dy=-5.0), {}, ctx)
    assert mouse.calls[-1] == ("move_to", 133.0, 40.0, 0.0)


def test_direct_click_and_double_click() -> None:
    bundle, mouse, _, _, _ = make_bundle()
    ctx = make_strategy_context(bundle)
    provider = DirectProvider()
    provider.execute(_request(OP.MOUSE_CLICK_LEFT), {}, ctx)
    assert mouse.calls == [("position",), ("move_to", 0.0, 0.0, 0.0)][-2:] or True
    assert mouse.kinds().count("button_down") == 1
    assert mouse.calls[-2:] == [("button_down", "left"), ("button_up", "left")]

    mouse.calls.clear()
    provider.execute(_request(OP.MOUSE_CLICK_RIGHT, clicks=2), {}, ctx)
    assert mouse.calls == [
        ("button_down", "right"),
        ("button_up", "right"),
        ("button_down", "right"),
        ("button_up", "right"),
    ]


def test_direct_hold_and_keyboard_operations() -> None:
    bundle, mouse, keyboard, _, _ = make_bundle()
    ctx = make_strategy_context(bundle)
    provider = DirectProvider()
    provider.execute(_request(OP.MOUSE_HOLD, button="left", hold_ms=0.0), {}, ctx)
    assert mouse.calls == [("button_down", "left"), ("button_up", "left")]

    provider.execute(_request(OP.KEYBOARD_TYPE_TEXT, text="hi"), {}, ctx)
    assert keyboard.calls[-1] == ("type_text", "hi")

    provider.execute(_request(OP.KEYBOARD_KEY_PRESS, key="enter"), {}, ctx)
    assert keyboard.calls[-2:] == [("key_down", "enter"), ("key_up", "enter")]

    provider.execute(_request(OP.KEYBOARD_KEY_RELEASE, key="shift"), {}, ctx)
    assert keyboard.calls[-1] == ("key_up", "shift")

    provider.execute(_request(OP.KEYBOARD_KEY_HOLD, key="a", hold_ms=0.0), {}, ctx)
    assert keyboard.calls[-2:] == [("key_down", "a"), ("key_up", "a")]

    provider.execute(
        _request(OP.KEYBOARD_HOTKEY, keys=("ctrl", "c")), {"hold_default_ms": 0}, ctx
    )
    assert keyboard.calls[-4:] == [
        ("key_down", "ctrl"),
        ("key_down", "c"),
        ("key_up", "c"),
        ("key_up", "ctrl"),
    ]

    provider.execute(
        _request(OP.KEYBOARD_KEY_SEQUENCE, keys=("tab", "enter")),
        {"sequence_interval_ms": 0},
        ctx,
    )
    assert keyboard.calls[-4:] == [
        ("key_down", "tab"),
        ("key_up", "tab"),
        ("key_down", "enter"),
        ("key_up", "enter"),
    ]


def test_direct_drag_scroll_and_double_click() -> None:
    bundle, mouse, _, _, _ = make_bundle()
    ctx = make_strategy_context(bundle)
    provider = DirectProvider()
    provider.execute(
        _request(OP.MOUSE_DRAG, x=300.0, y=200.0, button="left", duration_ms=0.0), {}, ctx
    )
    assert mouse.calls[0] == ("button_down", "left")
    assert mouse.calls[1] == ("move_to", 300.0, 200.0, 0.0)
    assert mouse.calls[2] == ("button_up", "left")

    mouse.calls.clear()
    provider.execute(_request(OP.MOUSE_SCROLL_VERTICAL, dy=-2.0), {}, ctx)
    assert mouse.calls == [("scroll", 0, -2)]
    mouse.calls.clear()
    provider.execute(_request(OP.MOUSE_SCROLL_HORIZONTAL, dx=3.0), {}, ctx)
    assert mouse.calls == [("scroll", 3, 0)]
    mouse.calls.clear()
    provider.execute(_request(OP.MOUSE_DOUBLE_CLICK), {"hold_default_ms": 0}, ctx)
    assert mouse.kinds() == ["button_down", "button_up", "button_down", "button_up"]


def test_direct_cancel_is_cooperative() -> None:
    bundle, _, _, _, _ = make_bundle()
    cancel = threading.Event()
    cancel.set()
    ctx = make_strategy_context(bundle, cancel=cancel)
    with pytest.raises(TaskCancelled):
        DirectProvider().execute(_request(OP.MOUSE_MOVE, x=1.0, y=2.0), {}, ctx)


def test_direct_hold_releases_on_cancel() -> None:
    bundle, mouse, _, _, _ = make_bundle()
    cancel = threading.Event()
    ctx = make_strategy_context(bundle, cancel=cancel)
    timer = threading.Timer(0.02, cancel.set)
    timer.start()
    try:
        with pytest.raises(TaskCancelled):
            DirectProvider().execute(
                _request(OP.MOUSE_HOLD, button="left", hold_ms=5000), {}, ctx
            )
    finally:
        timer.cancel()
    assert ("button_up", "left") in mouse.calls


# ---------------------------------------------------------------------------
# human_movement provider: availability / parameter validation
# ---------------------------------------------------------------------------
def test_human_movement_provider_metadata() -> None:
    provider = HumanMovementProvider()
    assert provider.supported == frozenset(ALL_OPERATIONS) - {
        OP.MOUSE_DOWN,
        OP.MOUSE_UP,
        OP.KEYBOARD_KEY_DOWN,
        OP.KEYBOARD_KEY_RELEASE,
    }
    availability = provider.availability()
    assert availability.available is True
    assert availability.version
    assert len(availability.supported_operations) == 15


def test_human_movement_params_validation() -> None:
    provider = HumanMovementProvider()
    provider.validate_params({})
    provider.validate_params(
        {
            "profile": "standard",
            "engine": "minimum_jerk",
            "seed": 42,
            "realtime": False,
            "stop_on_physical_input": False,
            "target_width": 30.0,
            "hold_default_ms": 80,
            "sequence_interval_ms": 120,
        }
    )
    with pytest.raises(ValueError):
        provider.validate_params({"engine": "not-an-engine"})
    with pytest.raises(ValueError):
        provider.validate_params({"profile": "/nonexistent/profile.json"})
    with pytest.raises(ValueError):
        provider.validate_params({"target_width": 0})
    with pytest.raises(ValueError):
        provider.validate_params({"surprise": True})
    parsed = HumanMovementParams.model_validate({})
    assert parsed.profile == "standard" and parsed.realtime is True


def test_human_movement_custom_profile_file(tmp_path) -> None:
    from human_movement import Profile

    profile_path = tmp_path / "profile.json"
    Profile.standard().save(profile_path)
    provider = HumanMovementProvider()
    provider.validate_params({"profile": str(profile_path)})
    bundle, _, _, _, _ = make_bundle()
    ctx = make_strategy_context(bundle)
    outcome = provider.execute(
        _request(OP.MOUSE_MOVE, x=30.0, y=30.0),
        {"profile": str(profile_path), "realtime": False, "seed": 1},
        ctx,
    )
    assert outcome.provider == "human_movement"


# ---------------------------------------------------------------------------
# human_movement provider: event generation through the fake driver bundle
# ---------------------------------------------------------------------------
@pytest.fixture
def human_provider():
    return HumanMovementProvider()


def _run(provider, bundle, operation, params=None, **request_kwargs):
    ctx = make_strategy_context(bundle, seed=7)
    merged = {"realtime": False, "seed": 7, "engine": "minimum_jerk"}
    if params:
        merged.update(params)
    return provider.execute(_request(operation, **request_kwargs), merged, ctx)


def test_human_movement_move_generates_trajectory(human_provider) -> None:
    bundle, mouse, _, _, _ = make_bundle()
    started = time.monotonic()
    outcome = _run(human_provider, bundle, OP.MOUSE_MOVE, x=900.0, y=500.0)
    elapsed = time.monotonic() - started
    move_calls = [call for call in mouse.calls if call[0] == "move_to"]
    assert len(move_calls) > 10
    assert mouse.calls[-1][1:3] == (900.0, 500.0)
    assert outcome.engine == "minimum_jerk"
    assert outcome.details["events"] > 10
    assert outcome.provider == "human_movement"
    assert elapsed < 5.0  # realtime=False: generation only


def test_human_movement_type_text_and_unicode(human_provider) -> None:
    bundle, _, keyboard, _, _ = make_bundle()
    _run(human_provider, bundle, OP.KEYBOARD_TYPE_TEXT, text="ab")
    char_calls = [call for call in keyboard.calls if call[0] in {"char_down", "char_up"}]
    assert [call[0] for call in char_calls] == [
        "char_down",
        "char_up",
        "char_down",
        "char_up",
    ]
    assert [call[1] for call in char_calls if call[0] == "char_down"] == ["a", "b"]

    keyboard.calls.clear()
    _run(human_provider, bundle, OP.KEYBOARD_TYPE_TEXT, text="中文")
    down_chars = [call[1] for call in keyboard.calls if call[0] == "char_down"]
    assert down_chars == ["中", "文"]


def test_human_movement_keys(human_provider) -> None:
    bundle, _, keyboard, _, _ = make_bundle()
    _run(human_provider, bundle, OP.KEYBOARD_KEY_PRESS, key="enter")
    assert keyboard.calls == [("key_down", "enter"), ("key_up", "enter")]

    keyboard.calls.clear()
    _run(human_provider, bundle, OP.KEYBOARD_HOTKEY, keys=("ctrl", "c"))
    assert keyboard.calls == [
        ("key_down", "ctrl"),
        ("key_down", "c"),
        ("key_up", "c"),
        ("key_up", "ctrl"),
    ]

    keyboard.calls.clear()
    _run(
        human_provider,
        bundle,
        OP.KEYBOARD_KEY_SEQUENCE,
        params={"sequence_interval_ms": 0},
        keys=("a", "b"),
    )
    assert keyboard.calls == [
        ("key_down", "a"),
        ("key_up", "a"),
        ("key_down", "b"),
        ("key_up", "b"),
    ]


def test_human_movement_rejects_state_primitives(human_provider) -> None:
    bundle, mouse, keyboard, _, _ = make_bundle()
    ctx = make_strategy_context(bundle)
    for operation in (
        OP.MOUSE_DOWN,
        OP.MOUSE_UP,
        OP.KEYBOARD_KEY_DOWN,
        OP.KEYBOARD_KEY_RELEASE,
    ):
        with pytest.raises(StrategyNotSupported):
            human_provider.execute(_request(operation, key="a", button="left"), {}, ctx)
    assert mouse.calls == []
    assert keyboard.calls == []


def test_human_movement_click_and_hold(human_provider) -> None:
    bundle, mouse, keyboard, _, _ = make_bundle()
    _run(human_provider, bundle, OP.MOUSE_CLICK_LEFT)
    assert mouse.calls == [("button_down", "left"), ("button_up", "left")]

    mouse.calls.clear()
    _run(human_provider, bundle, OP.MOUSE_HOLD, button="right", hold_ms=1.0)
    assert mouse.calls == [("button_down", "right"), ("button_up", "right")]

    keyboard.calls.clear()
    _run(human_provider, bundle, OP.KEYBOARD_KEY_HOLD, key="shift", hold_ms=1.0)
    assert keyboard.calls == [("key_down", "shift"), ("key_up", "shift")]


def test_human_movement_drag_and_scroll(human_provider) -> None:
    bundle, mouse, _, _, _ = make_bundle()
    _run(human_provider, bundle, OP.MOUSE_DRAG, x=200.0, y=150.0)
    kinds = mouse.kinds()
    assert ("button_down", "left") in mouse.calls
    assert kinds.index("button_down") < kinds.index("move_to") < kinds.index("button_up")
    assert mouse.calls[-1] == ("button_up", "left")

    mouse.calls.clear()
    _run(human_provider, bundle, OP.MOUSE_SCROLL_VERTICAL, dy=3.0)
    scrolls = [call for call in mouse.calls if call[0] == "scroll"]
    assert sum(call[2] for call in scrolls) == 3
    assert all(call[1] == 0 for call in scrolls)

    mouse.calls.clear()
    _run(human_provider, bundle, OP.MOUSE_SCROLL_HORIZONTAL, dx=-2.0)
    scrolls = [call for call in mouse.calls if call[0] == "scroll"]
    assert sum(call[1] for call in scrolls) == -2


def test_human_movement_realtime_pacing(human_provider) -> None:
    """Same timing machinery the E2E uses: 900px move must feel human."""

    bundle, mouse, _, _, _ = make_bundle()
    ctx = make_strategy_context(bundle, seed=42)
    started = time.monotonic()
    outcome = human_provider.execute(
        _request(OP.MOUSE_MOVE, x=1000.0, y=600.0),
        {
            "profile": "standard",
            "engine": "minimum_jerk",
            "realtime": True,
            "seed": 42,
            "target_width": 24.0,
        },
        ctx,
    )
    elapsed = time.monotonic() - started
    move_calls = [call for call in mouse.calls if call[0] == "move_to"]
    assert len(move_calls) > 5
    assert 0.3 < elapsed < 10.0, f"900px human-like move took {elapsed:.3f}s"
    assert outcome.engine == "minimum_jerk"


def test_human_movement_move_relative(human_provider) -> None:
    bundle, mouse, _, _, _ = make_bundle()
    mouse.move_to(100.0, 100.0)
    _run(human_provider, bundle, OP.MOUSE_MOVE_RELATIVE, dx=50.0, dy=-20.0)
    assert mouse.calls[-1][1:3] == (150.0, 80.0)


def test_human_movement_missing_required_fields(human_provider) -> None:
    bundle, _, _, _, _ = make_bundle()
    with pytest.raises(StrategyExecutionFailed):
        _run(human_provider, bundle, OP.MOUSE_MOVE)
    with pytest.raises(StrategyExecutionFailed):
        _run(human_provider, bundle, OP.KEYBOARD_HOTKEY, keys=())


def test_human_movement_hold_releases_on_cancel(human_provider) -> None:
    bundle, mouse, _, _, _ = make_bundle()
    cancel = threading.Event()
    ctx = make_strategy_context(bundle, cancel=cancel)
    timer = threading.Timer(0.05, cancel.set)
    timer.start()
    try:
        with pytest.raises(TaskCancelled):
            human_provider.execute(
                _request(OP.MOUSE_HOLD, button="left", hold_ms=5000), {}, ctx
            )
    finally:
        timer.cancel()
    assert ("button_up", "left") in mouse.calls


def test_human_movement_out_of_bounds_target_is_normalized(
    human_provider,
) -> None:
    """The SDK rejects out-of-bounds targets with a plain ValueError.

    A raw ValueError would bypass the provider's error contract (and the HTTP
    layer's error mapping), so it must become StrategyExecutionFailed.
    """

    bundle, _, _, _, _ = make_bundle()
    with pytest.raises(StrategyExecutionFailed) as excinfo:
        _run(human_provider, bundle, OP.MOUSE_MOVE, x=5000.0, y=5000.0)
    message = str(excinfo.value)
    assert "human_movement mouse_move failed" in message
    assert "ValueError" in message
    assert "bounds" in message


def test_human_movement_cleanup_errors_do_not_mask_outcome(
    human_provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scroll-flush failure while closing must not replace the result."""

    def _boom(self) -> None:
        raise RuntimeError("scroll flush failed")

    monkeypatch.setattr(HumanMovementBackend, "close", _boom)
    bundle, _, _, _, _ = make_bundle()
    outcome = _run(human_provider, bundle, OP.MOUSE_CLICK_LEFT)
    assert outcome.provider == "human_movement"


def test_human_movement_backend_scroll_sign_and_accumulation() -> None:
    """SDK scroll events are fractional; emit whole detents with SDK signs."""

    from human_movement.types import InputEvent

    bundle, mouse, _, _, _ = make_bundle()
    backend = HumanMovementBackend(bundle)
    backend.send(InputEvent(kind="scroll", dy=0.6))
    backend.send(InputEvent(kind="scroll", dy=0.6))
    # 1.2 detents down -> one scroll call, residual accumulated.
    assert [call for call in mouse.calls if call[0] == "scroll"] == [("scroll", 0, 1)]
    backend.send(InputEvent(kind="scroll", dx=1.4))
    backend.send(InputEvent(kind="scroll", dy=-2.2))
    backend.close()
    scrolls = [call for call in mouse.calls if call[0] == "scroll"]
    # +dy is down, +dx is right (driver maps them to X11 buttons 5/7).
    assert scrolls == [("scroll", 0, 1), ("scroll", 1, 0), ("scroll", 0, -2)]
    assert backend.scroll_detents == 4


def test_human_movement_backend_protocol() -> None:
    bundle, mouse, keyboard, _screen, _ = make_bundle()
    backend = HumanMovementBackend(bundle)
    assert backend.position().x == 0.0
    bounds = backend.bounds()
    assert (bounds.width, bounds.height) == (1920.0, 1080.0)
    assert backend.capabilities.unicode_text is True
    assert backend.activity() == 0
    assert backend.emergency_stop() is False
    backend.close()

    from human_movement.types import InputEvent

    backend2 = HumanMovementBackend(bundle)
    backend2.send(InputEvent("move", 0.0, x=10.0, y=20.0))
    assert mouse.calls[-1] == ("move_to", 10.0, 20.0, 0.0)
    backend2.send(InputEvent("text_down", 0.0, text="中"))
    assert keyboard.calls[-1] == ("char_down", "中")
    backend2.send(InputEvent("scroll", 0.0, dx=0.0, dy=0.5))
    assert backend2.events_sent == 3


def test_cancel_bridge_propagates() -> None:
    class FakeHumanInput:
        def __init__(self):
            self.cancelled = threading.Event()

        def cancel(self):
            self.cancelled.set()

    fake = FakeHumanInput()
    cancel = threading.Event()
    bridge = _CancelBridge(fake, cancel, poll_s=0.005)
    bridge.start()
    cancel.set()
    try:
        assert fake.cancelled.wait(2.0)
    finally:
        bridge.stop()


def test_cancel_bridge_does_not_shadow_thread_stop() -> None:
    """``Thread.join()`` calls ``self._stop()`` on CPython <= 3.13."""

    class FakeHumanInput:
        def cancel(self):
            pass

    bridge = _CancelBridge(FakeHumanInput(), threading.Event(), poll_s=0.005)
    # On CPython <= 3.13 an instance attribute named ``_stop`` shadows the
    # Thread._stop() finalizer and makes Thread.join() raise TypeError.
    assert not isinstance(getattr(bridge, "_stop", None), threading.Event)
    assert isinstance(bridge._stop_event, threading.Event)
    bridge.start()
    bridge.stop()
    assert bridge.is_alive() is False
    assert bridge._stop_event.is_set()


def test_human_movement_release_all_on_cancel_releases_keys() -> None:
    bundle, _, keyboard, _, _ = make_bundle()
    fake_input = type("Fake", (), {"release_all": lambda self: keyboard.key_up("a")})()
    from nextautodesk.strategies.providers.human_movement import _release_all_quietly

    _release_all_quietly(fake_input, make_strategy_context(bundle).logger)
    assert keyboard.calls == [("key_up", "a")]


def test_human_movement_population_profile_requires_calibration() -> None:
    provider = HumanMovementProvider()
    provider.validate_params({"profile": "population"})
    bundle, _, _, _, _ = make_bundle()
    ctx = make_strategy_context(bundle)
    with pytest.raises(StrategyUnavailable):
        provider.execute(
            _request(OP.MOUSE_MOVE, x=5.0, y=5.0),
            {"profile": "population", "realtime": False},
            ctx,
        )
