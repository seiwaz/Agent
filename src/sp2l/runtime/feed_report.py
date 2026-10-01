"""B46 transport-redundancy measurement report (read-only; computed from stored evidence).

Metrics: per-connection disconnects with exact close timestamps/reasons and reconnect
durations; A/B close correlation; connection lifetimes; merged uncovered seconds; merged
DATA_GAP minutes; duplicate rate; payload conflicts; longest continuous finalized M5 segment
and whether >= 150 consecutive bars were reached (B46 acceptance target).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

WARMUP_TARGET = 150
CORRELATION_WINDOW_S = 10


def _rows(db: Engine, sql: str, **p: Any) -> list[dict[str, Any]]:
    with db.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), p)]


def m5_segments(db: Engine, symbol: str, since: datetime | None = None) -> dict[str, Any]:
    rows = _rows(
        db,
        """
        WITH b AS (
            SELECT open_time,
                   open_time - (ROW_NUMBER() OVER (ORDER BY open_time)) * interval '5 min' AS g
            FROM candles_5m WHERE symbol = :s AND (CAST(:since AS timestamptz) IS NULL
                                                   OR open_time >= :since))
        SELECT COUNT(*) AS bars, MIN(open_time) AS first, MAX(open_time) AS last
        FROM b GROUP BY g ORDER BY MAX(open_time)""",
        s=symbol,
        since=since,
    )
    longest = max(rows, key=lambda r: r["bars"], default=None)
    current = rows[-1] if rows else None
    now = datetime.now(UTC)
    if current is not None and now - current["last"] > timedelta(minutes=10):
        current = None  # the latest segment has already been broken
    return {
        "segments": len(rows),
        "longest": longest,
        "current": current,
        "current_bars": current["bars"] if current else 0,
        "warmup_target": WARMUP_TARGET,
        "target_reached": bool(longest and longest["bars"] >= WARMUP_TARGET),
    }


def report(db: Engine, symbol: str, since: datetime) -> dict[str, Any]:
    events = _rows(
        db,
        "SELECT conn, kind, ts, reason, detail FROM feed_connection_events"
        " WHERE symbol = :s AND ts >= :t ORDER BY ts, id",
        s=symbol,
        t=since,
    )
    conns: dict[str, dict[str, Any]] = {}
    closes: list[tuple[str, datetime]] = []
    pending_close: dict[str, dict[str, Any]] = {}
    for e in events:
        c = conns.setdefault(
            e["conn"], {"disconnects": 0, "closes": [], "lifetimes_s": [], "reconnect_s": []}
        )
        if e["kind"] == "CLOSED":
            c["disconnects"] += 1
            item = {
                "ts": e["ts"].isoformat(),
                "reason": e["reason"],
                "lifetime_s": (e["detail"] or {}).get("lifetime_s"),
            }
            c["closes"].append(item)
            if item["lifetime_s"] is not None:
                c["lifetimes_s"].append(round(float(item["lifetime_s"]), 1))
            closes.append((e["conn"], e["ts"]))
            pending_close[e["conn"]] = item
        elif e["kind"] == "CONFIRMED" and e["conn"] in pending_close:
            item = pending_close.pop(e["conn"])
            dur = (e["ts"] - datetime.fromisoformat(item["ts"])).total_seconds()
            item["reconnect_s"] = round(dur, 1)
            c["reconnect_s"].append(round(dur, 1))
    correlated = []
    for i, (ca, ta) in enumerate(closes):
        for cb, tb in closes[i + 1 :]:
            if cb != ca and abs((tb - ta).total_seconds()) <= CORRELATION_WINDOW_S:
                correlated.append(
                    {
                        "a": f"{ca}@{ta.isoformat()}",
                        "b": f"{cb}@{tb.isoformat()}",
                        "delta_s": round(abs((tb - ta).total_seconds()), 1),
                    }
                )
    gaps = _rows(
        db,
        "SELECT gap_start, gap_end, reason FROM data_gaps WHERE symbol = :s AND"
        " kind = 'DATA_GAP' AND timeframe = 'trades' AND gap_end >= :t"
        " ORDER BY gap_start",
        s=symbol,
        t=since,
    )
    uncovered = sum(
        (min(g["gap_end"], datetime.now(UTC)) - max(g["gap_start"], since)).total_seconds()
        for g in gaps
    )
    m1 = _rows(
        db,
        "SELECT payload->>'status' AS status, COUNT(*) AS n FROM market_events"
        " WHERE symbol = :s AND kind = 'M1' AND ts >= :t GROUP BY 1",
        s=symbol,
        t=since,
    )
    m1_counts = {r["status"]: int(r["n"]) for r in m1}
    hb = _rows(
        db,
        "SELECT h.trades_total, h.detail FROM collector_heartbeats h JOIN"
        " collector_runs r ON r.id = h.run_id WHERE r.symbol = :s AND h.ts >= :t"
        " ORDER BY h.id DESC LIMIT 1",
        s=symbol,
        t=since,
    )
    dup = conflicts_live = None
    if hb:
        d = hb[0]["detail"] or {}
        trades = hb[0]["trades_total"] or 0
        dup = {
            "duplicates": d.get("duplicates"),
            "unique_trades": trades,
            "duplicate_rate": (round(d.get("duplicates", 0) / trades, 4) if trades else None),
            "orphans": d.get("orphans"),
        }
        conflicts_live = d.get("conflicts")
    # V5.6 audit: classify from the fingerprints themselves (pre-classification rows were all
    # multi-fills). Genuine conflict = same price+amount at a different exchange time.
    kinds = {
        r["k"]: int(r["n"])
        for r in _rows(
            db,
            "SELECT CASE WHEN first->'fingerprint'->>2 = other->'fingerprint'->>2 AND"
            " first->'fingerprint'->>3 = other->'fingerprint'->>3 AND"
            " first->'fingerprint'->>1 <> other->'fingerprint'->>1 THEN 'TIMESTAMP_CONFLICT'"
            " ELSE 'MULTI_FILL_SAME_SEQUENCE' END AS k, COUNT(*) AS n"
            " FROM feed_conflicts WHERE symbol = :s AND ts >= :t GROUP BY 1",
            s=symbol,
            t=since,
        )
    }
    conflicts = kinds.get("TIMESTAMP_CONFLICT", 0)
    return {
        "symbol": symbol,
        "since": since.isoformat(),
        "until": datetime.now(UTC).isoformat(),
        "connections": conns,
        "correlated_closes": correlated,
        "correlation_window_s": CORRELATION_WINDOW_S,
        "merged_gaps": [
            {
                "start": g["gap_start"].isoformat(),
                "end": g["gap_end"].isoformat(),
                "reason": g["reason"],
                "seconds": round((g["gap_end"] - g["gap_start"]).total_seconds(), 1),
            }
            for g in gaps
        ],
        "merged_uncovered_seconds": round(uncovered, 1),
        "merged_m1": m1_counts,
        "merged_data_gap_minutes": m1_counts.get("DATA_GAP", 0) + m1_counts.get("UNANCHORED", 0),
        "dedup": dup,
        "conflicts": int(conflicts),
        "multi_fill_sequences": kinds.get("MULTI_FILL_SAME_SEQUENCE", 0),
        "conflicts_in_process": conflicts_live,
        "m5": m5_segments(db, symbol, since),
    }
