"""Shared value types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True, slots=True)
class Candle:
    """A finalized OHLCV candle. `open_time` is the bucket start (UTC)."""

    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal(0)
    # V5.8: None = UNKNOWN. A CANDLE_HISTORY_REPAIR bar (Tabdeal chart history) carries OHLCV
    # but no trade count; it is never invented (the Liquidity gate stays UNKNOWN on it).
    trade_count: int | None = 0
    # V5.2 B31: a no-trade candle built while feed coverage was healthy (O=H=L=C=prev close).
    # Synthetic candles keep time/indicator continuity but are ineligible for P-Gap, Spike
    # confirmation, SpikeOrigin and directional-sequence continuation.
    synthetic: bool = False

    def __post_init__(self) -> None:
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(f"inconsistent OHLC at {self.open_time.isoformat()}")
        if self.volume < 0 or (self.trade_count is not None and self.trade_count < 0):
            raise ValueError(f"negative volume/trade_count at {self.open_time.isoformat()}")
        if self.synthetic and (self.volume != 0 or self.trade_count != 0):
            raise ValueError(f"synthetic candle with trades at {self.open_time.isoformat()}")
