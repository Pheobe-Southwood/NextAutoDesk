"""Unit tests for the optional alternate strategy providers.

These tests are completely headless: no X server is required, and the optional
third-party packages (HumanMoveMouse, windmouse) are simulated through
``sys.modules`` fakes. That also proves the provider modules never import their
optional dependencies at module-import time.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

from nextautodesk.drivers.base import DriverBundle
from nextautodesk.errors import (
    InvalidArgument,
    StrategyExecutionFailed,
    StrategyNotSupported,
    TaskCancelled,
)
from nextautodesk.models import Point
from nextautodesk.operations import Operation
from nextautodesk.strategies.base import OperationRequest, StrategyContext
from nextautodesk.strategies.providers import clack as clack_module
from nextautodesk.strategies.providers.clack import (
    ClackProvider,
    _build_command,
    _ByteStreamTranslator,
)
from nextautodesk.strategies.providers.human_move_mouse import HumanMoveMouseProvider
from nextautodesk.strategies.providers.windmouse import WindMouseProvider

LOGGER = logging.getLogger("tests.alt_providers")


# ---------------------------------------------------------------------------
# fakes / helpers
# ---------------------------------------------------------------------------
class FakeMouse:
    """Records driver calls; mirrors the MouseDriver protocol."""

    def __init__(self, position: tuple[float, float] = (0.0, 0.0)) -> None:
        self._position = Point(x=float(position[0]), y=float(position[1]))
        self.moves: list[tuple[float, float, float]] = []

    def position(self) -> Point:
        return self._position

    def move_to(self, x: float, y: float, duration_s: float = 0.0) -> None:
        self.moves.append((float(x), float(y), float(duration_s)))
        self._position = Point(x=float(x), y=float(y))

    def button_down(self, button: Any) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    def button_up(self, button: Any) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    def scroll(self, dx: int, dy: int) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    def buttons_down(self) -> set[Any]:  # pragma: no cover - unused
        return set()


class FakeKeyboard:
    """Records driver calls; mirrors the KeyboardDriver protocol."""

    def __init__(self) -> None:
        self.typed: list[str] = []
        self.keys: list[tuple[str, str]] = []
        #: Ordered event log used to reconstruct the resulting text.
        self.events: list[tuple[str, str]] = []

    def key_down(self, key: str) -> None:
        self.keys.append(("down", key))
        self.events.append(("key_down", key))

    def key_up(self, key: str) -> None:
        self.keys.append(("up", key))
        self.events.append(("key_up", key))

    def type_text(self, text: str) -> None:
        self.typed.append(text)
        self.events.append(("char", text))

    def char_down(self, char: str) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    def char_up(self, char: str) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    def supports_unicode(self) -> bool:
        return True


def make_ctx(
    *,
    position: tuple[float, float] = (0.0, 0.0),
    cancel: threading.Event | None = None,
):
    mouse = FakeMouse(position)
    keyboard = FakeKeyboard()
    bundle = DriverBundle(mouse=mouse, keyboard=keyboard, screen=object(), window=object())
    ctx = StrategyContext(
        drivers=bundle,
        cancel=cancel or threading.Event(),
        logger=LOGGER,
    )
    return ctx, mouse, keyboard


def hide_module(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``import name`` (and submodules) fail as if the package were missing."""

    for module_name in [item for item in sys.modules if item == name or item.startswith(f"{name}.")]:
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setitem(sys.modules, name, None)


def install_fake_humanmouse(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_path: Path,
    generate: Any,
) -> types.ModuleType:
    models = types.ModuleType("humanmouse.models")
    models.get_default_model_path = lambda: str(model_path)
    models.generate_mouse_trajectory = generate
    package = types.ModuleType("humanmouse")
    package.__path__ = []  # type: ignore[attr-defined]
    package.models = models
    monkeypatch.setitem(sys.modules, "humanmouse", package)
    monkeypatch.setitem(sys.modules, "humanmouse.models", models)
    return models


