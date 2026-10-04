"""Read-only data-layer queries behind the API (market data, collector health, integrity).

Strategy (SMC) queries live in api/smc.py. The frontend renders these values as they are.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

HEARTBEAT_STALE_S = 90
LONG_GAP_S = 60  # diagnostics: coverage gaps at least this long are highlighted


def _dec(d: Decimal) -> str:
    """Exact value without storage padding (numeric(38,18) -> '97.08711578')."""
    if not d.is_finite():
        return str(d)
    t = format(d.normalize(), "f")
    return "0" if t in ("-0", "") else t


def jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return _dec(v)
    if isinstance(v, datetime):
        return (v.astimezone(UTC) if v.tzinfo else v).isoformat()  # always UTC
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [jsonable(x) for x in v]
    return v


def rows(engine: Engine, sql: str, **p: Any) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [jsonable(dict(r._mapping)) for r in c.execute(text(sql), p)]


def one(engine: Engine, sql: str, **p: Any) -> dict[str, Any] | None:
    r = rows(engine, sql, **p)
    return r[0] if r else None


def collector_health(engine: Engine) -> dict[str, Any]:
    run = one(engine, "SELECT * FROM collector_runs ORDER BY id DESC LIMIT 1")
    hb = one(engine, "SELECT * FROM collector_heartbeats ORDER BY id DESC LIMIT 1")
    restarts = rows(
        engine,
        "SELECT id, started_at, ended_at, exit_reason, pid, mode FROM"
        " collector_runs ORDER BY id DESC LIMIT 20",
    )
    for i, r in enumerate(restarts):
        r["clean_exit"] = r["ended_at"] is not None
        r["run_status"] = (
            "CLEAN_EXIT" if r["ended_at"] is not None else "RUNNING" if i == 0 else "UNCLEAN_EXIT"
        )
    stale = True
    age_s = None
    if hb is not None:
        age = datetime.now(UTC) - datetime.fromisoformat(hb["ts"])
        age_s = int(age.total_seconds())
        stale = age_s > HEARTBEAT_STALE_S
    status = (
        "NO_DATA"
        if hb is None
        else ("STALE" if stale else ("CONNECTED" if hb["connected"] else "DISCONNECTED"))
    )
    return {
        "status": status,
        "heartbeat_age_s": age_s,
        "run": run,
        "heartbeat": hb,
        "runs": restarts,
    }


def data_quality(engine: Engine, symbol: str, minutes: int = 180) -> dict[str, Any]:
    """Minute timeline: OK / SYNTHETIC / DATA_GAP (no candle row = no proven coverage)."""
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=minutes)
    have = {
        r["open_time"]: r
        for r in rows(
            engine,
            "SELECT open_time, synthetic_no_trade, trade_count FROM candles_1m WHERE"
            " symbol = :s AND open_time >= :a AND open_time < :b",
            s=symbol,
            a=start,
            b=end,
        )
    }
    timeline = []
    counts = {"OK": 0, "SYNTHETIC": 0, "DATA_GAP": 0}
    for i in range(minutes):
        t = (start + timedelta(minutes=i)).isoformat()
        r = have.get(t)
        status = "DATA_GAP" if r is None else ("SYNTHETIC" if r["synthetic_no_trade"] else "OK")
        counts[status] += 1
        timeline.append({"minute": t, "status": status, "trades": r["trade_count"] if r else None})
    gaps = rows(
        engine,
        "SELECT gap_start, gap_end, reason, timeframe FROM data_gaps"
        " WHERE symbol = :s ORDER BY gap_start DESC LIMIT 20",
        s=symbol,
    )
    m5 = rows(
        engine,
        "SELECT open_time, synthetic_m1_count, synthetic_fraction,"
        " real_trade_count FROM candles_5m WHERE symbol = :s"
        " ORDER BY open_time DESC LIMIT 36",
        s=symbol,
    )
    return {
        "window_minutes": minutes,
        "counts": counts,
        "timeline": timeline,
        "gaps": gaps,
        "m5": m5,
    }


def diagnostics(engine: Engine, symbol: str) -> dict[str, Any]:
    """B45: host-sleep and long coverage-gap events (never hidden, never synthesized)."""
    sleeps = rows(
        engine,
        "SELECT gap_start, gap_end, reason,"
        " EXTRACT(EPOCH FROM gap_end - gap_start)::int AS seconds"
        " FROM data_gaps WHERE symbol = :s AND kind = 'HOST_SLEEP'"
        " ORDER BY gap_start DESC LIMIT 20",
        s=symbol,
    )
    long_gaps = rows(
        engine,
        "SELECT gap_start, gap_end, reason,"
        " EXTRACT(EPOCH FROM gap_end - gap_start)::int AS seconds"
        " FROM data_gaps WHERE symbol = :s AND kind = 'DATA_GAP' AND"
        " gap_end - gap_start >= make_interval(secs => :n)"
        " ORDER BY gap_start DESC LIMIT 20",
        s=symbol,
        n=LONG_GAP_S,
    )
    return {"host_sleeps": sleeps, "long_gaps": long_gaps, "long_gap_threshold_s": LONG_GAP_S}


def live_status(engine: Engine) -> dict[str, Any]:
    r = one(engine, "SELECT status, failing_items FROM live_automation_status")
    return r or {"status": "LIVE_AUTOMATION_DISABLED", "failing_items": []}


def split_close_pair(c: dict[str, Any]) -> dict[str, Any]:
    """Feed-report close pairs are 'CONN@iso'; return UTC timestamps for display."""
    out: dict[str, Any] = {"delta_s": c.get("delta_s")}
    for k in ("a", "b"):
        conn, _, ts = str(c.get(k, "")).partition("@")
        out[k] = {"conn": conn, "ts": jsonable(datetime.fromisoformat(ts)) if ts else None}
    return out


def last_trade(engine: Engine, symbol: str) -> dict[str, Any] | None:
    return one(
        engine,
        "SELECT price, exch_ts FROM raw_trades WHERE symbol = :s ORDER BY exch_ts DESC LIMIT 1",
        s=symbol,
    )


def market_summary(engine: Engine, symbol: str, display: str) -> dict[str, Any]:
    """Chart header: pair, timeframe, latest price and its age, last-60-min data quality."""
    t = last_trade(engine, symbol)
    c = one(
        engine,
        "SELECT open_time, close FROM candles_1m WHERE symbol = :s ORDER BY open_time DESC LIMIT 1",
        s=symbol,
    )
    q = data_quality(engine, symbol, 60)["counts"]
    gaps = q["DATA_GAP"]
    age = (
        None
        if t is None
        else int((datetime.now(UTC) - datetime.fromisoformat(t["exch_ts"])).total_seconds())
    )
    return {
        "symbol": display,
        "timeframe": "1m",
        "last_price": None if t is None else t["price"],
        "last_trade_at": None if t is None else t["exch_ts"],
        "age_s": age,
        "last_candle_open": None if c is None else c["open_time"],
        "quality": {
            "window": "last 60 min",
            "label": "Complete" if gaps == 0 else f"{gaps} gap minute{'s' if gaps != 1 else ''}",
            "tone": "ok" if gaps == 0 else "warn",
            "counts": q,
        },
    }


def conflict_counts(engine: Engine, symbol: str, run_id: int | None) -> dict[str, int]:
    """Genuine payload conflicts only (V5.6 audit): the same price+amount under one sequence
    at a DIFFERENT exchange time. Different price/amount under one sequence is a multi-fill;
    all 428 stored rows up to 2026-09-27 were multi-fills, incl. the 25 unclassified ones."""
    r = (
        one(
            engine,
            "SELECT COUNT(*) FILTER (WHERE run_id = :r) AS current_run,"
            " COUNT(*) FILTER (WHERE ts >= now() - interval '24 hours') AS last_24h"
            " FROM feed_conflicts WHERE symbol = :s"
            " AND first->'fingerprint'->>2 = other->'fingerprint'->>2"
            " AND first->'fingerprint'->>3 = other->'fingerprint'->>3"
            " AND first->'fingerprint'->>1 <> other->'fingerprint'->>1",
            s=symbol,
            r=run_id,
        )
        or {}
    )
    return {"current_run": int(r.get("current_run") or 0), "last_24h": int(r.get("last_24h") or 0)}


def repair_counts(engine: Engine, symbol: str) -> dict[str, Any]:
    r = (
        one(
            engine,
            "SELECT COUNT(*) FILTER (WHERE status = 'REPAIRED' AND resolved_at >= now() - interval '1 hour') AS rep,"
            " COUNT(*) FILTER (WHERE status = 'UNRECOVERED' AND resolved_at >= now() - interval '1 hour') AS unrec"
            " FROM gap_repairs WHERE symbol = :s",
            s=symbol,
        )
        or {}
    )
    last = {
        st: one(
            engine,
            'SELECT gap_start AS start, gap_end AS "end", failure, method, resolved_at,'
            " resolved_at >= now() - interval '15 minutes' AS recent FROM gap_repairs"
            " WHERE symbol = :s AND status = :st ORDER BY gap_start DESC LIMIT 1",
            s=symbol,
            st=st,
        )
        for st in ("REPAIRED", "UNRECOVERED")
    }
    return {
        "repaired_last_hour": r.get("rep") or 0,
        "unrecovered_last_hour": r.get("unrec") or 0,
        "last_repaired": last["REPAIRED"],
        "last_unrecovered": last["UNRECOVERED"],
    }


def validation(engine: Engine) -> list[dict[str, Any]]:
    return rows(
        engine,
        """
        SELECT i.item, r.passed, r.finished_at, r.api_version, r.notes, r.evidence
        FROM runtime_validation_items i
        LEFT JOIN LATERAL (SELECT * FROM runtime_validation_runs x WHERE x.item = i.item
                           ORDER BY finished_at DESC, id DESC LIMIT 1) r ON true
        ORDER BY i.item""",
    )


def live_snapshot(engine: Engine, symbol: str) -> dict[str, Any]:
    """Forming minutes after the last final canonical M1, from stored canonical trades
    (same construction as the collector's live view), plus the latest trade price."""
    last = one(engine, "SELECT MAX(open_time) AS t FROM candles_1m WHERE symbol = :s", s=symbol)
    since = (
        datetime.fromisoformat(last["t"]) + timedelta(minutes=1)
        if last and last.get("t")
        else datetime.now(UTC) - timedelta(minutes=2)
    )
    trades = rows(
        engine,
        "SELECT exch_ts, price, qty FROM raw_trades WHERE symbol = :s AND exch_ts >= :a"
        " AND NOT late AND source IN ('WS', 'REST')"
        " AND (sources IS NULL OR sources NOT LIKE 'DUPLICATE_OF:%')"
        " ORDER BY exch_ts, recv_ts, trade_id",
        s=symbol,
        a=since,
    )
    forming: dict[str, dict[str, Any]] = {}
    for t in trades:
        m = datetime.fromisoformat(t["exch_ts"]).replace(second=0, microsecond=0).isoformat()
        px_ = Decimal(str(t["price"]))
        f = forming.get(m)
        if f is None:
            forming[m] = {"t": m, "o": px_, "h": px_, "l": px_, "c": px_, "v": Decimal(0), "n": 0}
            f = forming[m]
        f["h"], f["l"], f["c"] = max(f["h"], px_), min(f["l"], px_), px_
        f["v"] += Decimal(str(t["qty"]))
        f["n"] += 1
    lt = last_trade(engine, symbol)
    return {
        "type": "snapshot",
        "last_final": last.get("t") if last else None,
        "forming": [
            {k: (_dec(v) if isinstance(v, Decimal) else v) for k, v in f.items()}
            for f in forming.values()
        ],
        "price": None if lt is None else _dec(Decimal(str(lt["price"]))),
        "price_ts": None if lt is None else lt.get("exch_ts"),
    }


def integrity(engine: Engine, symbol: str) -> dict[str, Any]:
    """Diagnostics: repairs, unrecoverable gaps, candle revisions, conflict audit, lineage."""
    return {
        "repairs": rows(
            engine,
            "SELECT gap_start, gap_end, reason, status, method, failure, trades_recovered,"
            " detail, resolved_at FROM gap_repairs WHERE symbol = :s ORDER BY gap_start DESC LIMIT 100",
            s=symbol,
        ),
        "revisions": rows(
            engine,
            "SELECT timeframe, open_time, revision, old, new, old_quality, new_quality, source,"
            " reason, reconciled_at FROM candle_revisions WHERE symbol = :s"
            " ORDER BY reconciled_at DESC, id DESC LIMIT 100",
            s=symbol,
        ),
        "revision_counts": rows(
            engine,
            "SELECT timeframe, reason, COUNT(*) AS n FROM candle_revisions WHERE symbol = :s"
            " GROUP BY 1, 2 ORDER BY 1, 2",
            s=symbol,
        ),
        "conflict_audit": rows(
            engine,
            "SELECT run_id, CASE WHEN first->'fingerprint'->>2 = other->'fingerprint'->>2 AND"
            " first->'fingerprint'->>3 = other->'fingerprint'->>3 AND first->'fingerprint'->>1"
            " <> other->'fingerprint'->>1 THEN 'TIMESTAMP_CONFLICT' ELSE 'MULTI_FILL_SAME_SEQUENCE'"
            " END AS classification, COUNT(*) AS n, COUNT(DISTINCT trade_id) AS sequences"
            " FROM feed_conflicts WHERE symbol = :s GROUP BY 1, 2 ORDER BY 1, 2",
            s=symbol,
        ),
        "latency_1h": rows(
            engine,
            "SELECT stage, COUNT(*) AS n,"
            " round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ms)::numeric) AS p50,"
            " round(percentile_cont(0.9) WITHIN GROUP (ORDER BY ms)::numeric) AS p90,"
            " round(percentile_cont(0.99) WITHIN GROUP (ORDER BY ms)::numeric) AS p99"
            " FROM pipeline_latency WHERE symbol = :s AND recorded_at > now() - interval '1 hour'"
            " GROUP BY 1 ORDER BY 1",
            s=symbol,
        ),
        "latest_canonical_minute": (
            one(engine, "SELECT MAX(open_time) AS t FROM candles_1m WHERE symbol = :s", s=symbol)
            or {}
        ).get("t"),
        "revision_total": (
            one(engine, "SELECT COUNT(*) AS n FROM candle_revisions WHERE symbol = :s", s=symbol)
            or {}
        ).get("n"),
        "quality_24h": rows(
            engine,
            "SELECT quality, COUNT(*) AS n FROM candles_1m WHERE symbol = :s AND"
            " open_time >= now() - interval '24 hours' GROUP BY 1 ORDER BY 1",
            s=symbol,
        ),
    }
