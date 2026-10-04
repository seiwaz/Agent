"""Swings, BOS / CHoCH, order blocks, fair value gaps and their causality."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from hypothesis import given, settings
from hypothesis import strategies as st

from sp2l.core.types import Candle, Side
from sp2l.smc.model import SmcParams, ZoneStatus
from sp2l.smc.structure import analyze
from tests.conftest import random_walk

T0 = datetime(2026, 1, 1, tzinfo=UTC)
P = SmcParams(swing_len=2, atr_len=3, fvg_min_atr=D(0))


def bars(rows: list[tuple[float, float, float, float]]) -> list[Candle]:
    return [
        Candle(T0 + timedelta(minutes=i), D(str(o)), D(str(h)), D(str(lo)), D(str(c)))
        for i, (o, h, lo, c) in enumerate(rows)
    ]


# a swing high at bar 2 (12), a pullback low at bar 5 (8), then a close above 12 at bar 8
UP = [
    (10, 10.5, 9.5, 10),
    (10, 11, 9.8, 11),
    (11, 12, 10.8, 11.5),  # swing high 12
    (11.5, 11.6, 10, 10.2),
    (10.2, 10.4, 9, 9.2),
    (9.2, 9.3, 8, 8.4),  # lowest low 8: the order block candle
    (8.4, 10, 8.3, 9.9),
    (9.9, 11.5, 9.8, 11.4),
    (11.4, 13, 11.3, 12.8),  # closes above 12: BOS (first break)
    (12.8, 13.5, 12.6, 13.2),
]


def test_swing_is_known_only_swing_len_bars_later():
    a = analyze(bars(UP), "1m", P)
    hi = [s for s in a.swings if s.kind == "HIGH"][0]
    assert (hi.idx, hi.price, hi.confirmed_idx) == (2, D(12), 4)


def test_first_break_is_bos_and_its_order_block_is_the_impulse_origin():
    a = analyze(bars(UP), "1m", P)
    ev = a.events[0]
    assert (ev.kind, ev.direction, ev.level, ev.break_idx) == ("BOS", Side.LONG, D(12), 8)
    ob = a.event_ob[ev.id]
    assert (ob.idx, ob.bottom, ob.top, ob.created_idx) == (5, D(8), D("9.3"), 8)
    assert a.trend[7] == 0 and a.trend[8] == 1


def test_break_against_the_trend_is_choch():
    down = [
        (13.2, 13.3, 12.4, 12.5),
        (12.5, 12.6, 11, 11.1),
        (11.1, 11.2, 10.5, 10.6),
        (10.6, 10.7, 7, 7.2),
    ]
    a = analyze(bars(UP + down), "1m", P)
    kinds = [(e.kind, e.direction) for e in a.events]
    assert ("CHOCH", Side.SHORT) in kinds


def test_fvg_is_invalid_once_completely_filled_by_a_wick():
    rows = [
        (10, 10.5, 9.5, 10),
        (10, 12, 9.9, 11.9),
        (12, 13, 11, 12.9),
        (12.9, 13, 10.4, 12.0),
    ]  # the wick fills the whole gap, the close stays above
    a = analyze(bars(rows), "1m", P)
    assert a.fvgs[0].status is ZoneStatus.MITIGATED and a.fvgs[0].mitigated_idx == 3
    keep = analyze(
        bars(rows), "1m", SmcParams(swing_len=2, atr_len=3, fvg_min_atr=D(0), fvg_fill="close")
    )
    assert keep.fvgs[0].status is ZoneStatus.TESTED


def test_fvg_detection_and_mitigation_by_close():
    rows = [
        (10, 10.5, 9.5, 10),
        (10, 12, 9.9, 11.9),
        (12, 13, 11, 12.9),  # low 11 > high 10.5
        (12.9, 13, 10.8, 11.2),  # trades into the gap: tested
        (11.2, 11.3, 9.0, 10.0),
    ]  # closes below 10.5: mitigated
    a = analyze(bars(rows), "1m", P)
    fvg = a.fvgs[0]
    assert (fvg.direction, fvg.bottom, fvg.top, fvg.created_idx) == (Side.LONG, D("10.5"), D(11), 2)
    assert (fvg.tested_idx, fvg.mitigated_idx, fvg.status) == (3, 4, ZoneStatus.MITIGATED)
    assert not fvg.valid_at(4) and fvg.valid_at(3)


def test_zones_expire_after_the_lookback():
    p = SmcParams(swing_len=2, atr_len=3, fvg_min_atr=D(0), lookback_1m=3)
    rows = [(10, 10.5, 9.5, 10), (10, 12, 9.9, 11.9), (12, 13, 11, 12.9)] + [
        (12.9, 13.2, 12.6, 13)
    ] * 6
    a = analyze(bars(rows), "1m", p)
    fvg = a.fvgs[0]
    assert fvg.valid_at(5) and not fvg.valid_at(6)


@settings(max_examples=15, deadline=None)
@given(seed=st.integers(0, 10_000), cut=st.integers(60, 280))
def test_analysis_is_causal(seed, cut):
    """Events, zones and trend up to bar k never change when later bars are appended."""
    walk = random_walk(300, seed)
    p = SmcParams(swing_len=3)
    full, part = analyze(walk, "5m", p), analyze(walk[:cut], "5m", p)
    assert [e for e in full.events if e.break_idx < cut] == part.events
    assert full.trend[:cut] == part.trend
    known = [(z.id, z.top, z.bottom) for z in full.zones if z.created_idx < cut]
    assert known == [(z.id, z.top, z.bottom) for z in part.zones]
    for zf in full.zones:
        if zf.created_idx >= cut:
            continue
        zp = next(z for z in part.zones if z.id == zf.id)
        assert zf.valid_at(cut - 1) == zp.valid_at(cut - 1)


def test_small_higher_timeframe_order_blocks_are_dropped_but_never_the_m1_block():
    small_ob = SmcParams(swing_len=2, atr_len=3, fvg_min_atr=D(0), ob_min_atr=D(5))
    htf = analyze(bars(UP), "15m", small_ob)
    assert htf.events and not htf.order_blocks  # the break is kept, the block is not
    m1 = analyze(bars(UP), "1m", small_ob)  # the trigger timeframe keeps its block
    assert m1.event_ob[m1.events[0].id].kind == "OB"


def test_ob_require_fvg_drops_blocks_whose_move_left_no_gap():
    strict = SmcParams(swing_len=2, atr_len=3, fvg_min_atr=D(0), ob_require_fvg=True)
    # UP: bar 7's low 9.8 is above the block's high 9.3 - the rally left a gap: the block stays
    assert analyze(bars(UP), "15m", strict).order_blocks
    # the same rally without a gap (every bar overlaps the one two bars back): no block
    nogap = UP[:7] + [(9.9, 11.5, 9.2, 11.4), (11.4, 13, 9.95, 12.8), (12.8, 13.5, 12.6, 13.2)]
    assert analyze(bars(nogap), "15m", strict).events
    assert not analyze(bars(nogap), "15m", strict).order_blocks
    assert analyze(bars(nogap), "1m", strict).order_blocks  # the M1 trigger block always stays
