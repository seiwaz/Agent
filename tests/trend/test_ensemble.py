"""Ensemble / vol-target engine: signal, no look-ahead, band, gross cap, costs, delisting,
universe ranking."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import numpy as np
import pytest

from sp2l.core.types import Candle
from sp2l.trend import ensemble as en

T0 = datetime(2020, 1, 1, tzinfo=UTC)


def series(closes, sym="X", start=0, vol=1.0):
    bars = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        bars.append(
            Candle(
                T0 + timedelta(days=start + i),
                D(str(o)),
                D(str(max(o, c))),
                D(str(min(o, c))),
                D(str(c)),
                D(str(vol)),
            )
        )
    return bars


def walk(n, seed, drift=0.001, start=0, sym="X", vol=1.0):
    r, px, out = random.Random(seed), 100.0, []
    for _ in range(n):
        px *= math.exp(r.gauss(drift, 0.03))
        out.append(round(px, 6))
    return series(out, sym, start, vol)


def test_signal_long_in_uptrend_flat_after_reversal():
    c = np.array([100 + i for i in range(60)] + [159 - 3 * i for i in range(30)], dtype=float)
    s = en.ensemble_signal(c, (5, 10, 20))
    assert s[25] == 1.0  # every lookback long in the steady rise
    assert s[-1] == 0.0  # every trailing stop hit in the fall
    assert np.all(s[:5] == 0)  # no lookback has history yet


def test_signal_trailing_stop_never_moves_down():
    c = np.array([100 + i for i in range(30)] + [129 - i for i in range(5)], dtype=float)
    # L=10: mid of the last 10 closes keeps rising while the rally lasts; the dip below it exits
    s = en.ensemble_signal(c, (10,))
    assert s[29] == 1.0 and s[-1] == 0.0


def test_realized_vol_matches_numpy():
    c = np.array([100 * 1.01**i * (1 + 0.02 * (-1) ** i) for i in range(50)])
    v = en.realized_vol(c, 10)
    r = c[1:] / c[:-1] - 1
    assert math.isnan(v[9]) and v[10] == pytest.approx(r[0:10].std(ddof=1) * math.sqrt(365))


def test_no_lookahead_equity_identical_when_the_future_is_cut():
    bars = {"A": walk(600, 1), "B": walk(500, 2, start=100)}
    p = en.EnsembleParams(
        lookbacks=(5, 20, 60), target_vol=0.5, slots=2, universe_size=2, band=0.01
    )
    full = en.run_ensemble(en.series_from(bars), p, 0.001, 0.0003)
    for cut in (200, 350, 550):
        cut_t = T0 + timedelta(days=cut)
        part = en.run_ensemble(
            en.series_from({k: [b for b in v if b.open_time < cut_t] for k, v in bars.items()}),
            p,
            0.001,
            0.0003,
        )
        assert part.equity == pytest.approx(full.equity[: len(part.equity)])


def test_weight_follows_signal_and_vol_target_with_costs():
    bars = {"A": series([100 * 1.01**i for i in range(80)])}  # steady rise, tiny vol
    p = en.EnsembleParams(lookbacks=(5, 10), target_vol=0.25, band=0.05)
    r = en.run_ensemble(en.series_from(bars), p, 0.001, 0.0)
    assert r.gross[-1] == pytest.approx(1.0, abs=0.02)  # vol << target: capped at weight 1
    assert r.costs == pytest.approx(r.traded * 0.001)
    flat = en.run_ensemble(en.series_from(bars), p, 0.0, 0.0)
    assert flat.equity[-1] > r.equity[-1]


def test_band_skips_small_rebalances():
    bars = {"A": walk(400, 7)}
    wide = en.run_ensemble(
        en.series_from(bars), en.EnsembleParams(lookbacks=(5, 20), band=0.2), 0.0, 0.0
    )
    tight = en.run_ensemble(
        en.series_from(bars), en.EnsembleParams(lookbacks=(5, 20), band=0.0), 0.0, 0.0
    )
    assert wide.trades < tight.trades


def test_gross_cap_scales_all_targets():
    up = [100 * 1.01**i for i in range(80)]
    bars = {"A": series(up, "A"), "B": series(up, "B"), "C": series(up, "C")}
    p = en.EnsembleParams(lookbacks=(5,), target_vol=1.0, slots=1, gross_cap=1.0, band=0.0)
    r = en.run_ensemble(en.series_from(bars), p, 0.0, 0.0)
    assert max(r.gross) <= 1.0 + 1e-9 and r.gross[-1] == pytest.approx(1.0, abs=0.02)


def test_delisted_series_is_sold_at_its_last_close():
    up = [100 * 1.01**i for i in range(60)]
    bars = {"A": series(up, "A"), "B": series([100 * 1.01**i for i in range(90)], "B")}
    p = en.EnsembleParams(lookbacks=(5,), target_vol=1.0, slots=2, band=0.0)
    r = en.run_ensemble(en.series_from(bars), p, 0.0, 0.0)
    assert r.forced_exits and r.forced_exits[0][0] == "A"
    assert r.forced_exits[0][1] == T0 + timedelta(days=60)


def test_gap_splits_a_series_into_separate_assets():
    b = series([100.0] * 10) + [
        Candle(T0 + timedelta(days=30 + i), D(1), D(1), D(1), D(1), D(1)) for i in range(5)
    ]
    parts = en.split_series("LUNA", b)
    assert [p.sym for p in parts] == ["LUNA", "LUNA#2"] and len(parts[1].t) == 5


def test_universe_takes_the_top_dollar_volume():
    bars = {
        k: walk(120, i, sym=k, vol=v)
        for i, (k, v) in enumerate([("A", 1.0), ("B", 50.0), ("C", 10.0)])
    }
    p = en.EnsembleParams(lookbacks=(5,), universe_size=2, min_history=30, volume_len=30, slots=2)
    r = en.run_ensemble(en.series_from(bars), p, 0.0, 0.0)
    later = [m for d, m in r.members.items() if d >= T0 + timedelta(days=60)]
    assert later and all(set(m) == {"B", "C"} for m in later)
