"""The playbook of one market: its closed 1h and 4h bars from Tabdeal's chart feed (cached in
chart_bars), a strategy run over them, and the answer the chart draws (series, setups, the
current setup with its checklist, the backtest)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from sp2l.chart.history import SECONDS, TabdealHistory
from sp2l.playbook.engine import (
    STRATEGIES,
    Costs,
    Ctx,
    Params,
    Setup,
    Strategy,
    outcome,
    run,
    stats,
)

PAGE = 500
WARM_1H = 300  # 1h bars before the window: ATR / EMA 50 / ADX / anchors warm up
WARM_4H = 260  # 4h bars before the window: the EMA 200 warms up
LIVE = ("pending", "open")


def _series(x: Ctx, st: Strategy, start: int) -> list[dict[str, Any]]:
    def pts(vals: list[float | None]) -> list[list[Any]]:
        out: list[list[Any]] = []
        for i in range(start, x.n):
            v = vals[i]
            out.append([x.t[i], None if v is None else round(v, 10)])
        return out

    out: list[dict[str, Any]] = [
        {"id": "ema4", "label": "EMA 200 (4h)", "pane": "price", "points": pts(x.ema4)}]
    if st.id == "donchian":
        out += [
            {"id": "prev_hi", "label": "Previous high (entry)", "pane": "price",
             "points": pts([None if v is None else v[0] for v in x.prev_hi])},
            {"id": "lo_out", "label": f"{x.p.exit_len}-bar low (exit)", "pane": "price",
             "points": pts(x.lo_out)},
        ]
    elif st.id == "ema":
        out += [
            {"id": "ema50", "label": f"EMA {x.p.ema_len}", "pane": "price",
             "points": pts(x.ema50)},
            {"id": "adx", "label": "ADX 14", "pane": "own", "levels": [x.p.adx_min],
             "points": pts(x.adx)},
        ]
    elif st.id == "avwap":
        for d, sid, label in ((1, "vwap_lo", "AVWAP (swing low)"),
                              (-1, "vwap_hi", "AVWAP (swing high)")):
            vals: list[float | None] = []
            prev = None
            for i in range(x.n):
                a = x.anchor[d][i]
                # a new anchor starts a new line (a gap at the change)
                vals.append(None if a is None or (prev is not None and a != prev)
                            else x.vwap(a, i))
                prev = a
            out.append({"id": sid, "label": label, "pane": "price", "points": pts(vals)})
    return out


def _row(s: Setup, x: Ctx, costs: Costs) -> dict[str, Any]:
    t = x.t
    risk = abs(s.entry - s.sl)
    return {
        "id": f"{s.strategy}-{s.side[0]}-{t[s.signal_i]}",
        "strategy": s.strategy, "side": s.side, "status": s.status, "reason": s.reason,
        "kind": s.kind,
        "signal_t": t[s.signal_i],
        "fill_t": t[s.fill_i] if s.fill_i is not None else None,
        "exit_t": t[s.exit_i] if s.exit_i is not None else None,
        "end_t": t[s.end_i] if s.end_i is not None else None,
        # a resting order: valid until the close of its last bar
        "valid_until": t[s.signal_i] + (s.valid + 1) * x.tf_s if s.kind != "market" else None,
        "entry": s.entry, "fill": s.fill, "sl": s.sl, "tp": s.tp, "exit_price": s.exit_price,
        "rr": round(abs(s.tp - s.entry) / risk, 2) if s.tp is not None and risk else None,
        "stop_pct": round(risk / s.entry * 100, 3) if s.entry else None,
        "exit_next": s.exit_next,
        "marks": s.marks,
        **outcome(s, x, costs),
    }


class Playbook:
    def __init__(self, history: TabdealHistory, costs: Costs,
                 clock: Callable[[], float] = time.time) -> None:
        self.history, self.costs, self.clock = history, costs, clock
        self._lock = threading.Lock()
        self._cache: dict[tuple[Any, ...], dict[str, Any]] = {}

    def _bars(self, tf: str, n: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """The newest n closed bars (oldest first) and the forming bar."""
        bars = self.history.tail(tf, PAGE)
        while bars and len(bars) < n + 1:
            older, more = self.history.older(tf, bars[0]["t"], PAGE)
            older = [b for b in older if b["t"] < bars[0]["t"]]
            if not older:
                break
            bars = older + bars
            if not more:
                break
        now = self.clock()
        closed = [b for b in bars if b["t"] + SECONDS[tf] <= now]
        forming = bars[-1] if bars and bars[-1]["t"] + SECONDS[tf] > now else None
        return closed[-n:], forming

    def analyse(self, strategy: str, days: int = 90, htf_filter: bool = True,
                brief: bool = False) -> dict[str, Any]:
        st = STRATEGIES[strategy]
        p = replace(Params(), htf_filter=htf_filter)
        with self._lock:  # one fetch per market at a time
            h1, forming = self._bars("1h", days * 24 + WARM_1H)
            h4, _ = self._bars("4h", days * 6 + WARM_4H)
            if not h1:
                return {"strategy": strategy, "ready": False, "reason": "no 1h bars from Tabdeal"}
            key = (strategy, days, htf_filter, h1[-1]["t"], len(h1), len(h4))
            hit = self._cache.get(key)
            if hit is None:
                x = Ctx(h1, h4, p)
                since = h1[-1]["t"] - days * 86400
                start = next((i for i, t in enumerate(x.t) if t > since), 0)
                rows = [_row(s, x, self.costs) for s in run(x, st, self.costs, start)]
                live = [r for r in rows if r["status"] in LIVE]
                last = x.n - 1
                fresh = [r for r in rows if r["status"] == "rejected"
                         and r["signal_t"] == x.t[last]]
                hit = {
                    "strategy": strategy, "label": st.label, "target": st.target,
                    "ready": True, "tf": "1h", "days": days, "htf_filter": htf_filter,
                    "asof": x.t[last], "close": x.c[last],
                    "from": x.t[start], "bars": x.n - start,
                    "current": (live or fresh)[-1] if live or fresh else None,
                    "checks": {side: st.detect(x, last, d)[0]
                               for side, d in (("LONG", 1), ("SHORT", -1)) if d in st.sides},
                    "setups": rows,
                    "stats": stats(rows),
                    "series": _series(x, st, start),
                    "costs": vars(self.costs),
                }
                if len(self._cache) > 32:
                    self._cache.clear()
                self._cache[key] = hit
        out = {**hit, "price": forming["c"] if forming else hit["close"]}
        if brief:  # the market scanner: no drawing
            out = {k: v for k, v in out.items() if k not in ("series", "setups")}
        return out
