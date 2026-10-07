"""Chart workspace data: candles, overlays and the per-timeframe trend, from the stored history.

Overlays are computed from CLOSED bars only and cached per (timeframe, last closed bar,
parameters), so a poll between two bar closes costs nothing. The forming bar is part of the
candles only.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any

from sqlalchemy import Engine

from sp2l.chart.overlays import ChartParams, overlays, structure, trend
from sp2l.smc.history import load_bars, series_end
from sp2l.smc.model import SmcParams
from sp2l.smc.timeframes import length

CHART_TFS = ("5m", "15m", "1h", "4h", "1d")
TREND_BARS = 300  # closed bars per timeframe for the trend strip
TREND_TTL_S = 30.0


class ChartService:
    def __init__(self, db: Engine, symbol: str, params: SmcParams) -> None:
        self.db, self.symbol, self.params = db, symbol, params
        self._lock = threading.Lock()
        self._cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._trends: dict[int, tuple[float, dict[str, Any]]] = {}  # by swing_len

    @property
    def grid(self) -> str:
        return self.params.htf_grid

    def candles(self, tf: str, limit: int) -> dict[str, Any]:
        upto = series_end(self.db, self.symbol)
        if upto is None:
            return {"tf": tf, "items": []}
        bars = load_bars(
            self.db, self.symbol, tf, limit, upto, include_forming=True, grid=self.grid
        )
        return {
            "tf": tf,
            "upto": upto.isoformat(),
            "items": [
                {
                    "t": int(b.open_time.timestamp()),
                    "o": float(b.open),
                    "h": float(b.high),
                    "l": float(b.low),
                    "c": float(b.close),
                    "v": float(b.volume),
                    "forming": b.open_time + length(tf) > upto,
                }
                for b in bars
            ],
        }

    def overlays(self, tf: str, bars: int, cp: ChartParams) -> dict[str, Any]:
        upto = series_end(self.db, self.symbol)
        if upto is None:
            return {"tf": tf, "ready": False, "reason": "no stored history"}
        closed = load_bars(self.db, self.symbol, tf, bars, upto, grid=self.grid)
        last: datetime | None = closed[-1].open_time if closed else None
        key = (tf, bars, last, tuple(sorted(asdict(cp).items())))
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                return hit
            out = overlays(closed, tf, self.params, cp)
            out["params"] = asdict(cp)
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
        upto = series_end(self.db, self.symbol)
        out: dict[str, Any] = {"symbol": self.symbol, "timeframes": []}
        for tf in CHART_TFS:
            closed = (
                []
                if upto is None
                else load_bars(self.db, self.symbol, tf, TREND_BARS, upto, grid=self.grid)
            )
            if len(closed) < 2 * cp.swing_len + 2:
                out["timeframes"].append({"tf": tf, "trend": "UNDEFINED", "bars": len(closed)})
                continue
            out["timeframes"].append(
                {
                    "tf": tf,
                    "trend": trend(structure(closed, tf, self.params, cp)),
                    "bars": len(closed),
                }
            )
        with self._lock:
            self._trends[cp.swing_len] = (time.time(), out)
        return out
