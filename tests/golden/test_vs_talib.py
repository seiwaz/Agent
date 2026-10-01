"""AT: ADX uses Wilder smoothing (CTX-06); ATR14/EMA20 (EXH-01/02) vs TA-Lib reference."""

from __future__ import annotations

import math
from decimal import Decimal

import numpy as np
import pytest
import talib

from sp2l.core.series import DiscontinuousSeriesError
from sp2l.indicators import UNKNOWN
from sp2l.indicators.chop import chop_series
from sp2l.indicators.ema import ema_series, ema_step
from sp2l.indicators.wilder import adx_series, atr_series
from tests.conftest import hl_series, random_walk

REL = 1e-12


def _arrays(cs):
    return tuple(np.array([float(getattr(c, k)) for c in cs]) for k in ("high", "low", "close"))


def _close(ours: Decimal, ref: float) -> bool:
    return abs(float(ours) - ref) <= REL * max(abs(ref), 1.0)


def test_atr_matches_talib_from_first_value(walk600):
    h, lo, c = _arrays(walk600)
    ref = talib.ATR(h, lo, c, 14)
    ours = atr_series(walk600)
    assert ours[13] is None and ours[14] is not None and not math.isnan(ref[14])
    assert all(_close(ours[i], ref[i]) for i in range(14, 600))


def test_ema_matches_talib_and_step_resume_is_identical(walk600):
    closes = [c.close for c in walk600]
    ref = talib.EMA(np.array([float(x) for x in closes]), 20)
    ours = ema_series(closes)
    assert ours[18] is None and ours[19] is not None
    assert all(_close(ours[i], ref[i]) for i in range(19, 600))
    # restart stability: resuming from persisted state reproduces the series exactly
    resumed = ours[299]
    for i in range(300, 600):
        resumed = ema_step(resumed, closes[i])
        assert resumed == ours[i]


def test_adx_matches_talib_bit_for_bit(walk600):
    """B06+: TA-Lib is the canonical ADX; DI and ADX match from the first value."""
    h, lo, c = _arrays(walk600)
    adx_ref, pdi_ref, mdi_ref = (
        talib.ADX(h, lo, c, 14),
        talib.PLUS_DI(h, lo, c, 14),
        talib.MINUS_DI(h, lo, c, 14),
    )
    ours = adx_series(walk600)
    assert ours[26].adx is None and isinstance(ours[27].adx, Decimal)
    for i in range(14, 600):
        assert _close(ours[i].plus_di, pdi_ref[i]) and _close(ours[i].minus_di, mdi_ref[i])
    assert all(_close(ours[i].adx, adx_ref[i]) for i in range(27, 600))


def test_adx_zero_denominator_is_unknown_and_poisons_recursion():
    flat = hl_series([("100", "100")] * 20)
    moving = random_walk(40, seed=3)
    pts = adx_series(flat)
    assert pts[14].dx is UNKNOWN and pts[19].adx is UNKNOWN
    assert all(isinstance(p.adx, Decimal) for p in adx_series(moving)[27:])


def test_chop_matches_independent_float_formula(walk600):
    ours = chop_series(walk600)
    assert ours[13] is None and ours[14] is not None
    for i in range(14, 600):
        w = walk600[i - 13 : i + 1]
        tr = [
            max(float(x.high - x.low), abs(float(x.high - p.close)), abs(float(x.low - p.close)))
            for p, x in zip(walk600[i - 14 : i], w, strict=True)
        ]
        hh, ll = max(float(x.high) for x in w), min(float(x.low) for x in w)
        ref = 100 * math.log10(sum(tr) / (hh - ll)) / math.log10(14)
        assert abs(float(ours[i]) - ref) < 1e-9


def test_chop_zero_range_fails_closed():
    assert chop_series(hl_series([("100", "100")] * 16))[15] is UNKNOWN


def test_discontinuous_series_is_rejected(walk600):
    with pytest.raises(DiscontinuousSeriesError):
        atr_series(walk600[:10] + walk600[11:30])
