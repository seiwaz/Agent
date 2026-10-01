from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from sp2l.core.types import Side
from sp2l.strategy.levels import compute_levels
from sp2l.strategy.pgap import detect_pgap
from tests.conftest import candle

prices = st.integers(min_value=1_000, max_value=100_000)


def _c(i: int, a: int, b: int):
    lo, hi = min(a, b), max(a, b)
    return candle(i, str(lo), str(hi), str(lo), str(hi))


@given(prices, prices, prices, prices, prices, prices)
def test_pgap_is_never_both_and_equality_never_counts(a, b, c, d, e, f):
    left, mid, right = _c(0, a, b), _c(1, c, d), _c(2, e, f)
    side = detect_pgap(left, mid, right)
    if side is Side.LONG:
        assert right.low > left.high
    elif side is Side.SHORT:
        assert right.high < left.low
    else:
        assert right.low <= left.high and right.high >= left.low


@given(st.integers(1, 10_000), st.integers(1, 10_000), st.integers(1, 10_000))
def test_single_tp_is_exactly_one_r(origin_low, gap, width):
    tick = Decimal("0.1")
    origin = _c(0, origin_low, origin_low + width)
    last = _c(1, origin_low + gap, origin_low + gap + width)
    lv = compute_levels(Side.LONG, origin, last, tick)
    assert lv.tp - lv.e1 == lv.e1 - lv.sl == lv.r > 0
