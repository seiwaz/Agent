"""Incremental indicators reproduce the batch references exactly (restart/replay safety)."""

from __future__ import annotations

from sp2l.indicators.chop import chop_series
from sp2l.indicators.ema import ema_series
from sp2l.indicators.incremental import AdxState, AtrState, ChopState, EmaState
from sp2l.indicators.wilder import adx_series, atr_series
from tests.conftest import hl_series


def test_incremental_equals_batch(walk600):
    atr, adx, ema, chop = AtrState(), AdxState(), EmaState(), ChopState()
    inc = [(atr.update(c), adx.update(c), ema.update(c.close), chop.update(c)) for c in walk600]
    assert [x[0] for x in inc] == atr_series(walk600)
    assert [x[1] for x in inc] == adx_series(walk600)
    assert [x[2] for x in inc] == ema_series([c.close for c in walk600])
    assert [x[3] for x in inc] == chop_series(walk600)


def test_incremental_unknown_matches_batch_on_flat_data():
    flat = hl_series([("100", "100")] * 30)
    adx = AdxState()
    assert [adx.update(c) for c in flat] == adx_series(flat)
