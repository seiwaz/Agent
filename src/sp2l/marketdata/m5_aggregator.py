"""M5 from canonical M1 (MKT-05; V5.2 B31, V5.3 B33/B34 data-quality inheritance).

An M5 bucket (UTC-epoch aligned) is:
- DATA_GAP if any of its minutes is DATA_GAP or UNANCHORED (nothing is synthesized);
- otherwise OK, aggregated from all five minutes, real and synthetic no-trade, with the
  quality fields synthetic_m1_count, synthetic_fraction and real_trade_count (B33). If all
  five are synthetic the M5 candle is synthetic too (never a pivot centre).
DATA_GAP breaks the M5 series.
V5.8: a bucket containing a CANDLE_HISTORY_REPAIR minute is TABDEAL_HISTORY_REPAIRED and its
trade count is UNKNOWN (None) - price continuity is restored, liquidity data is not invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from sp2l.core.types import Candle
from sp2l.marketdata.m1_builder import M1Result, M1Status, Quality

M5_STEP = timedelta(minutes=5)


def m5_bucket(ts: datetime) -> datetime:
    ts = ts.astimezone(UTC)
    return ts.replace(minute=ts.minute - ts.minute % 5, second=0, microsecond=0)


class M5Status(StrEnum):
    OK = "OK"
    DATA_GAP = "DATA_GAP"
    MISSING = "MISSING"  # internal: a silent hole in the M5 sequence


@dataclass(frozen=True, slots=True)
class M5Result:
    open_time: datetime
    status: M5Status
    candle: Candle | None
    synthetic_m1_count: int = 0
    repaired_m1_count: int = 0  # V5.6 lineage: minutes containing recovered trades
    history_m1_count: int = 0  # V5.8: minutes rebuilt from Tabdeal chart history

    @property
    def quality(self) -> Quality | None:
        if self.candle is None:
            return None
        if self.history_m1_count:
            return Quality.TABDEAL_HISTORY_REPAIRED
        if self.repaired_m1_count:
            return Quality.RECENT_TRADES_REPAIRED
        return Quality.SYNTHETIC_NO_TRADE if self.candle.synthetic else Quality.LIVE_PROVEN_RAW

    @property
    def close_time(self) -> datetime:
        return self.open_time + M5_STEP

    @property
    def synthetic_fraction(self) -> Fraction:
        return Fraction(self.synthetic_m1_count, 5)

    @property
    def real_trade_count(self) -> int | None:
        return self.candle.trade_count if self.candle is not None else 0


class M5Aggregator:
    def __init__(self) -> None:
        self._bucket: datetime | None = None
        self._minutes: list[M1Result] = []

    def add(self, m1: M1Result) -> M5Result | None:
        """Feed M1 results in order. Returns the M5 result when a bucket's 5th minute arrives."""
        bucket = m5_bucket(m1.open_time)
        if self._bucket is None:
            if m1.open_time != bucket:
                return None  # start on a bucket boundary; a partial first bucket is skipped
            self._bucket = bucket
        if bucket != self._bucket:
            raise ValueError(f"M1 {m1.open_time} out of order for bucket {self._bucket}")
        expected = self._bucket + timedelta(minutes=len(self._minutes))
        if m1.open_time != expected:
            raise ValueError(f"M1 {m1.open_time} out of order, expected {expected}")
        self._minutes.append(m1)
        if len(self._minutes) < 5:
            return None
        result = self._aggregate(self._bucket, self._minutes)
        self._bucket = self._bucket + M5_STEP
        self._minutes = []
        return result

    @staticmethod
    def _aggregate(open_time: datetime, minutes: list[M1Result]) -> M5Result:
        if any(
            m.status not in (M1Status.OK, M1Status.SYNTHETIC_NO_TRADE) or m.candle is None
            for m in minutes
        ):
            return M5Result(open_time, M5Status.DATA_GAP, None)
        cs = [m.candle for m in minutes if m.candle is not None]
        synthetic = sum(1 for c in cs if c.synthetic)
        vol = Decimal(0)
        count: int | None = 0
        for c in cs:
            vol += c.volume
            count = None if count is None or c.trade_count is None else count + c.trade_count
        candle = Candle(
            open_time,
            cs[0].open,
            max(c.high for c in cs),
            min(c.low for c in cs),
            cs[-1].close,
            vol,
            count,
            synthetic=synthetic == len(cs),
        )
        history = sum(1 for m in minutes if m.quality is Quality.TABDEAL_HISTORY_REPAIRED)
        return M5Result(
            open_time,
            M5Status.OK,
            candle,
            synthetic,
            sum(1 for m in minutes if m.repaired) - history,
            history,
        )
