"""System B parameters (defaults = the pre-declared rules in docs/TREND_STRATEGY.md)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields
from typing import Any

VERSION = "TREND-1.0"
SIZINGS = ("risk", "full")


@dataclass(frozen=True, slots=True)
class TrendParams:
    # entry: the daily close above the highest high of the previous `entry_len` days
    entry_len: int = 20
    # exit: the daily close below the lowest low of the previous `exit_len` days (next open)
    exit_len: int = 10
    # initial stop: `stop_atr` x ATR(`atr_len`, Wilder) below the fill, checked on every bar
    atr_len: int = 20
    stop_atr: float = 2.0
    # regime filter: enter only while the close is above SMA(`regime_ma`); 0 = off
    regime_ma: int = 0
    # sizing: risk = `risk_pct` of equity lost at the initial stop / full = all cash in
    sizing: str = "risk"
    risk_pct: float = 0.01
    max_exposure: float = 1.0  # notional / equity at most (1 = spot, no leverage)
    # pyramiding: add a unit each `add_atr` x N above the last fill, up to `max_units`
    # (N = ATR at the first entry); every unit's stop then moves to the last fill - stop_atr x N
    max_units: int = 1
    add_atr: float = 1.0
    initial_equity: float = 10_000.0
    # futures: shorts mirror the long rules (close below the entry_len low, stop above, exit on
    # a close above the exit_len high); cross-margin maintenance rate for the liquidation check
    allow_short: bool = False
    maint_margin: float = 0.005
    # yield per year on positive idle cash (e.g. a stablecoin savings rate); 0 = none
    cash_yield: float = 0.0

    def __post_init__(self) -> None:
        if self.entry_len < 1 or self.exit_len < 1 or self.atr_len < 1:
            raise ValueError("entry_len, exit_len and atr_len must be >= 1")
        if self.stop_atr <= 0 or self.add_atr <= 0:
            raise ValueError("stop_atr and add_atr must be > 0")
        if self.regime_ma < 0 or self.max_units < 1:
            raise ValueError("regime_ma must be >= 0 and max_units >= 1")
        if self.sizing not in SIZINGS:
            raise ValueError(f"sizing must be one of {SIZINGS}")
        if not 0 < self.risk_pct < 1 or not 0 < self.max_exposure <= 10:
            raise ValueError("risk_pct must be in (0, 1) and max_exposure in (0, 10]")
        if not 0 <= self.maint_margin < 1:
            raise ValueError("maint_margin must be in [0, 1)")

    @property
    def warmup(self) -> int:
        """Bars needed before the first signal can be computed."""
        return max(self.entry_len, self.exit_len, self.atr_len, self.regime_ma)

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def digest(self) -> str:
        return hashlib.sha256(repr(sorted(self.as_dict().items())).encode()).hexdigest()[:16]

    @classmethod
    def from_mapping(cls, m: dict[str, Any]) -> TrendParams:
        known = {f.name: f for f in fields(cls)}
        unknown = sorted(set(m) - set(known))
        if unknown:
            raise ValueError(f"trend: unknown parameter(s) {', '.join(unknown)}")
        base = cls()
        kw: dict[str, Any] = {}
        for k, v in m.items():
            cur = getattr(base, k)
            if isinstance(cur, bool):  # before int: a bool is an int in Python
                kw[k] = (
                    v.strip().lower() in ("1", "true", "yes", "on")
                    if isinstance(v, str)
                    else bool(v)
                )
            else:
                kw[k] = type(cur)(v) if not isinstance(cur, str) else str(v)
        return cls(**kw)
