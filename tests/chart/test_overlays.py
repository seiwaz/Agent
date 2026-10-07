# ruff: noqa: E501  (synthetic bar rows are clearer on one line)
"""Chart overlays: swing labels, bounded S/R zones and trendlines, zones end where they ended,
and nothing uses a bar that had not closed."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from sp2l.chart import overlays as ov
from sp2l.core.types import Candle
from sp2l.smc.model import SmcParams

T0 = datetime(2026, 1, 1, tzinfo=UTC)
P = SmcParams(htf_grid="utc")
CP = ov.ChartParams(swing_len=2, sr_tol_atr=0.5, sr_touches=2, sr_max=20, tl_max=5)


def bars_from(closes, spread=0.6):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(Candle(T0 + timedelta(hours=i), D(str(round(o, 4))), D(str(round(max(o, c) + spread, 4))), D(str(round(min(o, c) - spread, 4))), D(str(round(c, 4))), D(1)))
        prev = c
    return out


def zigzag(points, step=6):
    """Linear legs between turning points, `step` bars per leg."""
    out = []
    for a, b in zip(points, points[1:], strict=False):
        out += [a + (b - a) * k / step for k in range(step)]
    return out + [points[-1]]


def walk(n, seed):
    r, px, out = random.Random(seed), 100.0, []
    for _ in range(n):
        px *= math.exp(r.gauss(0, 0.01))
        out.append(px)
    return bars_from(out, spread=0.3)


def test_swings_are_labelled_against_the_previous_swing_of_their_kind():
    # rising highs and lows, then a lower high and a lower low
    b = bars_from(zigzag([100, 110, 104, 116, 108, 112, 101, 103]))
    labels = [s["label"] for s in ov.labelled_swings(ov.structure(b, "1h", P, CP))]
    assert {"HH", "HL", "LH", "LL"} <= set(labels)
    assert labels.index("HH") < labels.index("LH")  # the rise first, then the lower high


def test_support_zone_is_bounded_and_ends_at_the_breaking_close():
    # two bounces off ~100, then a close well below: the zone ends at that bar
    closes = [110.0] * 20 + zigzag([110, 100, 110, 100.2, 110, 104, 92, 90])  # 20 bars of ATR warmup
    b = bars_from(closes)
    a = ov.structure(b, "1h", P, CP)
    sr = ov.support_resistance(a, CP)
    z = next(z for z in sr if z["bottom"] < 100 < z["top"])
    assert z["role"] == "SUPPORT" and z["broken"] and z["touches"] >= 2
    brk = next(i for i, x in enumerate(b) if i > 40 and float(x.close) < z["bottom"])
    assert datetime.fromisoformat(z["to"]) <= b[brk].open_time + timedelta(hours=3)
    assert datetime.fromisoformat(z["to"]) < b[-1].open_time  # not drawn to the right edge


def test_unbroken_zone_and_line_end_at_the_last_bar_never_later():
    b = walk(400, 3)
    o = ov.overlays(b, "1h", P, CP)
    last = b[-1].open_time
    for x in o["sr"] + o["trendlines"] + o["zones"]:
        assert datetime.fromisoformat(x["to"]) <= last
        assert datetime.fromisoformat(x["from"]) <= datetime.fromisoformat(x["to"])
    assert any(not x["broken"] for x in o["sr"]) or o["sr"] == []


def test_rising_trendline_breaks_on_a_close_below_it():
    closes = zigzag([100, 108, 102, 112, 106, 116, 109, 120, 95])
    b = bars_from(closes)
    tls = ov.trendlines(ov.structure(b, "1h", P, CP), CP)
    sup = [t for t in tls if t["role"] == "SUPPORT"]
    assert sup and sup[0]["to_price"] > sup[0]["from_price"]
    assert any(t["broken"] for t in sup)


def test_no_lookahead_overlays_identical_on_the_shared_past():
    b = walk(500, 9)
    full = ov.overlays(b, "1h", P, CP)
    part = ov.overlays(b[:350], "1h", P, CP)
    cut = b[349].open_time
    # every swing and event known by bar 349 is identical in the full run
    assert [s for s in full["swings"] if datetime.fromisoformat(s["time"]) <= cut - timedelta(hours=CP.swing_len)][: len(part["swings"])] == part["swings"][: len([s for s in full["swings"] if datetime.fromisoformat(s["time"]) <= cut - timedelta(hours=CP.swing_len)])]
    assert [e for e in full["events"] if datetime.fromisoformat(e["to"]) <= cut] == part["events"]


def test_not_ready_with_too_few_bars():
    assert ov.overlays(walk(5, 1), "1h", P, CP)["ready"] is False
