"""SL one tick beyond Origin, single 1R TP, E2 midpoint; no TP2/partial/trailing field."""

from __future__ import annotations

import dataclasses
import re
from decimal import Decimal

import pytest

from sp2l.core.types import Side
from sp2l.strategy.levels import SetupLevels, compute_levels, e2_price
from tests.conftest import candle

TICK = Decimal("0.1")


def test_long_levels():
    origin = candle(0, "100", "101", "99.5", "100.8")
    last = candle(3, "103", "104", "102.5", "103.9")
    lv = compute_levels(Side.LONG, origin, last, TICK)
    assert lv.e1 == Decimal("102.5")
    assert lv.sl == Decimal("99.4")
    assert lv.r == Decimal("3.1")
    assert lv.tp == Decimal("105.6")
    assert lv.e2_mid == Decimal("100.95")
    assert lv.e2 == Decimal("100.9")  # B23: rounded toward SL (down for Long)
    assert lv.d2 == Decimal("1.5")  # risk from the rounded E2, not 0.5R = 1.55


def test_short_levels():
    origin = candle(0, "100", "100.5", "99", "99.2")
    last = candle(3, "97", "97.5", "96", "96.2")
    lv = compute_levels(Side.SHORT, origin, last, TICK)
    assert (lv.e1, lv.sl, lv.r, lv.tp) == (
        Decimal("97.5"),
        Decimal("100.6"),
        Decimal("3.1"),
        Decimal("94.4"),
    )


def test_e2_rounds_toward_sl():
    assert e2_price(Side.LONG, Decimal("102.5"), Decimal("99.5"), TICK) == Decimal("101.0")
    assert e2_price(Side.LONG, Decimal("102.5"), Decimal("99.4"), TICK) == Decimal("100.9")
    assert e2_price(Side.SHORT, Decimal("97.5"), Decimal("100.6"), TICK) == Decimal("99.1")


def test_off_tick_inputs_rejected():
    origin = candle(0, "100", "101", "99.55", "100.8")
    last = candle(3, "103", "104", "102.5", "103.9")
    with pytest.raises(ValueError, match="tick grid"):
        compute_levels(Side.LONG, origin, last, TICK)


def test_no_second_target_field_exists():
    forbidden = re.compile(r"tp2|tp_2|partial|trail|abcd|ab_cd|target2", re.I)
    names = [f.name for f in dataclasses.fields(SetupLevels)]
    assert [n for n in names if forbidden.search(n)] == []
    assert [n for n in names if n.startswith("tp")] == ["tp"]
