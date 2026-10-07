"""Chart candles straight from Tabdeal (the feed of Tabdeal's own futures chart), any depth.

Source: GET https://api-web.tabdeal.org/special-margin/plots/history/ (public, read-only; see
docs/tabdeal_history_source.md). One request returns a whole [from, to) range at a native
resolution: 5m -> 5, 15m -> 15, 1h -> 60, 4h -> 240, 1d -> 1D. The datafeed's timezone is
Asia/Tehran, so 1h / 4h / 1d bars open on Tehran boundaries, exactly as on Tabdeal's chart.
Bars follow the continuity model (open = previous close); the last bar of a recent range is still
forming.

Closed bars are cached in `chart_bars` (one row per market, timeframe and bar), so scrolling
back reads the database and asks Tabdeal only for ranges it has not seen. The most recent bars
are re-read from Tabdeal at most every TAIL_TTL_S seconds.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, text

from sp2l.core.types import Candle
from sp2l.marketdata import tabdeal_public
from sp2l.marketdata.tabdeal_ws import ws_market

log = logging.getLogger("sp2l.chart")
RESOLUTION = {"5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "1D"}
SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
TAIL_TTL_S = 5.0
TIMEOUT_S = 60.0
Bar = dict[str, Any]  # {"t": epoch s (open), "o", "h", "l", "c", "v"}


def plots(market: str, tf: str, start: int, end: int) -> list[Bar]:
    """Tabdeal chart bars with start <= open time < end (epoch seconds), oldest first."""
    base, quote = market.split("_")
    data = tabdeal_public._get(
        "/special-margin/plots/history/",
        {
            "first_currency_symbol": base,
            "second_currency_symbol": quote,
            "from": start,
            "to": end,
            "resolution": RESOLUTION[tf],
            "countback": max(1, (end - start) // SECONDS[tf] + 1),
            "symbol": market,
        },
        TIMEOUT_S,
    )
    out = []
    for b in data.get("data") or []:
        try:
            t = int(b["time"])
            o, h, lo, c = (float(b[k]) for k in ("open", "high", "low", "close"))
            v = float(b.get("volume") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if start <= t < end and lo <= min(o, c) and max(o, c) <= h:
            out.append({"t": t, "o": o, "h": h, "l": lo, "c": c, "v": v})
    out.sort(key=lambda x: x["t"])
    return out


Fetch = Callable[[str, str, int, int], list[Bar]]


class TabdealHistory:
    """Candles of one market from Tabdeal, cached; `fetch` is replaceable in tests."""

    def __init__(self, db: Engine, symbol: str, fetch: Fetch | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.db, self.symbol, self.market = db, symbol, ws_market(symbol)
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._tail: dict[str, tuple[float, list[Bar]]] = {}
        self._first: dict[str, int] = {}  # the oldest bar Tabdeal has, once a page came back short

    def fetch(self, tf: str, start: int, end: int) -> list[Bar]:
        f = self._fetch or plots  # looked up at call time (tests replace `plots`)
        return f(self.market, tf, start, end)

    def _final(self, tf: str, bars: list[Bar]) -> list[Bar]:
        now = self._clock()
        return [b for b in bars if b["t"] + SECONDS[tf] <= now]

    def _store(self, tf: str, bars: list[Bar]) -> None:
        rows = [
            {"s": self.symbol, "tf": tf, "t": datetime.fromtimestamp(b["t"], UTC),
             "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]}
            for b in self._final(tf, bars)
        ]
        if not rows:
            return
        with self.db.begin() as c:
            c.execute(
                text(
                    "INSERT INTO chart_bars (symbol, tf, open_time, open, high, low, close, volume)"
                    " VALUES (:s, :tf, :t, :o, :h, :l, :c, :v) ON CONFLICT (symbol, tf, open_time)"
                    " DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,"
                    " close = EXCLUDED.close, volume = EXCLUDED.volume, fetched_at = now()"
                ),
                rows,
            )

    def _stored(self, tf: str, before: int | None, since: int | None, limit: int) -> list[Bar]:
        q = (
            "SELECT open_time, open, high, low, close, volume FROM chart_bars"
            " WHERE symbol = :s AND tf = :tf"
        )
        args: dict[str, Any] = {"s": self.symbol, "tf": tf, "n": limit}
        if before is not None:
            q += " AND open_time < :b"
            args["b"] = datetime.fromtimestamp(before, UTC)
        if since is not None:
            q += " AND open_time >= :a"
            args["a"] = datetime.fromtimestamp(since, UTC)
        q += " ORDER BY open_time DESC LIMIT :n"
        with self.db.connect() as c:
            rows = c.execute(text(q), args).all()
        return [
            {"t": int(r[0].timestamp()), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
             "c": float(r[4]), "v": float(r[5])}
            for r in reversed(rows)
        ]

    # ---- what the chart asks for -----------------------------------------------------------
    def tail(self, tf: str, limit: int) -> list[Bar]:
        """The newest `limit` bars, the forming one last (re-read from Tabdeal every TAIL_TTL_S)."""
        now = self._clock()
        with self._lock:
            hit = self._tail.get(tf)
            if hit is not None and now - hit[0] < TAIL_TTL_S and len(hit[1]) >= limit:
                return hit[1][-limit:]
        step = SECONDS[tf]
        end = int(now) + step
        bars = self.fetch(tf, end - (limit + 2) * step, end)
        self._store(tf, bars)
        with self._lock:
            self._tail[tf] = (now, bars)
        return bars[-limit:]

    def older(self, tf: str, before: int, limit: int) -> tuple[list[Bar], bool]:
        """`limit` bars before `before` (oldest first) and whether older ones exist."""
        first = self._first.get(tf)
        if first is not None and before <= first:
            return [], False
        have = self._stored(tf, before, None, limit)
        contiguous = len(have) == limit and all(
            b["t"] - a["t"] <= SECONDS[tf] * 3 for a, b in zip(have, have[1:], strict=False)
        ) and before - have[-1]["t"] <= SECONDS[tf] * 3
        if not contiguous:
            step = SECONDS[tf]
            start = before - limit * step
            got = self.fetch(tf, start, before)
            self._store(tf, got)
            if len(got) < limit // 2:  # the market's start (listing) is inside this page
                self._first[tf] = got[0]["t"] if got else before
            have = got[-limit:]
        more = self._first.get(tf) is None or (bool(have) and have[0]["t"] > self._first[tf])
        return have, more

    def since(self, tf: str, since: int, limit: int) -> list[Candle]:
        """Closed bars from `since` (or the newest `limit`) as Candles, for the overlays."""
        self.tail(tf, 3)  # the newest closed bars are stored
        return [
            Candle(datetime.fromtimestamp(b["t"], UTC), Decimal(str(b["o"])), Decimal(str(b["h"])),
                   Decimal(str(b["l"])), Decimal(str(b["c"])), Decimal(str(b["v"])), None)
            for b in self._stored(tf, None, since, limit)
        ]
