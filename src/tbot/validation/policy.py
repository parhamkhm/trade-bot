"""Open SPEC readings, each behind ONE named switch (the decided rule is a one-line change).

Three questions are open and wait for Parham's decision. The harness keeps the current behaviour for each
and does not choose; the value below is what the code does today, and every report prints it.

``PENDING_ORDER_UNDER_DELAY`` (reading 6)
    A 4h point's order placed at a decision close fills 6 h later (SPEC 7.2 E, the gating mode), i.e. after
    one more 4h close. The engine skips a decision while an order is still pending, so the 02:00-style
    decision at the next 4h close is NOT taken: ``"skip"`` (current). The alternative is
    ``"cancel_and_replace"`` (cancel the pending order and place the new one). That needs an engine change,
    which is outside this harness: ``make_config`` raises ``NotImplementedError`` for it instead of
    silently running something else.

``S1_STOP_SOURCE`` (reading 7)
    The stop of the S1 blend (SPEC 7.2 C: ``P_ref - 3 x ATR20``) comes from ``"first_scheduled_member"``
    (current: the first member that has a scheduled point), ``"tightest_member"`` (the highest member stop
    for a long) or ``"widest_member"`` (the lowest). For the pre-registered members all three agree only if
    their ATR parameters are equal, which is why this is flagged.

``PBO_ZERO_LOGIT_IS_OVERFIT`` (PBO logit convention)
    PBO is the share of CSCV combinations whose logit is ``<= 0`` (``True``, current: the in-sample winner is
    at or below the out-of-sample median) rather than ``< 0`` (``False``: strictly below the median). They
    differ only on combinations where the winner sits exactly at the median rank.
"""

from __future__ import annotations

from typing import Final, Literal

__all__ = [
    "PBO_ZERO_LOGIT_IS_OVERFIT",
    "PENDING_ORDER_UNDER_DELAY",
    "POLICY_SUMMARY",
    "S1_STOP_SOURCE",
    "PendingPolicy",
    "StopSource",
]

PendingPolicy = Literal["skip", "cancel_and_replace"]
StopSource = Literal["first_scheduled_member", "tightest_member", "widest_member"]

PENDING_ORDER_UNDER_DELAY: Final[PendingPolicy] = "skip"
S1_STOP_SOURCE: Final[StopSource] = "first_scheduled_member"
PBO_ZERO_LOGIT_IS_OVERFIT: Final[bool] = True

POLICY_SUMMARY: Final[str] = (
    f"pending 4h order under the delay: {PENDING_ORDER_UNDER_DELAY}; "
    f"S1 stop source: {S1_STOP_SOURCE}; "
    f"PBO counts logit == 0 as overfit: {PBO_ZERO_LOGIT_IS_OVERFIT}"
)
