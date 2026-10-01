"""V5.6 audited reconciliation of stored market data (never destructive).

Finding (2026-09-27 audit): collector runs before the fingerprint fix de-duplicated trades by
`sequence` alone. A sequence can carry several genuine fills, so every fill after the first
of a multi-fill sequence was dropped from raw_trades and from the candles (volume, trade
count and sometimes high/low/close too low). Only the last pre-fix run logged the dropped
fills (feed_conflicts fingerprints); earlier runs dropped them silently.

reconcile_history():
1. restores the dropped fills that the conflict log recorded (raw_trades source
   RECOVERED_CONFLICT_LOG; each distinct fingerprint once),
2. rebuilds the M1 candles of the affected minutes from raw_trades with the canonical rule,
3. marks EVERY candle recorded before the fix CONFLICTED (values of unlogged runs cannot be
   corrected from our own data), each change as a recorded candle revision,
4. rebuilds every dependent M5 bar from the canonical M1 bars.
Dry run by default.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, text

from sp2l.core.types import Candle
from sp2l.marketdata.m1_builder import Quality
from sp2l.persistence.market_store import MarketStore

REASON_DEDUP = "PRE_FIX_SEQUENCE_DEDUP_DROPPED_MULTIFILLS"
REASON_RECOVERED = "DROPPED_MULTIFILLS_RECOVERED_FROM_CONFLICT_LOG"


def fix_started_at(db: Engine) -> datetime | None:
    """Start of the first collector run that classified multi-fill sequences (the fix)."""
    with db.connect() as c:
        row = c.execute(
            text(
                "SELECT r.started_at FROM feed_conflicts f JOIN collector_runs r ON r.id ="
                " f.run_id WHERE f.first->>'classification' = 'MULTI_FILL_SAME_SEQUENCE'"
                " ORDER BY r.started_at LIMIT 1"
            )
        ).first()
    return row[0].astimezone(UTC) if row else None


def reconcile_history(db: Engine, symbol: str, *, apply: bool = False) -> dict[str, Any]:
    fix = fix_started_at(db)
    if fix is None:
        return {"status": "NOTHING_TO_DO", "reason": "fingerprint dedup never ran"}
    store = MarketStore(db, symbol)
    with db.connect() as c:
        dropped = c.execute(
            text(
                "SELECT DISTINCT other->'fingerprint' AS fp, min(ts) AS ts FROM feed_conflicts"
                " WHERE symbol = :s AND ts < :f GROUP BY 1"
            ),
            {"s": symbol, "f": fix},
        ).all()
        stored = {
            (r[0], r[1].astimezone(UTC), r[2], r[3])
            for r in c.execute(
                text(
                    "SELECT split_part(trade_id, ':', 1), exch_ts, price, qty FROM raw_trades"
                    " WHERE symbol = :s AND exch_ts < :f"
                ),
                {"s": symbol, "f": fix},
            )
        }
    restore = []
    for fp, ts in dropped:
        seq, ets, px, qty = fp[0], datetime.fromisoformat(fp[1]), Decimal(fp[2]), Decimal(fp[3])
        if (seq, ets, px, qty) not in stored:
            restore.append((seq, ets, px, qty, ts))
    report: dict[str, Any] = {
        "fix_started_at": fix.isoformat(),
        "dropped_fills_logged": len(dropped),
        "fills_to_restore": len(restore),
        "applied": apply,
    }
    if apply and restore:
        with db.begin() as c:
            for seq, ets, px, qty, ts in restore:
                c.execute(
                    text(
                        "INSERT INTO raw_trades (symbol, trade_id, exch_ts, recv_ts, price, qty,"
                        " late, source) VALUES (:s, :id, :e, :r, :p, :q, false,"
                        " 'RECOVERED_CONFLICT_LOG') ON CONFLICT DO NOTHING"
                    ),
                    {
                        "s": symbol,
                        "id": f"{seq}:{px}:{qty}#1",
                        "e": ets,
                        "r": ts,
                        "p": px,
                        "q": qty,
                    },
                )
    touched = {ets.replace(second=0, microsecond=0) for _, ets, _, _, _ in restore}
    with db.connect() as c:
        candles = c.execute(
            text(
                "SELECT open_time, open, high, low, close, volume, trade_count,"
                " synthetic_no_trade, quality FROM candles_1m WHERE symbol = :s AND"
                " open_time < :f ORDER BY open_time"
            ),
            {"s": symbol, "f": fix},
        ).all()
        trades = c.execute(
            text(
                "SELECT exch_ts, recv_ts, trade_id, price, qty FROM raw_trades WHERE symbol = :s"
                " AND exch_ts < :f AND NOT late ORDER BY exch_ts, recv_ts, trade_id"
            ),
            {"s": symbol, "f": fix},
        ).all()
    rows_: list[Any] = list(trades)
    if not apply:  # dry run: show exactly what applying would change
        rows_ += [(ets, ts, f"{seq}:{px}:{qty}#1", px, qty) for seq, ets, px, qty, ts in restore]
        rows_.sort(key=lambda x: (x[0], x[1], x[2]))
    by_min: dict[datetime, list[Any]] = defaultdict(list)
    for t in rows_:
        by_min[t[0].astimezone(UTC).replace(second=0, microsecond=0)].append(t)
    counts = {"REVISED": 0, "UNCHANGED": 0, "INSERTED": 0, "value_changes": 0}
    buckets: set[datetime] = set()
    for r in candles:
        t = r[0].astimezone(UTC)
        cd = Candle(t, r[1], r[2], r[3], r[4], r[5], int(r[6]), synthetic=bool(r[7]))
        reason = REASON_DEDUP
        if t in touched and not r[7] and by_min.get(t):
            ts_ = by_min[t]
            prices = [x[3] for x in ts_]
            vol = sum((x[4] for x in ts_), Decimal(0))
            new = Candle(t, prices[0], max(prices), min(prices), prices[-1], vol, len(ts_))
            if (new.open, new.high, new.low, new.close, new.volume, new.trade_count) != (
                cd.open,
                cd.high,
                cd.low,
                cd.close,
                cd.volume,
                cd.trade_count,
            ):
                counts["value_changes"] += 1
                cd, reason = new, f"{REASON_DEDUP}; {REASON_RECOVERED}"
        buckets.add(t - timedelta(minutes=t.minute % 5))
        if apply:
            counts[
                store.reconcile(
                    "1m", cd, Quality.CONFLICTED, source="reconcile-history", reason=reason
                )
            ] += 1
    m5 = {"REVISED": 0, "UNCHANGED": 0, "INSERTED": 0, "INCOMPLETE": 0}
    if apply:
        for b in sorted(buckets):
            m5[store.rebuild_m5(b, source="reconcile-history", reason=REASON_DEDUP)] += 1
    report.update(
        {
            "pre_fix_m1_candles": len(candles),
            "minutes_with_restored_fills": len(touched),
            "m1": counts,
            "m5_buckets": len(buckets),
            "m5": m5,
        }
    )
    return report


def dedupe_rest_history(
    db: Engine, symbol: str, since: datetime, *, apply: bool = False
) -> dict[str, Any]:
    """V5.7 correction: REST-only rows written under the first (+-500 ms) identity rule that
    are in fact the same trade as a WS row (record time later than 500 ms). They are re-aligned
    with the order-preserving rule; a duplicate is kept (never deleted) but marked
    `sources = DUPLICATE_OF:<ws trade>` and its minute is revised through candle_revisions.
    Every WS trade is a candidate twin: a REST row that aligns to ANY stored WS trade is a
    second record of it (this also catches REST rows re-added after a restart for minutes that
    were already final)."""
    from sp2l.marketdata.collector import _align
    from sp2l.marketdata.tabdeal_public import RestTrade

    delta = timedelta(milliseconds=33)  # REST-only rows were stored at record time - 33 ms
    with db.connect() as c:
        rest_rows = c.execute(
            text(
                "SELECT trade_id, exch_ts, price, qty FROM raw_trades WHERE symbol = :s AND"
                " source = 'REST' AND exch_ts >= :a AND (sources IS NULL OR sources = 'REST')"
                " ORDER BY exch_ts, trade_id"
            ),
            {"s": symbol, "a": since},
        ).all()
        ws_rows = c.execute(
            text(
                "SELECT trade_id, exch_ts, price, qty FROM raw_trades WHERE symbol = :s AND"
                " source = 'WS' AND exch_ts >= :a"
                " ORDER BY exch_ts, trade_id"
            ),
            {"s": symbol, "a": since - timedelta(seconds=5)},
        ).all()
    rest = [
        RestTrade(r[1].astimezone(UTC) + delta, r[2], r[3], None, {"id": r[0]}) for r in rest_rows
    ]
    ws = [[w[1].astimezone(UTC), w[2], w[3], w[0], False] for w in ws_rows]
    pairs = _align(rest, ws, timedelta(seconds=3))
    dups = [(rest_rows[i][0], w[3], rest_rows[i][1]) for i, w in pairs.items()]
    minutes = sorted({ts.astimezone(UTC).replace(second=0, microsecond=0) for _, _, ts in dups})
    out: dict[str, Any] = {
        "rest_only_rows": len(rest_rows),
        "duplicates": len(dups),
        "minutes": [m.isoformat() for m in minutes],
        "applied": apply,
    }
    if apply and dups:
        with db.begin() as c:
            for rid, wid, _ in dups:
                c.execute(
                    text(
                        "UPDATE raw_trades SET sources = :d, reconciled_at = now()"
                        " WHERE trade_id = :r"
                    ),
                    {"d": f"DUPLICATE_OF:{wid}", "r": rid},
                )
                c.execute(
                    text(
                        "UPDATE raw_trades SET sources = COALESCE(sources, 'WS') || ',REST',"
                        " reconciled_at = now() WHERE trade_id = :w"
                    ),
                    {"w": wid},
                )
        store = MarketStore(db, symbol)
        out["revisions"] = {
            m.isoformat(): store.revise_m1_from_trades(
                m, reason="REST_DUPLICATE_REMOVED", source="dedupe-rest-history"
            )
            for m in minutes
        }
    return out


def backfill_history(
    db: Engine,
    symbol: str,
    since: datetime,
    fetch: Any,
    *,
    apply: bool = False,
    policy: Any = None,
    max_hole: timedelta = timedelta(days=7),
) -> dict[str, Any]:
    """V5.8 tier 3 for holes already in the stored series (e.g. gaps recorded before V5.8).

    Every run of missing canonical minutes in [since, now - 2 min) is offered to validated
    Tabdeal chart history exactly as the collector does it live (history.validate: final,
    complete, >= 5 overlapping canonical minutes, >= 80 % exact, H/L within 0.1 %). Accepted
    minutes are INSERTED as TABDEAL_HISTORY_REPAIRED / CANDLE_HISTORY_REPAIR (trade count
    UNKNOWN) through the audited reconcile path, their M5 buckets are rebuilt from canonical
    M1, and each decision is recorded in gap_repairs. Existing candles are never changed and
    past Shadow decisions are never rewritten. Dry run by default."""
    from sp2l.marketdata.history import HistoryPolicy, validate

    pol = policy or HistoryPolicy()
    store = MarketStore(db, symbol)
    now = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=2)
    with db.connect() as c:
        have = {
            r[0].astimezone(UTC): r
            for r in c.execute(
                text(
                    "SELECT open_time, open, high, low, close, volume, trade_count, quality,"
                    " synthetic_no_trade FROM candles_1m WHERE symbol = :s AND open_time >= :a"
                    " AND open_time < :b"
                ),
                {"s": symbol, "a": since - pol.overlap, "b": now},
            )
        }
    holes: list[tuple[datetime, datetime]] = []
    t = since.replace(second=0, microsecond=0)
    first_have = min((x for x in have if x >= t), default=None)
    t = first_have or t  # never before the first stored minute
    end = max(have, default=t)  # holes lie BETWEEN stored minutes (the tail is the collector's)
    while t < end:
        if t not in have:
            a = t
            while t < end and t not in have:
                t += timedelta(minutes=1)
            holes.append((a, t - timedelta(minutes=1)))
        t += timedelta(minutes=1)
    out: dict[str, Any] = {"holes": len(holes), "repaired": [], "failed": []}
    for a, b in holes:
        if b - a > max_hole:
            out["failed"].append({"from": a.isoformat(), "to": b.isoformat(), "reason": "TOO_LONG"})
            continue
        needed = []
        m = a
        while m <= b:
            needed.append(m)
            m += timedelta(minutes=1)
        canonical = {
            k: Candle(k, r[1], r[2], r[3], r[4], r[5], r[6], synthetic=bool(r[8]))
            for k, r in have.items()
            if a - pol.overlap <= k < a and r[7] in ("LIVE_RECONCILED", "LIVE_PROVEN_RAW")
        }
        requested = datetime.now(UTC)
        try:
            raw = fetch(a - pol.overlap - timedelta(minutes=1), b + timedelta(minutes=1))
        except Exception as e:  # network: reported, nothing written
            out["failed"].append({"from": a.isoformat(), "to": b.isoformat(), "reason": repr(e)})
            continue
        res = validate(raw, needed, canonical, requested, pol)
        item = {"from": a.isoformat(), "to": b.isoformat(), "minutes": len(needed), **res.detail}
        if not res.ok:
            out["failed"].append({**item, "reason": res.reason})
            continue
        out["repaired"].append(item)
        if not apply:
            continue
        for _k, cndl in sorted(res.candles.items()):
            store.reconcile(
                "1m",
                cndl,
                Quality.TABDEAL_HISTORY_REPAIRED,
                source="TABDEAL_HISTORY",
                reason="V5.8_HISTORY_BACKFILL",
                extra={
                    "repair_type": "CANDLE_HISTORY_REPAIR",
                    "synthetic_no_trade": cndl.synthetic,
                },
            )
        buckets = sorted({k - timedelta(minutes=k.minute % 5) for k in res.candles})
        for bk in buckets:
            store.rebuild_m5(bk, source="TABDEAL_HISTORY", reason="V5.8_HISTORY_BACKFILL")
        store.record_repair(
            a,
            b + timedelta(minutes=1),
            "V5.8_HISTORY_BACKFILL",
            "REPAIRED",
            "TABDEAL_HISTORY",
            None,
            0,
            {**res.detail, "repair_type": "CANDLE_HISTORY_REPAIR", "unknown": ["trade_count"]},
        )
    return out


BOOTSTRAP_REASON = "V5.12_HISTORY_BOOTSTRAP"
LIVE_QUALITIES = ("LIVE_RECONCILED", "LIVE_PROVEN_RAW")


def bootstrap_history(
    db: Engine,
    symbol: str,
    fetch: Any,
    *,
    lookback: timedelta,
    apply: bool = False,
    policy: Any = None,
    max_fill_gap: int = 2,
    validate_minutes: int = 60,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fill the canonical M1 series from Tabdeal's chart, back to `lookback` (one request).

    Unlike backfill_history (holes BETWEEN stored minutes, checked against the minutes before
    each hole), this also covers the region before the first stored minute - the cold start
    that otherwise needs a full live M5 warmup. Fail closed: the response is trusted only if it
    matches our own most recent live canonical minutes under the continuity model (the tier-3
    policy: >= min_overlap minutes, >= min_agreement exact, H/L within max_rel_diff).
    Without live minutes to compare there is nothing to validate against, so nothing is
    written. Existing candles are never changed; accepted minutes are stored as
    TABDEAL_HISTORY_REPAIRED / CANDLE_HISTORY_REPAIR (trade count UNKNOWN), short chart holes
    as synthetic no-trade minutes (see marketdata/bootstrap.py), and every M5 bucket touched
    is rebuilt from canonical M1. Dry run by default."""
    from sp2l.marketdata.bootstrap import MINUTE, plan_bootstrap
    from sp2l.marketdata.history import HistoryPolicy, validate

    pol = policy or HistoryPolicy()
    store = MarketStore(db, symbol)
    now = now or datetime.now(UTC)
    start = (now - lookback).replace(second=0, microsecond=0)
    with db.connect() as c:
        stored = {
            r[0].astimezone(UTC): r
            for r in c.execute(
                text(
                    "SELECT open_time, open, high, low, close, volume, trade_count, quality,"
                    " synthetic_no_trade FROM candles_1m WHERE symbol = :s AND open_time >= :a"
                ),
                {"s": symbol, "a": start - MINUTE},
            )
        }
    out: dict[str, Any] = {"symbol": symbol, "from": start.isoformat(), "applied": False}
    live = sorted(t for t, r in stored.items() if r[7] in LIVE_QUALITIES and not r[8])
    if not live:
        out["failure"] = "NO_LIVE_MINUTES_TO_VALIDATE_AGAINST"
        return out
    end = max(stored)  # the tail belongs to the collector (holes lie before it)
    out["to"] = end.isoformat()
    canonical = {
        t: Candle(
            t, stored[t][1], stored[t][2], stored[t][3], stored[t][4], stored[t][5], stored[t][6]
        )
        for t in live[-validate_minutes:]
    }
    requested = now
    try:
        raw = fetch(start - MINUTE, now + MINUTE)
    except Exception as e:  # network: reported, nothing written; normal warmup continues
        out["failure"] = f"FETCH_FAILED: {e!r}"
        return out
    check = validate(raw, [], canonical, requested, pol)
    out["validation"] = {"ok": check.ok, "reason": check.reason, **check.detail}
    if not check.ok:
        out["failure"] = check.reason
        return out
    plan = plan_bootstrap(
        raw,
        start,
        end,
        {t: r[4] for t, r in stored.items()},
        requested,
        settle=pol.settle,
        max_fill_gap=max_fill_gap,
    )
    if plan is None:
        out["failure"] = "HISTORY_BAR_INVALID"
        return out
    out.update(
        {
            "chart_first": plan.chart_first.isoformat() if plan.chart_first else None,
            "chart_last": plan.chart_last.isoformat() if plan.chart_last else None,
            "minutes_from_chart": len(plan.candles),
            "synthetic_no_trade_minutes": len(plan.synthetic),
            "gaps_left": [(a.isoformat(), b.isoformat()) for a, b in plan.gaps],
            "dropped_forming": plan.dropped_forming,
        }
    )
    if not apply or not plan.minutes:
        return out
    for _t, cndl in sorted(plan.minutes.items()):
        store.reconcile(
            "1m",
            cndl,
            Quality.TABDEAL_HISTORY_REPAIRED,
            source="TABDEAL_HISTORY",
            reason=BOOTSTRAP_REASON,
            extra={"repair_type": "CANDLE_HISTORY_REPAIR", "synthetic_no_trade": cndl.synthetic},
        )
    buckets = sorted({k - timedelta(minutes=k.minute % 5) for k in plan.minutes})
    for bk in buckets:
        store.rebuild_m5(bk, source="TABDEAL_HISTORY", reason=BOOTSTRAP_REASON)
    first = min(plan.minutes)
    store.record_repair(
        first,
        max(plan.minutes) + MINUTE,
        BOOTSTRAP_REASON,
        "REPAIRED",
        "TABDEAL_HISTORY",
        None,
        0,
        {
            **check.detail,
            "repair_type": "CANDLE_HISTORY_REPAIR",
            "unknown": ["trade_count"],
            "minutes_from_chart": len(plan.candles),
            "synthetic_no_trade_minutes": len(plan.synthetic),
            "gaps_left": len(plan.gaps),
        },
    )
    out["applied"] = True
    out["m5_buckets_rebuilt"] = len(buckets)
    return out
