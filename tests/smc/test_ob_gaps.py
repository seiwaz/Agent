"""The gaps behind an order block, as the chart receives them."""

from __future__ import annotations

from decimal import Decimal as D

from sp2l.api.smc import ob_gaps
from sp2l.smc.structure import analyze
from sp2l.smc.timeframes import length
from tests.smc.test_structure import UP, P, bars


def _block():
    a = analyze(bars(UP), "15m", P)
    return a, a.order_blocks[0], len(a.bars) - 1


def test_every_gap_of_the_move_out_of_the_block_is_returned_fresh():
    a, ob, k = _block()
    gaps = ob_gaps(ob, a, k, None, P)
    # bars 5-7 leave 9.3-9.8, bars 6-8 leave 10-11.3; bar 9 never comes back
    assert [(g["bottom"], g["top"]) for g in gaps] == [("9.3", "9.8"), ("10", "11.3")]
    assert all(g["status"] == "ACTIVE" and not g["tested"] and g["ob"] == ob.id for g in gaps)


def test_the_forming_bar_touches_and_fills_gaps_at_once():
    a, ob, k = _block()
    touched = ob_gaps(ob, a, k, (D(13), D("10.5")), P)
    assert [g["tested"] for g in touched] == [False, True]  # only the upper gap was reached
    filled = ob_gaps(ob, a, k, (D(13), D(9)), P)
    assert [g["status"] for g in filled] == ["FILLED", "FILLED"]
    # a filled gap is drawn over its three candles only
    assert filled[0]["from"] == a.bars[5].open_time.isoformat()
    assert filled[0]["to"] == (a.bars[7].open_time + length("15m")).isoformat()
