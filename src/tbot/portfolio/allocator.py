"""Allocator: turns the strategy intent(s) into the one intent the RiskManager sees.

v1 is a single-strategy pass-through (CLAUDE.md section 4: "single strategy x m", where the regime
multiplier ``m`` is applied by the caller through ``TargetIntent.scaled``). A static blend arrives with
the S1 allocator in phase 4; the rebalance-band rule of SPEC section 7.1 also belongs there because it
needs the current (drifted) weight, which a stateless ``Strategy`` cannot know.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from tbot.core.types import TargetIntent

__all__ = ["Allocator", "PassThroughAllocator"]


class Allocator(Protocol):
    def allocate(self, intents: Sequence[TargetIntent]) -> TargetIntent: ...


class PassThroughAllocator:
    """Exactly one intent in, the same intent out. Anything else is a wiring error."""

    def allocate(self, intents: Sequence[TargetIntent]) -> TargetIntent:
        if len(intents) != 1:
            raise ValueError(
                f"PassThroughAllocator handles exactly one intent, got {len(intents)}; "
                "multi-strategy blending is not implemented (phase 4)"
            )
        return intents[0]
