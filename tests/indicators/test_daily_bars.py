"""``complete_utc_days``: hand-computed 4h and 1h aggregates, incomplete days, causality."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tbot.indicators import complete_utc_days

H4 = 4 * 3600
D0 = datetime(2024, 3, 1, tzinfo=UTC)


def four_hour_bars(
    first_open: datetime, n: int
) -> tuple[list[datetime], list[float], list[float], list[float], list[float]]:
    """``n`` 4h bars from ``first_open``; bar k has open 100 + k, high 110 + k, low 90 + k, close 101 + k."""
    ts = [first_open + timedelta(hours=4 * (k + 1)) for k in range(n)]
    return (
        ts,
        [100.0 + k for k in range(n)],
        [110.0 + k for k in range(n)],
        [90.0 + k for k in range(n)],
        [101.0 + k for k in range(n)],
    )


def test_two_complete_days_of_4h_bars_are_aggregated() -> None:
    days = complete_utc_days(*four_hour_bars(D0, 12), bar_seconds=H4)
    assert days.close_ts == (D0 + timedelta(days=1), D0 + timedelta(days=2))
    # day 0 = bars 0..5, day 1 = bars 6..11
    assert days.open.tolist() == [100.0, 106.0]
    assert days.high.tolist() == [115.0, 121.0]  # max high of the day = the last bar's 110 + k
    assert days.low.tolist() == [90.0, 96.0]  # min low = the first bar's 90 + k
    assert days.close.tolist() == [106.0, 112.0]  # close of the last bar of the day
    assert len(days) == 2


def test_the_running_day_is_excluded_until_its_last_bar_has_closed() -> None:
    assert len(complete_utc_days(*four_hour_bars(D0, 11), bar_seconds=H4)) == 1  # last bar of day 1 missing
    assert len(complete_utc_days(*four_hour_bars(D0, 12), bar_seconds=H4)) == 2
    assert len(complete_utc_days(*four_hour_bars(D0, 13), bar_seconds=H4)) == 2  # day 2 has one bar only


def test_a_partial_first_day_is_excluded() -> None:
    start = D0 + timedelta(hours=8)  # first bar opens at 08:00
    days = complete_utc_days(*four_hour_bars(start, 4 + 6), bar_seconds=H4)  # 4 bars finish day 0, then day 1
    assert days.close_ts == (D0 + timedelta(days=2),)
    assert days.open.tolist() == [104.0]


def test_a_day_with_a_missing_bar_is_excluded() -> None:
    ts, o, h, lo, c = four_hour_bars(D0, 18)
    drop = 8  # a bar of day 1
    keep = [k for k in range(18) if k != drop]
    days = complete_utc_days(
        [ts[k] for k in keep],
        [o[k] for k in keep],
        [h[k] for k in keep],
        [lo[k] for k in keep],
        [c[k] for k in keep],
        bar_seconds=H4,
    )
    assert days.close_ts == (D0 + timedelta(days=1), D0 + timedelta(days=3))  # day 0 and day 2


def test_hourly_bars_and_daily_bars_themselves() -> None:
    ts = [D0 + timedelta(hours=k + 1) for k in range(24)]
    values = [float(k) for k in range(24)]
    days = complete_utc_days(
        ts, values, [v + 1 for v in values], [v - 1 for v in values], values, bar_seconds=3600
    )
    assert (days.open[0], days.high[0], days.low[0], days.close[0]) == (0.0, 24.0, -1.0, 23.0)
    daily = complete_utc_days([D0 + timedelta(days=1)], [5.0], [8.0], [4.0], [7.0], bar_seconds=86400)
    assert (daily.open[0], daily.high[0], daily.low[0], daily.close[0]) == (5.0, 8.0, 4.0, 7.0)


def test_aggregating_a_prefix_gives_a_prefix_of_the_days() -> None:
    ts, o, h, lo, c = four_hour_bars(D0, 30)
    full = complete_utc_days(ts, o, h, lo, c, bar_seconds=H4)
    for n in range(1, 31):
        part = complete_utc_days(ts[:n], o[:n], h[:n], lo[:n], c[:n], bar_seconds=H4)
        k = len(part)
        assert part.close_ts == full.close_ts[:k]
        assert part.close.tolist() == full.close[:k].tolist() and part.high.tolist() == full.high[:k].tolist()
        assert k == n // 6  # a day appears exactly when its sixth bar has closed


def test_empty_input_and_bad_arguments() -> None:
    assert len(complete_utc_days([], [], [], [], [], bar_seconds=H4)) == 0
    one = four_hour_bars(D0, 6)
    with pytest.raises(ValueError, match="divide a day"):
        complete_utc_days(*one, bar_seconds=7 * 3600)
    with pytest.raises(ValueError, match="same length"):
        complete_utc_days(one[0], one[1][:-1], one[2], one[3], one[4], bar_seconds=H4)
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        complete_utc_days([t.replace(tzinfo=None) for t in one[0]], *one[1:], bar_seconds=H4)
    with pytest.raises(ValueError, match="strictly increase"):
        complete_utc_days(one[0][::-1], *one[1:], bar_seconds=H4)