def install_fake_windmouse(monkeypatch: pytest.MonkeyPatch, wind_mouse: Any) -> types.ModuleType:
    core = types.ModuleType("windmouse.core")
    core.wind_mouse = wind_mouse
    package = types.ModuleType("windmouse")
    package.__path__ = []  # type: ignore[attr-defined]
    package.core = core
    monkeypatch.setitem(sys.modules, "windmouse", package)
    monkeypatch.setitem(sys.modules, "windmouse.core", core)
    return core


def reconstruct_text(events: list[tuple[str, str]]) -> str:
    """Apply a FakeKeyboard event log the way a terminal would."""

    result: list[str] = []
    for kind, value in events:
        if kind == "char":
            result.append(value)
        elif kind == "key_down" and value == "backspace":
            if result:
                result.pop()
        elif kind == "key_down" and value == "enter":
            result.append("\n")
        elif kind == "key_down" and value == "tab":
            result.append("\t")
    return "".join(result)


def wait_for(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# module import contract
# ---------------------------------------------------------------------------
def test_provider_modules_import_without_optional_packages() -> None:
    code = (
        "import sys\n"
        "import nextautodesk.strategies.providers.human_move_mouse\n"
        "import nextautodesk.strategies.providers.windmouse\n"
        "import nextautodesk.strategies.providers.clack\n"
        "for name in ('humanmouse', 'windmouse', 'pyautogui'):\n"
        "    assert name not in sys.modules, f'{name} imported eagerly'\n"
        "print('lazy-import-ok')\n"
    )
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    # pytest's `pythonpath = ["src"]` only applies to the pytest process; make
    # the child process resolve the working tree too.
    src_root = str(Path(clack_module.__file__).resolve().parents[3])
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (src_root, env.get("PYTHONPATH")))
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "lazy-import-ok" in result.stdout


def test_provider_names_and_supported_operations() -> None:
    assert HumanMoveMouseProvider.name == "human_move_mouse"
    assert HumanMoveMouseProvider.supported == frozenset(
        {Operation.MOUSE_MOVE, Operation.MOUSE_MOVE_RELATIVE}
    )
    assert WindMouseProvider.name == "windmouse"
    assert WindMouseProvider.supported == frozenset(
        {Operation.MOUSE_MOVE, Operation.MOUSE_MOVE_RELATIVE}
    )
    assert ClackProvider.name == "clack"
    assert ClackProvider.supported == frozenset({Operation.KEYBOARD_TYPE_TEXT})


def test_provider_modules_are_listed_for_discovery() -> None:
    from nextautodesk.strategies.providers import PROVIDER_MODULES

    assert {"human_move_mouse", "windmouse", "clack"} <= set(PROVIDER_MODULES)


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------
def test_human_move_mouse_unavailable_without_package(monkeypatch: pytest.MonkeyPatch) -> None:
    hide_module(monkeypatch, "humanmouse")
    availability = HumanMoveMouseProvider().availability()
    assert availability.available is False
    assert availability.reason
    assert "HumanMoveMouse" in availability.reason
    assert availability.supported_operations == [
        Operation.MOUSE_MOVE,
        Operation.MOUSE_MOVE_RELATIVE,
    ]


def test_human_move_mouse_available_when_package_and_model_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = tmp_path / "mouse_model.pkl"
    model.write_bytes(b"fake-model")
    install_fake_humanmouse(monkeypatch, model_path=model, generate=lambda **_: None)
    availability = HumanMoveMouseProvider().availability()
    assert availability.available is True
    assert availability.details["model_path"] == str(model)


def test_human_move_mouse_unavailable_when_model_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_fake_humanmouse(
        monkeypatch, model_path=tmp_path / "missing.pkl", generate=lambda **_: None
    )
    availability = HumanMoveMouseProvider().availability()
    assert availability.available is False
    assert "model" in (availability.reason or "").lower()


def test_windmouse_unavailable_without_package(monkeypatch: pytest.MonkeyPatch) -> None:
    hide_module(monkeypatch, "windmouse")
    availability = WindMouseProvider().availability()
    assert availability.available is False
    assert "windmouse" in (availability.reason or "")


