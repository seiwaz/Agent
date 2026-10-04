"""Multi-timeframe analysis context with per-timeframe caching.

A timeframe is re-analysed only when a new bar of it has closed; the same parameters apply
to every timeframe so structure, zones and signals stay consistent when the chart switches.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Engine

from sp2l.smc.history import load_bars
from sp2l.smc.model import Analysis, SmcParams
from sp2l.smc.structure import analyze
from sp2l.smc.timeframes import ORDER, bucket_start


def needed_tfs(p: SmcParams) -> list[str]:
    want = {p.trigger_tf, p.bias_tf, p.confirm_bias_tf, *p.poi_tfs}
    return [tf for tf in ORDER if tf in want]


class ContextBuilder:
    def __init__(self, db: Engine, symbol: str, params: SmcParams) -> None:
        self.db = db
        self.symbol = symbol
        self.params = params
        self._cache: dict[str, tuple[datetime, Analysis]] = {}

    def analysis(self, tf: str, upto: datetime) -> Analysis:
        key = bucket_start(upto, tf)
        hit = self._cache.get(tf)
        if hit is not None and hit[0] == key:
            return hit[1]
        bars = load_bars(self.db, self.symbol, tf, self.params.lookback(tf), upto)
        a = analyze(bars, tf, self.params)
        self._cache[tf] = (key, a)
        return a

    def build(self, upto: datetime, tfs: list[str] | None = None) -> dict[str, Analysis]:
        return {tf: self.analysis(tf, upto) for tf in (tfs or needed_tfs(self.params))}
