"""SPK-01..04 with V5.1 B02 (extension), B03/B29 (one candidate per run), B04 (SEQ triple)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

from sp2l.core.types import Candle, Side
from sp2l.marketdata.m1_builder import M1Result, M1Status
from sp2l.strategy.spike import RunTracker, Spike
from tests.conftest import T0, candle


def ok(i, o, h, lo, c):
    return M1Result(T0 + timedelta(minutes=i), M1Status.OK, candle(i, o, h, lo, c))


def quiet(i, price="101"):
    """A synthetic no-trade minute (B31): ineligible for runs, P-Gap, Spike and Origin."""
    p = D(price)
    t = T0 + timedelta(minutes=i)
    return M1Result(t, M1Status.SYNTHETIC_NO_TRADE, Candle(t, p, p, p, p, D(0), 0, synthetic=True))


def gap(i):
    return M1Result(T0 + timedelta(minutes=i), M1Status.DATA_GAP, None)


def longs(signals):
    return [s for s in signals if s.side is Side.LONG]


def test_pgap_confirms_with_origin_at_run_start():
    rt = RunTracker(D("0.1"))
    seq = [
        ok(0, "100", "101", "95", "96"),  # violation baseline
        ok(1, "96", "99", "94", "98"),  # run start (low 94 < 95 breaks the prior run)
        ok(2, "98", "100", "97", "99.5"),
        ok(3, "99.5", "104", "99", "103"),
        ok(4, "103", "108", "102", "107"),  # low 102 > high[2] 100 -> P-Gap (2,3,4)
    ]
    sigs = [s for m in seq for s in longs(rt.on_m1(m))]
    assert len(sigs) == 1
    s = sigs[0]
    assert s.first_in_run and s.origin.open_time == T0 + timedelta(minutes=1)
    assert s.right.open_time == T0 + timedelta(minutes=4)


def test_pgap_triple_must_satisfy_sequence_b04():
    rt = RunTracker(D("0.1"))
    seq = [
        ok(0, "100", "101", "99", "100"),
        ok(1, "100", "110", "98", "109"),  # low 98 < 99: middle breaks the long sequence
        ok(2, "109", "112", "102", "111"),  # low 102 > high[0] 101 but the triple is not in one run
    ]
    assert [s for m in seq for s in longs(rt.on_m1(m))] == []


def test_second_pgap_same_run_is_log_only_until_sequence_breaks_b03():
    rt = RunTracker(D("0.1"))
    seq = [
        ok(0, "100", "101", "99", "100"),
        ok(1, "99.6", "102", "99.5", "101.9"),  # strong impulse (V5.9)
        ok(2, "101.6", "104", "101.5", "103.9"),  # P-Gap #1 (low 101.5 > high[0] 101)
        ok(3, "103", "106", "103", "105"),  # P-Gap #2 same run (low 103 > high[1] 102)
        ok(4, "105", "105.5", "100", "101"),  # violation -> new run
        ok(5, "100.6", "103", "100.5", "102.9"),
        ok(6, "106", "107", "105.9", "106"),  # low 105.9 > high[4] 105.5 -> first of new run
    ]
    sigs = [s for m in seq for s in longs(rt.on_m1(m))]
    assert [s.first_in_run for s in sigs] == [True, False, True]


def test_violating_candle_seeds_next_run_b29():
    rt = RunTracker(D("0.1"))
    seq = [
        ok(0, "100", "103", "100", "102"),
        ok(1, "102", "103", "99", "100"),  # Low 99 < 100: violation, seeds the new run
        ok(2, "99.6", "101.5", "99.5", "101.4"),  # strong bullish impulse (V5.9)
        ok(3, "103.5", "105", "103.5", "104"),  # low 103.5 > high[1] 103 -> P-Gap (1,2,3)
    ]
    sigs = [s for m in seq for s in longs(rt.on_m1(m))]
    assert len(sigs) == 1 and sigs[0].origin.open_time == T0 + timedelta(minutes=1)


def test_data_gap_breaks_run():
    rt = RunTracker(D("0.1"))
    seq = [
        ok(0, "100", "101", "99", "100"),
        ok(1, "100", "102", "99.5", "101"),
        gap(2),
        ok(3, "102", "104", "101.5", "103"),
        ok(4, "103", "106", "103", "105"),
    ]
    assert [s for m in seq for s in longs(rt.on_m1(m))] == []


def test_missing_minute_breaks_run():
    rt = RunTracker(D("0.1"))
    seq = [
        ok(0, "100", "101", "99", "100"),
        ok(1, "100", "102", "99.5", "101"),
        quiet(2),
        ok(3, "102", "104", "101.5", "103"),
        ok(4, "103", "106", "103", "105"),
    ]
    assert [s for m in seq for s in longs(rt.on_m1(m))] == []


def test_extension_every_continuing_candle_b02_and_stops_on_break():
    base = [candle(i, str(100 + i), str(101 + i), str(99 + i), str(100 + i)) for i in range(3)]
    sp = Spike(Side.LONG, list(base))
    assert sp.try_extend(ok(3, "102", "106", "101.5", "105"))  # higher low: extends
    assert sp.try_extend(ok(4, "105", "105.2", "101.5", "104"))  # equal low still continues
    assert not sp.try_extend(ok(5, "104", "107", "101", "106"))  # lower low: sequence ends
    assert not sp.try_extend(ok(6, "106", "108", "105", "107"))  # frozen afterwards
    assert sp.last.open_time == T0 + timedelta(minutes=4)
    assert str(sp.extreme) == "106" and str(sp.range) == "7"


def test_extension_stops_at_synthetic_minute():
    sp = Spike(Side.LONG, [candle(0, "100", "101", "99", "100.5")])
    assert not sp.try_extend(quiet(1, "100.5"))
    assert not sp.try_extend(ok(2, "101", "103", "100", "102"))
