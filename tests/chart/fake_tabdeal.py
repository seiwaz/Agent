"""A stand-in for Tabdeal's chart feed (`sp2l.chart.history.plots`): deterministic bars for any
range, aligned like Tabdeal's (5m / 15m on UTC, 1h / 4h / 1d on Tehran boundaries), continuity
bars (open = previous close), nothing before the listing, the last bar still forming."""

from __future__ import annotations

import math
import time
from typing import Any

from sp2l.chart.history import SECONDS

OFFSET = {"5m": 0, "15m": 0, "1h": 1800, "4h": 1800, "1d": 73800}  # Tehran = UTC+03:30
LISTED_DAYS = 200


class FakeTabdeal:
    def __init__(self, listed_days: int = LISTED_DAYS) -> None:
        self.listed = int(time.time()) - listed_days * 86400
        self.calls: list[tuple[str, str, int, int]] = []

    @staticmethod
    def price(tf: str, k: int) -> float:
        n = k * SECONDS[tf] / 3600  # hours since the epoch: the same path on every timeframe
        wobble = math.sin(k * 12.9898) * 43758.5453 % 1 - 0.5
        return 50_000 * (1 + 0.15 * math.sin(n / 90) + 0.05 * math.sin(n / 9)) + 120 * wobble

    def __call__(self, market: str, tf: str, start: int, end: int) -> list[dict[str, Any]]:
        self.calls.append((market, tf, start, end))
        step, off = SECONDS[tf], OFFSET[tf]
        now = int(time.time())
        lo = max(start, self.listed)
        k0 = -(-(lo - off) // step)  # first bar opening at or after lo
        out = []
        k = k0
        while (t := k * step + off) < min(end, now + 1):
            scale = 1.0 if market.startswith("BTC") else 1.45 / 50_000  # XRP trades near 1.45
            o, c = self.price(tf, k - 1) * scale, self.price(tf, k) * scale
            hi, lo_ = max(o, c) * 1.002, min(o, c) * 0.998
            if scale != 1.0:
                o, c, hi, lo_ = (round(v, 5) for v in (o, c, hi, lo_))
            out.append({"t": t, "o": o, "h": hi, "l": lo_, "c": c, "v": 1.0})
            k += 1
        return out
