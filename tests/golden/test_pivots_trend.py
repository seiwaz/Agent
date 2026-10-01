"""AT: pivot equality rejected; pivot unavailable until two right bars close (CTX-03..05)."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from sp2l.indicators.pivots import PivotKind, confirmed_pivots, first_break_index
from sp2l.indicators.trend import Trend, classify_trend
from tests.conftest import candle, hl_series


def test_strict_swing_high_and_low():
    s = hl_series([("10", "5"), ("11", "6"), ("15", "7"), ("12", "6.5"), ("11", "6")])
    (p,) = confirmed_pivots(s)
    assert (p.index, p.kind, p.price, p.confirmed_index) == (2, PivotKind.HIGH, D("15"), 4)


@pytest.mark.parametrize("neighbour", [0, 1, 3, 4])
def test_any_equal_neighbour_is_not_a_pivot(neighbour):
    rows = [("10", "5"), ("11", "6"), ("15", "7"), ("12", "6.5"), ("11", "6")]
    rows[neighbour] = ("15", rows[neighbour][1])
    assert [p for p in confirmed_pivots(hl_series(rows)) if p.kind is PivotKind.HIGH] == []


def test_pivot_not_confirmed_before_second_right_bar():
    s = hl_series([("10", "5"), ("11", "6"), ("15", "7"), ("12", "6.5"), ("11", "6")])
    assert confirmed_pivots(s, through_index=3) == []
    assert len(confirmed_pivots(s, through_index=4)) == 1


def test_outside_bar_reported_as_dual():
    s = hl_series([("10", "5"), ("11", "6"), ("15", "1"), ("12", "6.5"), ("11", "6")])
    (p,) = confirmed_pivots(s)
    assert p.kind is PivotKind.DUAL
    with pytest.raises(ValueError, match="B11"):
        _ = p.price


def test_break_requires_strict_close_after_confirmation():
    rows = [("10", "5"), ("11", "6"), ("15", "7"), ("12", "6.5"), ("11", "6")]
    s = hl_series(rows)
    s.append(candle(5, "14", "16", "13", "15", step_min=5))  # close == level: not broken
    s.append(candle(6, "15", "17", "14", "15.01", step_min=5))  # close > level: broken
    (p,) = confirmed_pivots(s[:5])
    assert first_break_index(p, s) == 6


@pytest.mark.parametrize(
    ("highs", "lows", "expected"),
    [
        (["10", "11"], ["5", "6"], Trend.BULL),
        (["11", "10"], ["6", "5"], Trend.BEAR),
        (["10", "10"], ["5", "6"], Trend.NEUTRAL),
        (["10", "11"], ["6", "5"], Trend.NEUTRAL),
        (["10"], ["5", "6"], Trend.NEUTRAL_INSUFFICIENT_STRUCTURE),
    ],
)
def test_trend(highs, lows, expected):
    assert classify_trend([D(h) for h in highs], [D(x) for x in lows]) is expected
