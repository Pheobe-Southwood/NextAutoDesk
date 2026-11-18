"""Unit tests for strategy provider discovery, configuration merging and resolution."""

from __future__ import annotations

import pytest

from nextautodesk.config import StrategiesSettings
from nextautodesk.errors import StrategyUnavailable
from nextautodesk.models import ProviderAvailability
from nextautodesk.operations import Operation
from nextautodesk.strategies.base import (
    OperationRequest,
    StrategyContext,
    StrategyOutcome,
    StrategyProvider,
)
from nextautodesk.strategies.registry import StrategyRegistry


def _settings() -> StrategiesSettings:
    return StrategiesSettings.model_validate(
        {
            "providers": {
                "fake": {"enabled": True, "params": {"speed": 1.0, "shared": "provider"}},
                "disabled": {"enabled": False, "params": {}},
            },
            "operations": {
                op.value: {
                    "provider": "fake",
                    "params": {"shared": "operation"} if op is Operation.MOUSE_MOVE else {},
                }
                for op in Operation
            },
        }
    )


class FakeProvider(StrategyProvider):
    name = "fake"
    version = "9.9.9"
    description = "test provider"
    supported = frozenset({Operation.MOUSE_MOVE, Operation.KEYBOARD_TYPE_TEXT})

    def __init__(self) -> None:
        self.calls: list[tuple[Operation, dict, object]] = []
        self._available = True

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(
            name=self.name,
            available=self._available,
            version=self.version,
            description=self.description,
            supported_operations=sorted(self.supported, key=lambda op: op.value),
            reason=None if self._available else "missing optional dependency",
        )

    def execute(self, request, params, ctx) -> StrategyOutcome:  # type: ignore[override]
        self.calls.append((request.operation, dict(params), ctx))
        return StrategyOutcome(operation=request.operation, provider=self.name)


class OtherFakeProvider(FakeProvider):
    name = "other"
    supported = frozenset({Operation.MOUSE_MOVE})


class DisabledFakeProvider(FakeProvider):
    name = "disabled"


def _ctx() -> StrategyContext:
    import logging
    import threading

    class _Drivers:
        pass

    return StrategyContext(  # type: ignore[arg-type]
        drivers=_Drivers(), cancel=threading.Event(), logger=logging.getLogger("test")
    )


def test_merge_provider_defaults_with_operation_overrides():
    registry = StrategyRegistry.build(
        _settings(), provider_classes=[FakeProvider, OtherFakeProvider, DisabledFakeProvider]
    )
    resolved = registry.resolve(Operation.MOUSE_MOVE)
    assert resolved.provider.name == "fake"
    assert resolved.params["speed"] == 1.0  # provider default
    assert resolved.params["shared"] == "operation"  # operation override wins


def test_override_provider():
    registry = StrategyRegistry.build(
        _settings(), provider_classes=[FakeProvider, OtherFakeProvider, DisabledFakeProvider]
    )
    resolved = registry.resolve(Operation.MOUSE_MOVE, override="other")
    assert resolved.provider.name == "other"


def test_unavailable_provider_rejected():
    class OffProvider(FakeProvider):
        name = "fake"

        def availability(self):
            availability = super().availability()
            availability.available = False
            availability.reason = "broken"
            return availability

    registry = StrategyRegistry.build(
        _settings(), provider_classes=[OffProvider, OtherFakeProvider, DisabledFakeProvider]
    )
    with pytest.raises(StrategyUnavailable) as excinfo:
        registry.resolve(Operation.MOUSE_MOVE)
    assert "unavailable" in str(excinfo.value)


def test_unsupported_operation_rejected():
    registry = StrategyRegistry.build(
        _settings(), provider_classes=[FakeProvider, OtherFakeProvider, DisabledFakeProvider]
    )
    with pytest.raises(StrategyUnavailable) as excinfo:
        registry.resolve(Operation.MOUSE_DRAG)
    assert "does not support" in str(excinfo.value)


def test_disabled_provider_rejected():
    registry = StrategyRegistry.build(
        _settings(), provider_classes=[FakeProvider, OtherFakeProvider, DisabledFakeProvider]
    )
    with pytest.raises(StrategyUnavailable):
        registry.resolve(Operation.MOUSE_MOVE, override="disabled")


def test_unknown_override_rejected():
    registry = StrategyRegistry.build(
        _settings(), provider_classes=[FakeProvider, OtherFakeProvider, DisabledFakeProvider]
    )
    with pytest.raises(StrategyUnavailable):
        registry.resolve(Operation.MOUSE_MOVE, override="nope")


def test_unknown_configured_provider_fails_build():
    # construct without pydantic validation to exercise the registry's own check
    settings = StrategiesSettings.model_construct(
        providers={},
        operations={
            op.value: type("OpCfg", (), {"provider": "nope", "enabled": True, "params": {}})()
            for op in Operation
        },
    )
    with pytest.raises(ValueError) as excinfo:
        StrategyRegistry.build(settings, provider_classes=[])
    assert "unknown strategy providers" in str(excinfo.value)


def test_operation_views_contains_all_operations():
    registry = StrategyRegistry.build(
        _settings(), provider_classes=[FakeProvider, OtherFakeProvider, DisabledFakeProvider]
    )
    views = registry.operation_views()
    assert set(views) == {op.value for op in Operation}
    mouse_move = views["mouse_move"]
    assert mouse_move.provider == "fake"
    assert mouse_move.available is True
    assert "fake" in mouse_move.candidates
    # second provider supports move only, so it must not be a candidate for typing
    assert views["keyboard_type_text"].candidates == ["fake"]


def test_execute_delegates_to_provider():
    registry = StrategyRegistry.build(
        _settings(), provider_classes=[FakeProvider, OtherFakeProvider, DisabledFakeProvider]
    )
    resolved = registry.resolve(Operation.MOUSE_MOVE)
    request = OperationRequest(operation=Operation.MOUSE_MOVE, x=10, y=20)
    outcome = resolved.provider.execute(request, resolved.params, _ctx())
    assert outcome.provider == "fake"
    assert outcome.operation is Operation.MOUSE_MOVE
    provider = resolved.provider
    assert provider.calls and provider.calls[0][0] is Operation.MOUSE_MOVE  # type: ignore[attr-defined]


class StrictFakeProvider(FakeProvider):
    name = "fake"

    def validate_params(self, params):  # type: ignore[override]
        if "bad" in params:
            raise ValueError("bad is not a valid parameter")


def test_build_rejects_invalid_operation_params() -> None:
    raw = {
        "providers": {"fake": {"enabled": True, "params": {}}},
        "operations": {
            op.value: {
                "provider": "fake",
                "params": {"bad": True} if op is Operation.MOUSE_MOVE else {},
            }
            for op in Operation
        },
    }
    settings = StrategiesSettings.model_validate(raw)
    with pytest.raises(ValueError, match=r"mouse_move.*bad is not a valid parameter"):
        StrategyRegistry.build(settings, provider_classes=[StrictFakeProvider])


def test_build_accepts_valid_operation_params() -> None:
    settings = _settings()
    registry = StrategyRegistry.build(
        settings, provider_classes=[FakeProvider, OtherFakeProvider, DisabledFakeProvider]
    )
    resolved = registry.resolve(Operation.MOUSE_MOVE)
    assert resolved.params["shared"] == "operation"
    assert resolved.params["speed"] == 1.0
