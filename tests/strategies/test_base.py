from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tbot.core.types import BarWindow, TargetIntent
from tbot.strategies.base import (
    StrategyRegistrationError,
    StrategyRegistry,
    is_valid_stop,
    validate_strategy,
)

_TS = datetime(2024, 1, 1, tzinfo=UTC)


class _Complete:
    id = "complete"
    warmup_bars = 3

    def on_bar(self, window: BarWindow) -> TargetIntent:  # noqa: ARG002
        return TargetIntent(strategy_id=self.id, ts=_TS, target_weight=0.0)

    def stop_price(self, window: BarWindow, entry_price: Decimal) -> Decimal:  # noqa: ARG002
        return entry_price * Decimal("0.9")


class _NoStop:
    id = "no_stop"
    warmup_bars = 3

    def on_bar(self, window: BarWindow) -> TargetIntent:  # noqa: ARG002
        return TargetIntent(strategy_id=self.id, ts=_TS, target_weight=0.0)


class _BadStopSignature:
    id = "bad_sig"
    warmup_bars = 3

    def on_bar(self, window: BarWindow) -> TargetIntent:  # noqa: ARG002
        return TargetIntent(strategy_id=self.id, ts=_TS, target_weight=0.0)

    def stop_price(self, window: BarWindow) -> Decimal:  # noqa: ARG002
        return Decimal(1)


def test_complete_strategy_registers() -> None:
    registry = StrategyRegistry()
    registry.register(_Complete())
    assert registry.ids() == ("complete",)
    assert len(registry) == 1
    assert registry.get("complete").id == "complete"


def test_strategy_without_stop_price_fails_registration() -> None:
    with pytest.raises(StrategyRegistrationError, match="stop_price"):
        StrategyRegistry().register(_NoStop())


def test_stop_price_with_wrong_signature_fails_registration() -> None:
    with pytest.raises(StrategyRegistrationError, match="stop_price"):
        validate_strategy(_BadStopSignature())


@pytest.mark.parametrize(
    ("attr", "value"),
    [("id", ""), ("id", 7), ("warmup_bars", 0), ("warmup_bars", True), ("warmup_bars", "3")],
)
def test_bad_id_or_warmup_fails_registration(attr: str, value: object) -> None:
    strategy = _Complete()
    setattr(strategy, attr, value)
    with pytest.raises(StrategyRegistrationError):
        validate_strategy(strategy)


def test_duplicate_id_is_refused() -> None:
    registry = StrategyRegistry()
    registry.register(_Complete())
    with pytest.raises(StrategyRegistrationError, match="already registered"):
        registry.register(_Complete())


@pytest.mark.parametrize(
    ("stop", "valid"),
    [
        (Decimal("90"), True),
        (Decimal("100"), False),  # not strictly below the price
        (Decimal("101"), False),
        (Decimal("0"), False),
        (Decimal("-1"), False),
        (Decimal("NaN"), False),
        (Decimal("Infinity"), False),
        (90.0, False),  # a float is not an exact price
        (None, False),
    ],
)
def test_is_valid_stop(stop: object, valid: bool) -> None:
    assert is_valid_stop(stop, Decimal("100")) is valid


class _RequiredKeywordOnly(_Complete):
    id = "kw_only"

    def stop_price(self, window: BarWindow, entry_price: Decimal, *, mult: Decimal) -> Decimal:  # type: ignore[override]  # noqa: ARG002
        return entry_price * mult


class _OptionalExtraPositional(_Complete):
    id = "optional_extra"

    def stop_price(
        self, window: BarWindow, entry_price: Decimal, buffer: Decimal | None = None  # noqa: ARG002
    ) -> Decimal:
        return entry_price * Decimal("0.9")


class _VarArgs(_Complete):
    id = "varargs"

    def stop_price(self, *args: object) -> Decimal:  # noqa: ARG002
        return Decimal(1)


def test_required_keyword_only_argument_fails_registration() -> None:
    with pytest.raises(StrategyRegistrationError, match="keyword-only"):
        validate_strategy(_RequiredKeywordOnly())


@pytest.mark.parametrize("strategy", [_OptionalExtraPositional(), _VarArgs()])
def test_callable_with_two_positional_arguments_registers(strategy: object) -> None:
    assert validate_strategy(strategy) is strategy


@pytest.mark.parametrize("reference", [Decimal("NaN"), Decimal("0"), Decimal("-5"), 100.0, None])
def test_is_valid_stop_rejects_an_invalid_reference_price_without_raising(reference: object) -> None:
    assert is_valid_stop(Decimal("90"), reference) is False
