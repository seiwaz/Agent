"""Exact trade-level repair of a merged coverage gap (V5.6 market-data integrity).

Source: Tabdeal's public `recent-trades` (the last 50 trades, see tabdeal_public.py).
A repair is accepted only if it reproduces EXACTLY what the live stream would have given;
anything uncertain fails closed (UNRECOVERED -> the existing DATA_GAP rules apply):

1. Completeness: the REST window must reach back before the gap (its oldest trade was
   recorded before gap_start), so every trade of the gap is in it.
2. Time model: REST `created` minus the WebSocket `updated` time of the same trade lies in
   [delta_min, delta_max] (measured over 3,527 trades: -25 ms .. +427 ms, p50 33 ms; bounds
   configured at -100 / +500 ms). Every REST trade that also
   arrived live must match one live trade (same price and amount) within that bound;
   any violation of the model on this very window fails the repair.
3. Unmatched REST trades in [gap_start, gap_end + delta_max) are the missing trades. Their
   WebSocket time is only known to lie in [created - delta_max, created] (clipped to the gap);
   if that interval crosses a minute boundary the minute is ambiguous -> fail.
4. Per affected minute the first and last trade (open/close) must be unambiguous under the
   same uncertainty (candidates that could be first/last must all have the same price).

Volume, high, low and trade count are order-independent, so 1-4 make the repaired candle
identical to the uninterrupted one. OHLC-only sources (the chart history) cannot pass these
checks and are never used here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sp2l.marketdata.m1_builder import Trade, floor_minute
from sp2l.marketdata.tabdeal_public import RestTrade

MATCH_WINDOW = timedelta(seconds=1)  # never treat a live trade further away as the same trade


@dataclass(frozen=True, slots=True)
class KnownTrade:
    """A trade already received live (proven or in_gap), in WebSocket time."""

    exch_ts: datetime
    price: Decimal
    qty: Decimal


@dataclass
class RepairOutcome:
    status: str  # REPAIRED | UNRECOVERED
    method: str  # REST_RECENT_TRADES | NONE
    reason: str | None = None
    trades: list[Trade] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def repaired(self) -> bool:
        return self.status == "REPAIRED"


def _fail(reason: str, **detail: Any) -> RepairOutcome:
    return RepairOutcome("UNRECOVERED", "REST_RECENT_TRADES", reason, [], detail)


def repair_gap(
    gap_start: datetime,
    gap_end: datetime,
    rest: list[RestTrade],
    known: list[KnownTrade],
    *,
    delta_max: timedelta,
    fetched_at: datetime,
    delta_min: timedelta = timedelta(milliseconds=-100),
) -> RepairOutcome:
    if not rest:
        return _fail("NO_REST_TRADES")
    rest = sorted(rest, key=lambda r: r.created)
    if fetched_at < gap_end + delta_max:
        return _fail("FETCHED_TOO_EARLY", fetched_at=fetched_at.isoformat())
    if rest[0].created >= gap_start:
        return _fail(
            "REST_WINDOW_TOO_SHORT",
            oldest=rest[0].created.isoformat(),
            gap_start=gap_start.isoformat(),
        )
    window = [r for r in rest if gap_start + delta_min <= r.created < gap_end + delta_max]
    # (2) match against live receipts; the time model must hold on this window
    pool = sorted(known, key=lambda k: k.exch_ts)
    used = [False] * len(pool)
    missing: list[RestTrade] = []
    matched = 0
    for r in window:
        best = None
        for i, k in enumerate(pool):
            if used[i] or k.price != r.price or k.qty != r.qty:
                continue
            d = r.created - k.exch_ts
            if -MATCH_WINDOW <= d <= MATCH_WINDOW and (
                best is None or abs(d) < abs(r.created - pool[best].exch_ts)
            ):
                best = i
        if best is None:
            missing.append(r)
            continue
        d = r.created - pool[best].exch_ts
        if not delta_min <= d <= delta_max:
            return _fail("TIME_MODEL_VIOLATION", offset_s=d.total_seconds())
        used[best] = True
        matched += 1
    # (3) place each missing trade in WebSocket time, clipped to the gap
    placed: list[tuple[datetime, datetime, RestTrade]] = []
    for r in missing:
        lo = max(r.created - delta_max, gap_start)
        hi = min(r.created - delta_min, gap_end - timedelta(microseconds=1))
        if hi < lo:
            return _fail("OUTSIDE_GAP", created=r.created.isoformat())
        if floor_minute(lo) != floor_minute(hi):
            return _fail("AMBIGUOUS_MINUTE", created=r.created.isoformat())
        placed.append((lo, hi, r))
    # (4) open/close of every affected minute must be unambiguous
    minutes = sorted({floor_minute(lo) for lo, _, _ in placed})
    for m in minutes:
        end = m + timedelta(minutes=1)
        spans: list[tuple[datetime, datetime, Decimal]] = [
            (k.exch_ts, k.exch_ts, k.price) for k in known if m <= k.exch_ts < end
        ]
        spans += [(lo, hi, r.price) for lo, hi, r in placed if m <= lo < end]
        first_bound = min(hi for _, hi, _ in spans)
        last_bound = max(lo for lo, _, _ in spans)
        firsts = {p for lo, _, p in spans if lo <= first_bound}
        lasts = {p for _, hi, p in spans if hi >= last_bound}
        if len(firsts) > 1:
            return _fail("AMBIGUOUS_OPEN", minute=m.isoformat())
        if len(lasts) > 1:
            return _fail("AMBIGUOUS_CLOSE", minute=m.isoformat())
    trades: list[Trade] = []
    counts: dict[tuple[str, str, str], int] = {}
    for lo, hi, r in placed:
        key = (r.created.isoformat(), str(r.price), str(r.qty))
        counts[key] = counts.get(key, 0) + 1
        ts = lo + (hi - lo) / 2  # any point in [lo, hi] gives the same candle (checked above)
        trades.append(
            Trade(
                trade_id=f"R:{key[0]}:{key[1]}:{key[2]}#{counts[key]}",
                exch_ts=ts,
                recv_ts=fetched_at,
                price=r.price,
                qty=r.qty,
                taker_side=r.side,
                raw=_raw_json(r.raw),
                source="REPAIR_REST",
            )
        )
    return RepairOutcome(
        "REPAIRED",
        "REST_RECENT_TRADES",
        None,
        trades,
        {
            "rest_trades": len(rest),
            "window": len(window),
            "matched_live": matched,
            "recovered": len(trades),
            "minutes": [m.isoformat() for m in minutes],
            "oldest_rest": rest[0].created.isoformat(),
        },
    )


def _raw_json(d: dict[str, Any]) -> str:
    import json

    return json.dumps(d, sort_keys=True)
