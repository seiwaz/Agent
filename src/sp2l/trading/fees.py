"""Tabdeal's current futures (special-margin) commission, from its public commission table
(`get-commission-levels-v2`, the level by 30-day volume), refreshed every REFRESH_S; the
config's `costs` while Tabdeal does not answer. Used for the break-even line of open trades."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

REFRESH_S = 6 * 3600.0
RETRY_S = 300.0


def commission_levels() -> Any:
    from sp2l.marketdata.tabdeal_public import _get

    return _get("/get-commission-levels-v2/", {}, 10.0)


def pick_level(payload: Any, volume_usdt: float) -> dict[str, Any]:
    """The special-margin level whose 30-day volume range holds `volume_usdt`."""
    levels = payload["special_margin_commission_levels"] if isinstance(payload, dict) else []
    vol = Decimal(str(volume_usdt))
    for lv in levels:
        if Decimal(str(lv["from_volume"])) <= vol < Decimal(str(lv["to_volume"])):
            return {"maker": float(lv["maker_commission_percent"]),
                    "taker": float(lv["taker_commission_percent"]),
                    "level": str(lv.get("name") or lv.get("level") or "")}
    raise ValueError(f"no special-margin commission level for a 30-day volume of {volume_usdt}")


class Fees:
    def __init__(self, maker: float, taker: float, volume_usdt: float = 0.0,
                 slippage: float = 0.0,
                 fetch: Callable[[], Any] | None = commission_levels,
                 clock: Callable[[], float] = time.time) -> None:
        # slippage: how much worse than its price a market exit fills (config's
        # costs.slippage_allowance; Tabdeal publishes none), added to every answer
        self.slippage = slippage
        self.fallback = {"maker": maker, "taker": taker, "level": None, "source": "config"}
        self.volume, self.fetch, self.clock = volume_usdt, fetch, clock
        self._lock = threading.Lock()
        self._value: dict[str, Any] | None = None
        self._until = 0.0
        self._busy = False

    def refresh(self) -> dict[str, Any]:
        """Read Tabdeal's table now (blocking); the config's fees while it fails."""
        now = self.clock()
        try:
            if self.fetch is None:
                raise ValueError("no fetch")
            v = {**pick_level(self.fetch(), self.volume), "source": "tabdeal"}
            until = now + REFRESH_S
        except (OSError, ValueError, KeyError, TypeError) as e:
            v = {**self.fallback, "problem": f"{type(e).__name__}: {e}"[:200]}
            until = now + RETRY_S
        with self._lock:
            self._value, self._until, self._busy = (
                {**v, "slippage": self.slippage, "fetched_at": now}, until, False)
            return self._value

    def get(self) -> dict[str, Any]:
        """The fees known now, never waiting for Tabdeal: a stale value is refreshed in the
        background (until the first answer: the config's fees)."""
        with self._lock:
            value, stale = self._value, self.clock() >= self._until
            start = stale and not self._busy
            if start:
                self._busy = True
        if start:
            threading.Thread(target=self.refresh, name="fees", daemon=True).start()
        return value if value is not None else {**self.fallback, "slippage": self.slippage,
                                                "fetched_at": None}
