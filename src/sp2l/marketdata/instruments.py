"""Price tick and quantity step of every dashboard market.

The config's `instruments` win; any other market takes them from Tabdeal's futures exchangeInfo
(`pricePrecision` / `quantityPrecision`, all markets in one public call), fetched in the
background when the API starts and refreshed every REFRESH_S. A market whose precision is not
known yet is absent from the mapping, so nothing is ever sent to the exchange with a guessed
precision (trading refuses it until exchangeInfo has answered).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from decimal import Decimal
from typing import Any

log = logging.getLogger("sp2l.instruments")
REFRESH_S = 3600.0
RETRY_S = 60.0


def exchange_info() -> Any:
    from sp2l.marketdata.tabdeal_rest import get_json

    return get_json("/r/fapi/v1/exchangeInfo", timeout=15.0)


def parse(payload: Any) -> dict[str, dict[str, Any]]:
    """exchangeInfo -> {BTCUSDT: {tick, step, status, market}}."""
    out: dict[str, dict[str, Any]] = {}
    rows = payload.get("symbols", []) if isinstance(payload, dict) else []
    for r in rows:
        try:
            market = str(r["symbol"])
            tick = Decimal(1).scaleb(-int(r["pricePrecision"]))
            step = Decimal(1).scaleb(-int(r["quantityPrecision"]))
        except (KeyError, TypeError, ValueError):
            continue
        out[market.replace("_", "")] = {"tick": tick, "step": step, "market": market,
                                         "status": str(r.get("status", ""))}
    return out


class Instruments(Mapping[str, tuple[Decimal, Decimal]]):
    """{symbol: (tick, step)} of the dashboard markets whose precision is known."""

    def __init__(
        self,
        markets: list[str],
        configured: dict[str, tuple[Decimal, Decimal]],
        fetch: Callable[[], Any] | None = exchange_info,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.markets, self.configured = list(markets), dict(configured)
        self.fetch, self.clock = fetch, clock
        self._lock = threading.Lock()
        self._info: dict[str, dict[str, Any]] = {}
        self.fetched_at: float | None = None
        self.problem: str | None = None

    def refresh(self) -> bool:
        if self.fetch is None:
            return False
        try:
            info = parse(self.fetch())
        except Exception as e:  # unreachable / unexpected answer: keep what we have
            self.problem = f"Tabdeal exchangeInfo: {type(e).__name__}: {e}"[:300]
            log.warning("%s", self.problem)
            return False
        with self._lock:
            self._info, self.fetched_at, self.problem = info, self.clock(), None
        missing = [m for m in self.markets if m not in info and m not in self.configured]
        if missing:
            log.warning("not listed in Tabdeal futures exchangeInfo: %s", ", ".join(missing))
        return True

    def run(self, stop: threading.Event) -> None:
        """Refresh now, then every REFRESH_S (every RETRY_S while it fails)."""
        while not stop.is_set():
            ok = self.refresh()
            stop.wait(REFRESH_S if ok else RETRY_S)

    # ---- the mapping ----------------------------------------------------------------------------
    def __getitem__(self, symbol: str) -> tuple[Decimal, Decimal]:
        if symbol not in self.markets:
            raise KeyError(symbol)
        if symbol in self.configured:
            return self.configured[symbol]
        with self._lock:
            i = self._info.get(symbol)
        if i is None:
            raise KeyError(symbol)
        return i["tick"], i["step"]

    def __iter__(self) -> Iterator[str]:
        return (m for m in self.markets if m in self)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def describe(self, symbol: str) -> dict[str, Any]:
        """What the dashboard shows about a market's precision and listing."""
        with self._lock:
            i = self._info.get(symbol)
        listed = None if self.fetched_at is None else i is not None
        known = symbol in self
        tick, step = self[symbol] if known else (None, None)
        return {"tick": tick, "step": step, "known": known, "listed": listed,
                "status": i["status"] if i else None,
                "source": "config" if symbol in self.configured else "exchangeInfo" if i else None}
