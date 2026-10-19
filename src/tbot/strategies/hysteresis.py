"""Own long/flat signal state of C4 and C5 (SPEC section 7.2 C, [OPERATIONAL] D-097).

For C4 and C5 the strategy keeps its own signal state, replayed from the trial's first tradable close
(``index + 1 >= warmup_bars``), where it STARTS FLAT -- nothing is carried in from earlier bars:

* flat -> long at a close where the entry condition holds;
* long -> flat at a close where the exit condition holds;
* otherwise unchanged.

The position machine of ``ReplayStrategy`` reads this state as "the strategy wants to be long". "Its own entry
condition goes false and then true again" (the fresh-entry rule after a stop-out) refers to this signal state:
after a stop-out the position stays STOPPED until the exit condition fires (the signal state goes flat) and a
later close then satisfies the entry condition. The entry condition is never evaluated while the state is
long, and the exit condition never while it is flat.
"""

from __future__ import annotations

from abc import abstractmethod

from tbot.core.types import BarWindow
from tbot.strategies.replay import ReplayStrategy

__all__ = ["HysteresisStrategy"]


class HysteresisStrategy(ReplayStrategy):
    """``ReplayStrategy`` whose signal is a flat/long state machine with separate entry and exit rules."""

    def _reset_signal(self) -> None:
        self._long = False
        self._reset_indicators()

    # ---- subclass hooks (called with the index of the bar just appended; may read indices <= i only) ----
    def _reset_indicators(self) -> None:
        """Clear any per-bar indicator cache (called with every replay reset)."""

    def _observe(self, i: int) -> None:
        """Called once per bar, in order, for EVERY bar of the replay (also before the first tradable one)."""

    @abstractmethod
    def _entry_at(self, i: int) -> bool: ...

    @abstractmethod
    def _exit_at(self, i: int) -> bool: ...

    # ---- state machine -------------------------------------------------------------------------------
    def _signal_at(self, i: int) -> bool:
        self._observe(i)
        if i + 1 < self.warmup_bars:  # before the first tradable close the state is not started: it is flat
            return False
        if self._long:
            if self._exit_at(i):
                self._long = False
        elif self._entry_at(i):
            self._long = True
        return self._long

    def signal_long(self, window: BarWindow) -> bool:
        """The own signal state after the close of the last bar of ``window`` (anchored replay)."""
        self._sync(window)
        return self._long

    def entry_holds(self, window: BarWindow) -> bool:
        """The entry condition at the last close of ``window``, whatever the signal state (for
        tests/audits)."""
        self._sync(window)
        return self._entry_at(len(window) - 1)

    def exit_holds(self, window: BarWindow) -> bool:
        """The exit condition at the last close of ``window``, whatever the signal state."""
        self._sync(window)
        return self._exit_at(len(window) - 1)
