"""Strategy registration and protective-stop validation (SPEC D-050).

Every strategy must implement ``stop_price(window, entry_price)``. A strategy without it cannot be
registered, so it can never reach a backtest, paper or live run. ``is_valid_stop`` is the pure check
the RiskManager (phase 4) applies to every long: a stop must be a finite, positive ``Decimal`` strictly
below the reference price.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from decimal import Decimal

from tbot.core.types import Strategy

__all__ = ["StrategyRegistrationError", "StrategyRegistry", "is_valid_stop", "validate_strategy"]


class StrategyRegistrationError(TypeError):
    """Raised when an object does not satisfy the full ``Strategy`` contract."""


def _require_method(obj: object, name: str, n_params: int) -> None:
    method = getattr(obj, name, None)
    if method is None or not callable(method):
        raise StrategyRegistrationError(
            f"{type(obj).__name__} has no callable {name}() -- every strategy must implement it (D-050)"
        )
    try:
        params = list(inspect.signature(method).parameters.values())
    except (TypeError, ValueError) as exc:  # unverifiable contract: refuse rather than hope
        raise StrategyRegistrationError(
            f"{type(obj).__name__}.{name}() has no inspectable signature"
        ) from exc
    where = f"{type(obj).__name__}.{name}()"
    if any(p.kind is p.KEYWORD_ONLY and p.default is p.empty for p in params):
        raise StrategyRegistrationError(f"{where} must not have required keyword-only arguments")
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    required = sum(1 for p in positional if p.default is p.empty)
    takes_varargs = any(p.kind is p.VAR_POSITIONAL for p in params)
    # Callable with exactly n_params positional arguments: no more required, and room for all of them.
    if required > n_params or (len(positional) < n_params and not takes_varargs):
        raise StrategyRegistrationError(
            f"{where} must accept {n_params} positional argument(s) "
            f"(has {required} required, {len(positional)} total)"
        )


def validate_strategy(obj: object) -> Strategy:
    """Return ``obj`` typed as a ``Strategy`` if it satisfies the whole contract, else raise."""
    strategy_id = getattr(obj, "id", None)
    if not isinstance(strategy_id, str) or not strategy_id:
        raise StrategyRegistrationError(f"{type(obj).__name__}.id must be a non-empty string")
    warmup = getattr(obj, "warmup_bars", None)
    if not isinstance(warmup, int) or isinstance(warmup, bool) or warmup < 1:
        raise StrategyRegistrationError(f"{type(obj).__name__}.warmup_bars must be an int >= 1")
    _require_method(obj, "on_bar", 1)
    _require_method(obj, "stop_price", 2)
    if not isinstance(obj, Strategy):  # runtime_checkable: final structural check
        raise StrategyRegistrationError(f"{type(obj).__name__} does not satisfy the Strategy protocol")
    return obj


def is_valid_stop(stop: object, reference_price: object) -> bool:
    """True iff ``stop`` is a finite, positive ``Decimal`` strictly below ``reference_price``, which must
    itself be a finite, positive ``Decimal`` (never raises; a NaN or float input is simply invalid)."""
    for value in (stop, reference_price):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            return False
    assert isinstance(stop, Decimal) and isinstance(reference_price, Decimal)  # narrowed above
    return stop < reference_price


class StrategyRegistry:
    """Holds the strategies a run may use, keyed by id. Registration enforces the full contract."""

    def __init__(self) -> None:
        self._strategies: dict[str, Strategy] = {}

    def register(self, obj: object) -> Strategy:
        strategy = validate_strategy(obj)
        if strategy.id in self._strategies:
            raise StrategyRegistrationError(f"strategy id {strategy.id!r} is already registered")
        self._strategies[strategy.id] = strategy
        return strategy

    def get(self, strategy_id: str) -> Strategy:
        return self._strategies[strategy_id]

    def ids(self) -> tuple[str, ...]:
        return tuple(self._strategies)

    def __iter__(self) -> Iterator[Strategy]:
        return iter(self._strategies.values())

    def __len__(self) -> int:
        return len(self._strategies)
