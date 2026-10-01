"""Canonical M1 candles from raw trades (V5.1 B05, B25; V5.2 B31; V5.3 B34).

- Buckets use the exchange trade timestamp, aligned to UTC epoch minutes.
- A minute finalizes once the receive clock passes bucket_end + grace (2 s).
- A trade for an already-finalized minute is late: it is reported, never applied.
- Feed coverage is explicit: the collector reports gaps and a "healthy until" watermark.
  A minute is DATA_GAP if any gap overlaps it or healthy coverage is not confirmed
  through its end. DATA_GAP minutes are never synthesized.
- A fully covered minute with zero trades is SYNTHETIC_NO_TRADE:
  O=H=L=C=anchor, volume 0, trade_count 0, synthetic=True.
- B34: a pre-gap price is never carried across a DATA_GAP. The anchor is re-established
  only by the first genuine post-gap trade (a trade after the gap's end, while coverage is
  healthy), then follows the last valid post-gap price. A zero-trade minute without an
  anchor is UNANCHORED (treated as a gap downstream); nothing is synthesized.
- TradeCount is the raw number of distinct trades.
- V5.6 gap repair: while a coverage gap may still be repaired, minutes overlapping it are
  HELD (not finalized). Recovered trades are then added like live ones and the repaired
  interval counts as covered, so a repaired candle is built by exactly the same code as a
  live one and tagged REPAIRED_TABDEAL. If repair fails the hold is released and those
  minutes finalize as DATA_GAP, exactly as before.
- V5.8 three-tier recovery: a minute the WS feeds and the recent-trades window could not
  prove is built from a VALIDATED Tabdeal chart-history bar (`history`) when one was accepted
  for it: TABDEAL_HISTORY_REPAIRED, repair type CANDLE_HISTORY_REPAIR - OHLCV only, trade
  count UNKNOWN, no intrabar order. Only when every tier fails is the minute DATA_GAP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from sp2l.core.types import Candle

MINUTE = timedelta(minutes=1)
DEFAULT_GRACE = timedelta(milliseconds=2000)


def floor_minute(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware UTC")
    ts = ts.astimezone(UTC)
    return ts.replace(second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class Trade:
    trade_id: str
    exch_ts: datetime
    recv_ts: datetime
    price: Decimal
    qty: Decimal
    taker_side: str | None = None  # "BUY" / "SELL" when the exchange reports it
    # lineage (V5.6): the exact exchange payload, and where the trade came from
    raw: str | None = field(default=None, compare=False, repr=False)
    source: str = field(default="WS", compare=False)  # WS | REPAIR_REST

    def __post_init__(self) -> None:
        if self.price <= 0 or self.qty <= 0:
            raise ValueError(f"trade {self.trade_id}: price and qty must be positive")


class Quality(StrEnum):
    """Candle lineage (V5.6 market-data integrity)."""

    LIVE_RECONCILED = "LIVE_RECONCILED"  # WS + continuous REST reconciliation (V5.7)
    # V5.8 names. EXACT_RAW_REPAIR: every trade recovered from recent-trades (exact identity)
    RECENT_TRADES_REPAIRED = "RECENT_TRADES_REPAIRED"
    # CANDLE_HISTORY_REPAIR: validated Tabdeal chart bar (OHLCV only, trade count UNKNOWN)
    TABDEAL_HISTORY_REPAIRED = "TABDEAL_HISTORY_REPAIRED"
    REST_REPAIRED = "REST_REPAIRED"  # V5.7 name of RECENT_TRADES_REPAIRED (legacy rows)
    LIVE_WS_ONLY = "LIVE_WS_ONLY"  # REST unavailable before the deadline: WS only (V5.7)
    SYNTHETIC_NO_TRADE = "SYNTHETIC_NO_TRADE"
    # legacy lineage of rows written before V5.7
    LIVE_PROVEN_RAW = "LIVE_PROVEN_RAW"  # V5.6: WS only, REST reconciliation not yet running
    REPAIRED_TABDEAL = "REPAIRED_TABDEAL"  # V5.6 on-reconnect REST repair
    CONFLICTED = "CONFLICTED"  # recorded under a known-deficient rule (see revisions)


EXACT_RAW_REPAIR = "EXACT_RAW_REPAIR"
CANDLE_HISTORY_REPAIR = "CANDLE_HISTORY_REPAIR"
EXACT_REPAIRS = frozenset(
    {Quality.RECENT_TRADES_REPAIRED, Quality.REST_REPAIRED, Quality.REPAIRED_TABDEAL}
)


class M1Status(StrEnum):
    OK = "OK"
    SYNTHETIC_NO_TRADE = "SYNTHETIC_NO_TRADE"
    DATA_GAP = "DATA_GAP"
    UNANCHORED = "UNANCHORED"  # covered, no trades, no post-gap anchor yet (B34)


@dataclass(frozen=True, slots=True)
class M1Result:
    open_time: datetime
    status: M1Status
    candle: Candle | None
    quality: Quality | None = None  # None for DATA_GAP / UNANCHORED (no candle)
    lineage: dict[str, object] | None = field(default=None, compare=False, repr=False)

    @property
    def repaired(self) -> bool:
        """Rebuilt after a coverage gap (history, never a live decision point)."""
        return self.quality in EXACT_REPAIRS or self.quality is Quality.TABDEAL_HISTORY_REPAIRED

    @property
    def repair_type(self) -> str | None:
        """V5.8: EXACT_RAW_REPAIR (trades and their order recovered: causal replay allowed) or
        CANDLE_HISTORY_REPAIR (OHLCV only: price history, never fills or intrabar order)."""
        if self.quality in EXACT_REPAIRS:
            return EXACT_RAW_REPAIR
        if self.quality is Quality.TABDEAL_HISTORY_REPAIRED:
            return CANDLE_HISTORY_REPAIR
        return None

    @property
    def tradeable(self) -> bool:
        """Eligible for P-Gap / Spike / Origin / sequence continuation (B31)."""
        return self.status is M1Status.OK and self.candle is not None


@dataclass(slots=True)
class _Bucket:
    trades: list[tuple[datetime, int, Trade]] = field(default_factory=list)


class CoverageOracle(Protocol):
    """Merged proven coverage (B46): replaces the single watermark + gap list."""

    def covers(self, start: datetime, end: datetime) -> bool: ...
    def component_start(self, t: datetime) -> datetime | None: ...


class M1Builder:
    def __init__(
        self,
        start: datetime,
        grace: timedelta = DEFAULT_GRACE,
        coverage: CoverageOracle | None = None,
    ) -> None:
        self._next = floor_minute(start)  # first minute not yet finalized
        self._grace = grace
        self._buckets: dict[datetime, _Bucket] = {}
        self._seen_ids: dict[datetime, set[str]] = {}
        self._gaps: list[tuple[datetime, datetime]] = []
        self._healthy_until: datetime | None = None
        self._ref_close: Decimal | None = None
        self._coverage = coverage
        self._seq = 0
        self.late_trades: list[Trade] = []
        self.hold_from: datetime | None = None  # V5.6: do not finalize minutes past this
        self._repaired: list[tuple[datetime, datetime]] = []
        # V5.7 continuous REST reconciliation: REST coverage oracle and trade provenance
        self.rest_cover: CoverageOracle | None = None
        self.provenance: dict[str, set[str]] | None = None
        # V5.8 tier 3: validated chart-history candles for minutes no live source proved
        self.history: dict[datetime, Candle] = {}

    @property
    def next_open_minute(self) -> datetime:
        return self._next

    def add_trade(self, trade: Trade) -> bool:
        """Buffer a trade. Returns False (and records it) if its minute already finalized."""
        minute = floor_minute(trade.exch_ts)
        if minute < self._next:
            self.late_trades.append(trade)
            return False
        ids = self._seen_ids.setdefault(minute, set())
        if trade.trade_id in ids:
            return True  # duplicate delivery of the same trade; count once
        ids.add(trade.trade_id)
        self._seq += 1
        self._buckets.setdefault(minute, _Bucket()).trades.append((trade.exch_ts, self._seq, trade))
        return True

    def mark_healthy_until(self, ts: datetime) -> None:
        """The feed was connected and complete up to `ts` (exchange time)."""
        if self._healthy_until is None or ts > self._healthy_until:
            self._healthy_until = ts

    def mark_repaired(self, start: datetime, end: datetime) -> None:
        """Trades in [start, end) were recovered from Tabdeal (V5.6): lineage only."""
        self._repaired.append((start, end))

    def _rest_covered(self, minute: datetime) -> bool | None:
        if self.rest_cover is None:
            return None
        return self.rest_cover.covers(minute, minute + MINUTE)

    def _quality(self, minute: datetime, synthetic: bool) -> Quality:
        end = minute + MINUTE
        rest = self._rest_covered(minute)
        if any(a < end and b > minute for a, b in self._repaired):
            return Quality.RECENT_TRADES_REPAIRED if rest is not None else Quality.REPAIRED_TABDEAL
        if synthetic:
            return Quality.SYNTHETIC_NO_TRADE
        if rest is None:
            return Quality.LIVE_PROVEN_RAW
        return Quality.LIVE_RECONCILED if rest else Quality.LIVE_WS_ONLY

    def _lineage(self, trades: list[Trade]) -> dict[str, object] | None:
        if self.provenance is None:
            return None
        prov = {t.trade_id: sorted(self.provenance.get(t.trade_id, {t.source})) for t in trades}

        def n(src: str) -> int:
            return sum(1 for v in prov.values() if src in v)

        return {
            "ws_a": n("A"),
            "ws_b": n("B"),
            "rest": n("REST"),
            "rest_only": sum(1 for v in prov.values() if v == ["REST"]),
            "sources": {k: ",".join(v) for k, v in prov.items()},
        }

    def add_coverage_gap(self, start: datetime, end: datetime) -> None:
        """Feed was not connected (or lost data) during [start, end)."""
        if end <= start:
            raise ValueError("gap end must be after start")
        self._gaps.append((start, end))

    def _data_gap(self, minute: datetime) -> bool:
        end = minute + MINUTE
        if self._coverage is not None:
            return not self._coverage.covers(minute, end)
        if self._healthy_until is None or self._healthy_until < end:
            return True
        return any(g0 < end and g1 > minute for g0, g1 in self._gaps)

    def _post_gap_anchor(self, minute: datetime, trades: list[Trade]) -> Decimal | None:
        """Last genuine trade of a gap minute that happened after the gap, under coverage."""
        end = minute + MINUTE
        if self._coverage is not None:
            return next(
                (
                    t.price
                    for t in reversed(trades)
                    if (cs := self._coverage.component_start(t.exch_ts)) is not None
                    and self._coverage.covers(cs, end)
                    and t.exch_ts >= cs
                ),
                None,
            )
        gap_end = max((g1 for g0, g1 in self._gaps if g0 < end and g1 > minute), default=minute)
        healthy = self._healthy_until
        valid = [
            t for t in trades if t.exch_ts >= gap_end and (healthy is None or t.exch_ts < healthy)
        ]
        return valid[-1].price if valid else None

    def advance(self, now_recv: datetime) -> list[M1Result]:
        """Finalize every minute whose end + grace <= now_recv, in order."""
        out: list[M1Result] = []
        while self._next + MINUTE + self._grace <= now_recv:
            if self.hold_from is not None and self._next + MINUTE > self.hold_from:
                break  # V5.6: a repair may still complete this minute
            minute = self._next
            bucket = self._buckets.pop(minute, None)
            self._seen_ids.pop(minute, None)
            trades = (
                [t for _, _, t in sorted(bucket.trades, key=lambda x: (x[0], x[1]))]
                if bucket is not None
                else []
            )
            hist = self.history.pop(minute, None)
            if self._data_gap(minute) and hist is not None:
                # V5.8 tier 3: the whole minute from the validated Tabdeal chart bar
                self._ref_close = hist.close
                out.append(
                    M1Result(
                        minute,
                        M1Status.OK,
                        hist,
                        Quality.TABDEAL_HISTORY_REPAIRED,
                        {
                            "repair_type": CANDLE_HISTORY_REPAIR,
                            "source": "TABDEAL_CHART_HISTORY",
                            "unknown_fields": ["trade_count", "intrabar_order"],
                            "live_trades_seen": len(trades),
                        },
                    )
                )
            elif self._data_gap(minute):
                self._ref_close = self._post_gap_anchor(minute, trades)
                out.append(M1Result(minute, M1Status.DATA_GAP, None))
            elif not trades and self._rest_covered(minute) is False:
                # V5.7: zero trades is only proven by REST reconciliation, never by WS alone
                self._ref_close = None
                out.append(M1Result(minute, M1Status.DATA_GAP, None))
            elif not trades:
                if self._ref_close is None:
                    out.append(M1Result(minute, M1Status.UNANCHORED, None))  # B34
                else:
                    p = self._ref_close
                    candle = Candle(minute, p, p, p, p, Decimal(0), 0, synthetic=True)
                    out.append(
                        M1Result(
                            minute,
                            M1Status.SYNTHETIC_NO_TRADE,
                            candle,
                            self._quality(minute, True),
                        )
                    )
            else:
                prices = [t.price for t in trades]
                vol = Decimal(0)
                for t in trades:
                    vol += t.qty
                candle = Candle(
                    minute, prices[0], max(prices), min(prices), prices[-1], vol, len(trades)
                )
                self._ref_close = candle.close
                out.append(
                    M1Result(
                        minute,
                        M1Status.OK,
                        candle,
                        self._quality(minute, False),
                        self._lineage(trades),
                    )
                )
            self._next = minute + MINUTE
        self._gaps = [g for g in self._gaps if g[1] > self._next]
        self._repaired = [g for g in self._repaired if g[1] > self._next]
        for stale in [m for m in self.history if m < self._next]:
            del self.history[stale]
        return out
