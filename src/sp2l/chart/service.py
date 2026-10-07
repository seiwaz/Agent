"""Chart workspace data: candles, overlays and the per-timeframe trend, from Tabdeal's chart
feed (`sp2l.chart.history`).

Overlays are computed from CLOSED bars only and cached per (timeframe, first and last closed
bar, parameters), so a poll between two bar closes costs nothing. The forming bar is part of the
candles only.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict
from typing import Any

from sqlalchemy import Engine

from sp2l.chart.history import SECONDS, TabdealHistory
from sp2l.chart.overlays import ChartParams, overlays, structure, trend
from sp2l.smc.model import SmcParams

CHART_TFS = ("5m", "15m", "1h", "4h", "1d")
TREND_BARS = 300  # closed bars per timeframe for the trend strip
TREND_TTL_S = 30.0
MAX_OVERLAY_BARS = 5000


class ChartService:
    def __init__(self, db: Engine, symbol: str, params: SmcParams,
                 history: TabdealHistory | None = None) -> None:
        self.symbol, self.params = symbol, params
        self.history = history or TabdealHistory(db, symbol)
        self._lock = threading.Lock()
        self._cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._trends: dict[int, tuple[float, dict[str, Any]]] = {}  # by swing_len

    def candles(self, tf: str, limit: int, before: int | None = None) -> dict[str, Any]:
        """The newest `limit` bars (the forming one last), or `limit` bars before `before`."""
        now = time.time()
        if before is None:
            bars, more = self.history.tail(tf, limit), True
        else:
            bars, more = self.history.older(tf, before, limit)
        return {
            "tf": tf,
            "more": more,
            "items": [{**b, "forming": b["t"] + SECONDS[tf] > now} for b in bars],
        }

    def overlays(self, tf: str, since: int | None, bars: int, cp: ChartParams) -> dict[str, Any]:
        """Overlays over the closed bars from `since` (or the newest `bars`) to now."""
        closed = self.history.since(tf, since if since is not None else 0,
                                    MAX_OVERLAY_BARS if since is not None else bars)
        if not closed:
            return {"tf": tf, "ready": False, "reason": "no closed bars from Tabdeal yet"}
        key = (tf, closed[0].open_time, closed[-1].open_time, tuple(sorted(asdict(cp).items())))
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        out = overlays(closed, tf, self.params, cp)
        out["params"] = asdict(cp)
        with self._lock:
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[key] = out
        return out

    def trends(self, swing_len: int | None = None) -> dict[str, Any]:
        cp = ChartParams(swing_len=swing_len or self.params.swing_len)
        with self._lock:
            hit = self._trends.get(cp.swing_len)
            if hit is not None and time.time() - hit[0] < TREND_TTL_S:
                return hit[1]
        out: dict[str, Any] = {"symbol": self.symbol, "timeframes": []}
        for tf in CHART_TFS:
            try:
                self.history.tail(tf, TREND_BARS)
                closed = self.history.since(tf, 0, TREND_BARS)
            except Exception:  # Tabdeal unreachable: this timeframe has no trend for now
                closed = []
            if len(closed) < 2 * cp.swing_len + 2:
                out["timeframes"].append({"tf": tf, "trend": "UNDEFINED", "bars": len(closed)})
                continue
            out["timeframes"].append(
                {"tf": tf, "trend": trend(structure(closed, tf, self.params, cp)),
                 "bars": len(closed)}
            )
        with self._lock:
            self._trends[cp.swing_len] = (time.time(), out)
        return out