def test_windmouse_available_with_fake_package(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_windmouse(monkeypatch, wind_mouse=lambda *args, **kwargs: iter(()))
    availability = WindMouseProvider().availability()
    assert availability.available is True
    assert availability.details["license"] == "GPL-3.0-only"


def test_clack_unavailable_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clack_module, "_resolve_binary", lambda settings: None)
    availability = ClackProvider().availability()
    assert availability.available is False
    assert "binary" in (availability.reason or "").lower()


def test_clack_available_with_configured_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "clack"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setattr(clack_module, "_resolve_binary", lambda settings: str(binary))
    availability = ClackProvider().availability()
    assert availability.available is True
    assert availability.details["binary"] == str(binary)


def test_clack_availability_uses_vendored_binary() -> None:
    vendored = clack_module._vendored_binary()
    if not vendored.is_file():  # pragma: no cover - installed (non-checkout) layout
        pytest.skip("repository-vendored clack binary not present")
    availability = ClackProvider().availability()
    assert availability.available is True


# ---------------------------------------------------------------------------
# parameter validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "params",
    [
        {"num_points": 1},
        {"num_points": "100"},
        {"num_points": True},
        {"jitter_amplitude": -0.1},
        {"jitter_amplitude": "0.3"},
        {"speed_factor": 0},
        {"speed_factor": float("nan")},
        {"seed": -1},
        {"seed": 2**32},
        {"model_path": 123},
        {"model_path": "/definitely/not/here.pkl"},
    ],
)
def test_human_move_mouse_rejects_invalid_params(
    params: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    hide_module(monkeypatch, "humanmouse")
    with pytest.raises(ValueError):
        HumanMoveMouseProvider().validate_params(params)


def test_human_move_mouse_accepts_valid_params(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    model = tmp_path / "mouse_model.pkl"
    model.write_bytes(b"fake-model")
    HumanMoveMouseProvider().validate_params(
        {"model_path": str(model), "num_points": 50, "jitter_amplitude": 0, "speed_factor": 2.5, "seed": 0}
    )
    HumanMoveMouseProvider().validate_params({})


@pytest.mark.parametrize(
    "params",
    [
        {"gravity": -1},
        {"wind": -1},
        {"max_step": 0},
        {"damped_distance": -1},
        {"step_duration_s": 0},
        {"step_duration_s": 11},
        {"seed": -1},
        {"gravity": "9"},
    ],
)
def test_windmouse_rejects_invalid_params(params: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        WindMouseProvider().validate_params(params)


def test_human_move_mouse_rejects_unknown_params() -> None:
    with pytest.raises(ValueError, match="unknown human_move_mouse parameters"):
        HumanMoveMouseProvider().validate_params({"points": 100})


def test_windmouse_rejects_unknown_params() -> None:
    with pytest.raises(ValueError, match="unknown windmouse parameters"):
        WindMouseProvider().validate_params({"max_steps": 15})


def test_human_move_mouse_availability_is_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = tmp_path / "mouse_model.pkl"
    model.write_bytes(b"fake-model")
    install_fake_humanmouse(monkeypatch, model_path=model, generate=lambda **_: None)
    provider = HumanMoveMouseProvider()
    calls: list[bool] = []
    original = provider._probe_availability

    def _counting() -> Any:
        calls.append(True)
        return original()

    monkeypatch.setattr(provider, "_probe_availability", _counting)
    first = provider.availability()
    second = provider.availability()
    assert len(calls) == 1
    assert first.available is True
    # Callers get an independent copy, so mutating one does not poison the cache.
    first.available = False
    assert provider.availability().available is True
    assert second.available is True


def test_windmouse_availability_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_windmouse(monkeypatch, wind_mouse=lambda *args, **kwargs: iter(()))
    provider = WindMouseProvider()
    calls: list[bool] = []
    original = provider._probe_availability

    def _counting() -> Any:
        calls.append(True)
        return original()

    monkeypatch.setattr(provider, "_probe_availability", _counting)
    provider.availability()
    provider.availability()
    assert len(calls) == 1


def test_windmouse_accepts_valid_params() -> None:
    WindMouseProvider().validate_params({})
    WindMouseProvider().validate_params(
        {"gravity": 0, "wind": 0, "max_step": 5, "damped_distance": 0, "step_duration_s": 0.05, "seed": 7}
    )


@pytest.mark.parametrize(
    "params",
    [
        {"binary": ""},
        {"wpm": 0},
        {"wpm": "80"},
        {"jitter": -0.1},
        {"error_rate": 1.5},
        {"correction_rate": -0.1},
        {"layout": "nonsense"},
        {"seed": -1},
        {"no_fatigue": "yes"},
        {"code_mode": 1},
        {"max_pause_ms": -1},
        {"thinking_pause_prob": 2},
        {"startup_timeout_s": 0},
        {"char_timeout_s": 0},
    ],
)
def test_clack_rejects_invalid_params(params: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ClackProvider().validate_params(params)


def test_clack_rejects_unknown_params() -> None:
    # A typo must fail loudly instead of silently using the default.
    with pytest.raises(ValueError, match="unknown clack parameters"):
        ClackProvider().validate_params({"wmp": 90})


@pytest.mark.parametrize("params", [{"wpm": float("nan")}, {"jitter": float("inf")}])
def test_clack_rejects_non_finite_params(params: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="finite"):
        ClackProvider().validate_params(params)


def test_clack_char_timeout_is_floored_above_max_pause() -> None:
    # A short configured idle timeout must not kill a run whose Clack CLI may
    # legitimately pause for max_pause_ms between two bytes.
    settings = clack_module._settings({"max_pause_ms": 20_000, "char_timeout_s": 1})
    assert clack_module._effective_char_timeout(settings) == 21.0
    assert clack_module._effective_char_timeout(clack_module._settings({})) == 10.0


def test_clack_raw_output_details_are_bounded() -> None:
    details = clack_module._raw_output_details(b"a" * 600)
    assert len(details["raw_output_hex"]) == 2 * clack_module._RAW_OUTPUT_LIMIT
    assert details["raw_output_truncated"] is True
    small = clack_module._raw_output_details(b"abc")
    assert small == {"raw_output_hex": b"abc".hex(), "raw_output_truncated": False}


def test_clack_accepts_valid_params() -> None:
    ClackProvider().validate_params({})
    ClackProvider().validate_params(
        {
            "binary": "/usr/local/bin/clack",
            "wpm": 120,
            "jitter": 0,
            "error_rate": 0.1,
            "correction_rate": 1,
            "layout": "dvorak",
            "seed": 42,
            "no_fatigue": False,
            "max_pause_ms": 1000,
            "thinking_pause_prob": 0.5,
            "code_mode": True,
            "startup_timeout_s": 1.5,
            "char_timeout_s": 5,
        }
    )


# ---------------------------------------------------------------------------
# human_move_mouse execution (fake upstream package)
# ---------------------------------------------------------------------------
def test_human_move_mouse_absolute_move(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    model = tmp_path / "mouse_model.pkl"
    model.write_bytes(b"fake-model")
    calls: dict[str, Any] = {}

    def fake_generate(**kwargs: Any) -> tuple[list[list[float]], list[float]]:
        calls.update(kwargs)
        return ([[10.0, 20.0], [50.0, 50.0], [90.0, 90.0]], [0.0, 0.0, 0.0])

    install_fake_humanmouse(monkeypatch, model_path=model, generate=fake_generate)
    ctx, mouse, _ = make_ctx(position=(5.0, 6.0))
    outcome = HumanMoveMouseProvider().execute(
        OperationRequest(operation=Operation.MOUSE_MOVE, x=90, y=90),
        {"num_points": 3, "jitter_amplitude": 0.0, "speed_factor": 2.0, "seed": 7},
        ctx,
    )

    assert mouse.moves == [(10.0, 20.0, 0.0), (50.0, 50.0, 0.0), (90.0, 90.0, 0.0)]
    assert calls == {
        "model_path": str(model),
        "start_point": (5.0, 6.0),
        "end_point": (90.0, 90.0),
        "num_points": 3,
        "jitter_amplitude": 0.0,
        "seed": 7,
    }
    assert outcome.provider == "human_move_mouse"
    assert outcome.engine and "humanmouse" in outcome.engine
    assert outcome.details["points_moved"] == 3
    assert outcome.details["path_length_px"] > 0


def test_human_move_mouse_relative_move(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    model = tmp_path / "mouse_model.pkl"
    model.write_bytes(b"fake-model")
    calls: dict[str, Any] = {}

    def fake_generate(**kwargs: Any) -> tuple[list[list[float]], list[float]]:
        calls.update(kwargs)
        return ([[100.0, 100.0], [110.0, 80.0]], [0.0, 0.0])

    install_fake_humanmouse(monkeypatch, model_path=model, generate=fake_generate)
    ctx, _, _ = make_ctx(position=(100.0, 100.0))
    HumanMoveMouseProvider().execute(
        OperationRequest(operation=Operation.MOUSE_MOVE_RELATIVE, dx=10, dy=-20),
        {},
        ctx,
    )
    assert calls["start_point"] == (100.0, 100.0)
    assert calls["end_point"] == (110.0, 80.0)


def test_human_move_mouse_aligns_exact_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = tmp_path / "mouse_model.pkl"
    model.write_bytes(b"fake-model")

    def fake_generate(**_: Any) -> tuple[list[list[float]], list[float]]:
        return ([[10.0, 10.0], [99.0, 99.0]], [0.0, 0.0])

    install_fake_humanmouse(monkeypatch, model_path=model, generate=fake_generate)
    ctx, mouse, _ = make_ctx(position=(5.0, 6.0))
    outcome = HumanMoveMouseProvider().execute(
        OperationRequest(operation=Operation.MOUSE_MOVE, x=100, y=100),
        {"seed": 1},
        ctx,
    )
    assert mouse.moves[-1] == (100.0, 100.0, 0.0)
    assert outcome.details["final_position"] == [100.0, 100.0]
    assert outcome.details["points_moved"] == 3


def test_human_move_mouse_generation_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    model = tmp_path / "mouse_model.pkl"
    model.write_bytes(b"fake-model")

    def boom(**_: Any) -> Any:
        raise RuntimeError("sklearn model version mismatch")

    install_fake_humanmouse(monkeypatch, model_path=model, generate=boom)
    ctx, _, _ = make_ctx()
    with pytest.raises(StrategyExecutionFailed, match="generation failed"):
        HumanMoveMouseProvider().execute(
            OperationRequest(operation=Operation.MOUSE_MOVE, x=1, y=2), {}, ctx
        )


def test_human_move_mouse_execute_without_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_module(monkeypatch, "humanmouse")
    ctx, _, _ = make_ctx()
    with pytest.raises(StrategyExecutionFailed, match="unavailable"):
        HumanMoveMouseProvider().execute(
            OperationRequest(operation=Operation.MOUSE_MOVE, x=1, y=2), {}, ctx
        )


def test_human_move_mouse_rejects_unsupported_operation() -> None:
    ctx, _, _ = make_ctx()
    with pytest.raises(StrategyNotSupported):
        HumanMoveMouseProvider().execute(
            OperationRequest(operation=Operation.MOUSE_CLICK_LEFT, x=1, y=2), {}, ctx
        )


# ---------------------------------------------------------------------------
# windmouse execution (fake upstream package)
# ---------------------------------------------------------------------------
def test_windmouse_move_uses_driver_step_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    def fake_wind_mouse(*args: Any, **kwargs: Any) -> Any:
        calls["args"] = args
        calls["kwargs"] = kwargs
        yield from [(10, 10), (20, 20), (30, 30)]

    install_fake_windmouse(monkeypatch, fake_wind_mouse)
    ctx, mouse, _ = make_ctx(position=(0.0, 0.0))
    outcome = WindMouseProvider().execute(
        OperationRequest(operation=Operation.MOUSE_MOVE, x=30, y=30),
        {"step_duration_s": 0.05, "gravity": 9, "wind": 3, "max_step": 15, "damped_distance": 12},
        ctx,
    )

    assert mouse.moves == [(10.0, 10.0, 0.05), (20.0, 20.0, 0.05), (30.0, 30.0, 0.05)]
    assert calls["args"] == (0, 0, 30, 30)
    assert calls["kwargs"]["gravity_magnitude"] == 9.0
    assert calls["kwargs"]["wind_magnitude"] == 3.0
    assert outcome.provider == "windmouse"
    assert outcome.details["points_yielded"] == 3
    assert outcome.details["license"] == "GPL-3.0-only"


def test_windmouse_relative_move(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    def fake_wind_mouse(*args: Any, **kwargs: Any) -> Any:
        calls["args"] = args
        yield (15, 25)

    install_fake_windmouse(monkeypatch, fake_wind_mouse)
    ctx, _, _ = make_ctx(position=(10.0, 20.0))
    WindMouseProvider().execute(
        OperationRequest(operation=Operation.MOUSE_MOVE_RELATIVE, dx=5, dy=5), {}, ctx
    )
    assert calls["args"] == (10, 20, 15, 25)


def test_windmouse_applies_numpy_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[int] = []

    def fake_wind_mouse(*_: Any, **__: Any) -> Any:
        yield (1, 1)

    fake_numpy = types.ModuleType("numpy")
    fake_numpy.random = types.SimpleNamespace(  # type: ignore[attr-defined]
        seed=lambda value: recorded.append(int(value))
    )
    install_fake_windmouse(monkeypatch, fake_wind_mouse)
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)
    ctx, _, _ = make_ctx()
    WindMouseProvider().execute(
        OperationRequest(operation=Operation.MOUSE_MOVE, x=1, y=1), {"seed": 42}, ctx
    )
    assert recorded == [42]


def test_windmouse_aligns_exact_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_wind_mouse(*args: Any, **kwargs: Any) -> Any:
        yield from [(10, 10), (29, 29)]

    install_fake_windmouse(monkeypatch, fake_wind_mouse)
    ctx, mouse, _ = make_ctx(position=(0.0, 0.0))
    outcome = WindMouseProvider().execute(
        OperationRequest(operation=Operation.MOUSE_MOVE, x=30, y=30),
        {"step_duration_s": 0.05},
        ctx,
    )
    assert mouse.moves[-1] == (30.0, 30.0, 0.05)
    assert outcome.details["final_position"] == [30.0, 30.0]


def test_windmouse_generation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("bad parameters")

    install_fake_windmouse(monkeypatch, boom)
    ctx, _, _ = make_ctx()
    with pytest.raises(StrategyExecutionFailed, match="execution failed"):
        WindMouseProvider().execute(
            OperationRequest(operation=Operation.MOUSE_MOVE, x=1, y=1), {}, ctx
        )


def test_windmouse_execute_without_package(monkeypatch: pytest.MonkeyPatch) -> None:
    hide_module(monkeypatch, "windmouse")
    ctx, _, _ = make_ctx()
    with pytest.raises(StrategyExecutionFailed, match="unavailable"):
        WindMouseProvider().execute(
            OperationRequest(operation=Operation.MOUSE_MOVE, x=1, y=1), {}, ctx
        )


# ---------------------------------------------------------------------------
# clack byte-stream translation
# ---------------------------------------------------------------------------
def apply_actions(actions: list[tuple[str, str]], typed: list[str]) -> None:
    for kind, value in actions:
        if kind == "char":
            typed.append(value)
        elif value == "backspace":
            if typed:
                typed.pop()
        elif value == "enter":
            typed.append("\n")
        elif value == "tab":
            typed.append("\t")
        else:  # pragma: no cover - no other keys expected
            raise AssertionError(f"unexpected key {value}")


def translate_stream(data: bytes) -> tuple[str, _ByteStreamTranslator]:
    translator = _ByteStreamTranslator()
    typed: list[str] = []
    for byte in data:
        apply_actions(translator.feed(byte), typed)
    translator.flush()
    return "".join(typed), translator


def test_translator_typo_correction_stream_from_real_clack_output() -> None:
    # Captured verbatim from: printf 'the quick fox' | clack --seed 42
    #   --error-rate 1.0 --correction-rate 1.0 --wpm 80
    stream = b"thw \x08\x08e uq\x08\x08quiv\x08ck fox"
    final_text, translator = translate_stream(stream)
    assert final_text == "the quick fox"
    assert translator.backspaces == 5
    assert translator.chars_typed == 18
    assert translator.enters == 0


def test_translator_newline_and_tab() -> None:
    final_text, translator = translate_stream(b"ab\ncd\tef")
    assert final_text == "ab\ncd\tef"
    assert translator.enters == 1
    assert translator.tabs == 1


def test_translator_skips_other_control_bytes() -> None:
    final_text, translator = translate_stream(b"a\x01b\x07c\x7fd")
    assert final_text == "abcd"
    assert translator.skipped_control_bytes == [0x01, 0x07, 0x7F]
    assert translator.chars_typed == 4


def test_translator_decodes_utf8_and_skips_invalid_bytes() -> None:
    final_text, translator = translate_stream("héllo".encode() + b"\xff")
    assert final_text == "héllo"
    assert translator.chars_typed == 5
    assert translator.invalid_utf8_bytes == 1


def test_clack_build_command_flags() -> None:
    command = _build_command(
        "/usr/local/bin/clack",
        {
            "wpm": 80,
            "jitter": 0.15,
            "error_rate": 0.3,
            "correction_rate": 1.0,
            "layout": "qwerty",
            "seed": 42,
            "no_fatigue": True,
            "max_pause_ms": 5000,
            "thinking_pause_prob": 0.0,
            "code_mode": True,
        },
    )
    assert command[0] == "/usr/local/bin/clack"
    assert command[command.index("--error-rate") + 1] == "0.3"
    assert command[command.index("--correction-rate") + 1] == "1"
    assert command[command.index("--seed") + 1] == "42"
    assert command[command.index("--layout") + 1] == "qwerty"
    assert "--no-fatigue" in command
    assert "--code-mode" in command
    assert "--no-errors" not in command


def test_clack_build_command_no_errors_by_default() -> None:
    command = _build_command(
        "/usr/local/bin/clack", clack_module._settings({"no_fatigue": False, "seed": None})
    )
    assert "--no-errors" in command
    assert "--error-rate" not in command
    assert "--seed" not in command
    assert "--no-fatigue" not in command
    assert "--code-mode" not in command


# ---------------------------------------------------------------------------
# clack execution with a faked subprocess
# ---------------------------------------------------------------------------
class FakeStdin:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> int:
        self.data.extend(data)
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    """Minimal Popen double backed by a real OS pipe (select/os.read work)."""

    def __init__(
        self,
        stdout_bytes: bytes = b"",
        *,
        returncode: int = 0,
        stderr_bytes: bytes = b"",
        keep_stdout_open: bool = False,
    ) -> None:
        read_fd, write_fd = os.pipe()
        if stdout_bytes:
            os.write(write_fd, stdout_bytes)
        if not keep_stdout_open:
            os.close(write_fd)
            write_fd = -1
        self._write_fd = write_fd
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.stdin: FakeStdin | None = FakeStdin()
        self.stderr: io.BytesIO | None = io.BytesIO(stderr_bytes)
        self._returncode = returncode
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self._returncode
        self._close_stdout_writer()
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._close_stdout_writer()

    def kill(self) -> None:
        self.terminate()

    def _close_stdout_writer(self) -> None:
        if self._write_fd >= 0:
            os.close(self._write_fd)
            self._write_fd = -1


def install_fake_popen(monkeypatch: pytest.MonkeyPatch, process: FakeProcess) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(clack_module.subprocess, "Popen", fake_popen)
    return captured


def test_clack_execute_types_and_corrects_typos(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(b"thw \x08\x08e uq\x08\x08quiv\x08ck fox")
    captured = install_fake_popen(monkeypatch, process)
    ctx, _, keyboard = make_ctx()
    outcome = ClackProvider().execute(
        OperationRequest(operation=Operation.KEYBOARD_TYPE_TEXT, text="the quick fox"),
        {"error_rate": 0.3, "correction_rate": 1.0, "seed": 42, "wpm": 80},
        ctx,
    )

    assert reconstruct_text(keyboard.events) == "the quick fox"
    assert [key for direction, key in keyboard.keys if direction == "down"] == ["backspace"] * 5
    assert process.stdin is not None and process.stdin.closed is True
    assert bytes(process.stdin.data) == b"the quick fox"
    assert captured["command"][0] == str(clack_module._vendored_binary().resolve())
    assert "--error-rate" in captured["command"]
    assert captured["kwargs"]["stdout"] == subprocess.PIPE
    assert outcome.provider == "clack"
    assert outcome.engine == "clack-cli"
    assert outcome.details["chars_typed"] == 18
    assert outcome.details["backspaces"] == 5
    assert outcome.details["output_bytes"] == len(b"thw \x08\x08e uq\x08\x08quiv\x08ck fox")
    assert outcome.details["raw_output_truncated"] is False
    assert outcome.details["exit_code"] == 0


def test_clack_execute_rejects_non_ascii_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_popen(*_: Any, **__: Any) -> Any:  # pragma: no cover - must not be called
        raise AssertionError("clack must not be spawned for non-ASCII text")

    monkeypatch.setattr(clack_module.subprocess, "Popen", fail_popen)
    ctx, _, _ = make_ctx()
    with pytest.raises(InvalidArgument, match="ASCII"):
        ClackProvider().execute(
            OperationRequest(operation=Operation.KEYBOARD_TYPE_TEXT, text="中文"),
            {},
            ctx,
        )


def test_clack_execute_reports_stderr_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(b"", returncode=1, stderr_bytes=b"Invalid layout 'nonsense'")
    install_fake_popen(monkeypatch, process)
    ctx, _, _ = make_ctx()
    with pytest.raises(StrategyExecutionFailed, match="exit"):
        ClackProvider().execute(
            OperationRequest(operation=Operation.KEYBOARD_TYPE_TEXT, text="abc"),
            {"layout": "qwerty"},
            ctx,
        )


def test_clack_execute_startup_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(keep_stdout_open=True)
    install_fake_popen(monkeypatch, process)
    ctx, _, _ = make_ctx()
    with pytest.raises(StrategyExecutionFailed, match="timed out"):
        ClackProvider().execute(
            OperationRequest(operation=Operation.KEYBOARD_TYPE_TEXT, text="abc"),
            {"startup_timeout_s": 0.1, "char_timeout_s": 0.1},
            ctx,
        )
    assert process.terminated is True


def test_clack_execute_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(keep_stdout_open=True)
    install_fake_popen(monkeypatch, process)
    cancel = threading.Event()
    cancel.set()
    ctx, _, _ = make_ctx(cancel=cancel)
    with pytest.raises(TaskCancelled):
        ClackProvider().execute(
            OperationRequest(operation=Operation.KEYBOARD_TYPE_TEXT, text="abc"),
            {"startup_timeout_s": 30, "char_timeout_s": 30},
            ctx,
        )
    assert process.terminated is True


def test_clack_execute_rejects_unsupported_operation() -> None:
    ctx, _, _ = make_ctx()
    with pytest.raises(StrategyNotSupported):
        ClackProvider().execute(
            OperationRequest(operation=Operation.MOUSE_MOVE, x=1, y=1), {}, ctx
        )


def test_clack_execute_empty_text_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_popen(*_: Any, **__: Any) -> Any:  # pragma: no cover - must not be called
        raise AssertionError("clack must not be spawned for empty text")

    monkeypatch.setattr(clack_module.subprocess, "Popen", fail_popen)
    ctx, _, keyboard = make_ctx()
    outcome = ClackProvider().execute(
        OperationRequest(operation=Operation.KEYBOARD_TYPE_TEXT, text=""), {}, ctx
    )
    assert keyboard.typed == []
    assert outcome.details["chars_typed"] == 0
