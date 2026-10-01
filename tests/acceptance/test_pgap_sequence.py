"""AT: strict P-Gap equality rejected (PG-01..03); directional sequence (SEQ-01..03)."""

from __future__ import annotations

from sp2l.core.types import Side
from sp2l.strategy.pgap import detect_pgap
from sp2l.strategy.sequence import continues
from tests.conftest import candle


def test_bullish_pgap_strict():
    left = candle(0, "100", "105", "99", "104")
    mid = candle(1, "104", "110", "103", "109")
    assert detect_pgap(left, mid, candle(2, "109", "112", "105.01", "111")) is Side.LONG


def test_bullish_pgap_equality_rejected():
    left = candle(0, "100", "105", "99", "104")
    mid = candle(1, "104", "110", "103", "109")
    assert detect_pgap(left, mid, candle(2, "109", "112", "105", "111")) is None


def test_bearish_pgap_strict_and_equality():
    left = candle(0, "100", "101", "95", "96")
    mid = candle(1, "96", "97", "90", "91")
    assert detect_pgap(left, mid, candle(2, "91", "94.99", "88", "89")) is Side.SHORT
    assert detect_pgap(left, mid, candle(2, "91", "95", "88", "89")) is None


def test_pgap_ignores_middle_candle():
    left = candle(0, "100", "105", "99", "104")
    right = candle(2, "109", "112", "106", "111")
    for mid in (candle(1, "104", "130", "98", "99"), candle(1, "104", "106", "104", "105")):
        assert detect_pgap(left, mid, right) is Side.LONG


def test_long_sequence_equal_low_continues_and_colour_ignored():
    prev = candle(0, "100", "105", "99", "104")
    red = candle(1, "104", "106", "99", "100")  # bearish body, equal low
    assert continues(Side.LONG, prev, red)
    assert not continues(Side.LONG, prev, candle(1, "104", "106", "98.99", "105"))


def test_short_sequence_equal_high_continues():
    prev = candle(0, "100", "105", "99", "100")
    green = candle(1, "100", "105", "97", "104")
    assert continues(Side.SHORT, prev, green)
    assert not continues(Side.SHORT, prev, candle(1, "100", "105.01", "97", "98"))
