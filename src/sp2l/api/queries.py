"""Read-only queries behind the API. All presentation-relevant derivations happen here
(UI-02): the frontend renders these values and never computes strategy state.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from sp2l.api import presentation as px
from sp2l.core.types import Candle
from sp2l.runtime.feed_report import WARMUP_TARGET

FILTERS: dict[str, tuple[str, ...]] = {
    "ACTIVE": (),
    "TRADED": ("CLOSED",),
    "REJECTED_CONTEXT": ("REJECTED_CONTEXT",),
    "REJECTED_EXHAUSTION": ("REJECTED_EXHAUSTION",),
    "REJECTED_RISK": ("REJECTED_RISK",),
    "EXPIRED": ("EXPIRED_UNARMED", "EXPIRED_NO_FILL"),
    "AMBIGUOUS_DATA_GAP": ("AMBIGUOUS_DATA_GAP",),
    "ERROR_HOLD": ("ERROR_HOLD",),
    "REJECTED": ("REJECTED_CONTEXT", "REJECTED_EXHAUSTION", "REJECTED_RISK"),
    "SESSION_ENDED": ("SESSION_ENDED",),
}
PRIMARY_FILTERS = ("ACTIVE", "TRADED", "REJECTED")
STORED_TERMINAL = {
    "CLOSED",
    "REJECTED_CONTEXT",
    "REJECTED_EXHAUSTION",
    "REJECTED_RISK",
    "EXPIRED_UNARMED",
    "EXPIRED_NO_FILL",
    "AMBIGUOUS_DATA_GAP",
    "ERROR_HOLD",
}
# A setup still non-terminal in the store whose Shadow session has ended (superseded by a new
# session, e.g. after a spec upgrade) no longer runs anywhere: it is shown as SESSION_ENDED.
# Display only - nothing stored is rewritten and no outcome is fabricated.
SESSION_ENDED = "SESSION_ENDED"
TERMINAL = STORED_TERMINAL | {SESSION_ENDED}
_STORED_TERMINAL_SQL = "','".join(sorted(STORED_TERMINAL))
CANDIDATES = f"""(SELECT c.id, c.symbol, c.side, c.mode, c.spike_id, c.spec_version, c.spec_sha256,
        c.created_at, c.setup_key,
        CASE WHEN c.status NOT IN ('{_STORED_TERMINAL_SQL}') AND EXISTS (
            SELECT 1 FROM setup_checkpoints sc JOIN shadow_sessions ss ON ss.id = sc.session_id
            WHERE sc.candidate_id = c.id AND ss.ended_at IS NOT NULL)
        THEN '{SESSION_ENDED}' ELSE c.status END AS status,
        c.primary_reason, c.reasons, c.status_ts
    FROM candidate_current c WHERE c.symbol = :sym)"""  # only the configured symbol
HEARTBEAT_STALE_S = 90
LONG_GAP_S = 60  # diagnostics: coverage gaps at least this long are highlighted (B45)
FLOW = ["SPIKE", "CONTEXT", "EXHAUSTION", "E1", "PULLBACK", "E1_FILL", "E2", "POSITION"]


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


# ---- overview -------------------------------------------------------------------------


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


def performance(engine: Engine, symbol: str) -> dict[str, Any]:
    """Confirmed Shadow performance: CLOSED setups only. AMBIGUOUS_DATA_GAP setups and
    AMBIGUOUS counterfactuals are counted separately and excluded (B39)."""
    s = one(
        engine,
        "SELECT id, initial_wallet_usdt, started_at FROM shadow_sessions WHERE symbol = :s AND ended_at IS NULL"
        " ORDER BY started_at DESC LIMIT 1",
        s=symbol,
    )
    if s is None:
        return {"session": None}
    q = {"s": s["id"]}
    confirmed = (
        one(
            engine,
            """
        SELECT COUNT(*) FILTER (WHERE cc.status = 'CLOSED') AS closed,
               COUNT(*) FILTER (WHERE cc.status = 'AMBIGUOUS_DATA_GAP') AS ambiguous_excluded,
               COUNT(*) FILTER (WHERE cc.status = 'ERROR_HOLD') AS error_hold
        FROM candidate_current cc JOIN setup_checkpoints sc ON sc.candidate_id = cc.id
        WHERE sc.session_id = :s""",
            **q,
        )
        or {}
    )
    exits = (
        one(
            engine,
            """
        SELECT COUNT(*) FILTER (WHERE e.payload->>'exit_kind' = 'TP') AS tp,
               COUNT(*) FILTER (WHERE e.payload->>'exit_kind' = 'SL') AS sl
        FROM strategy_events e JOIN candidate_current cc ON cc.id = e.candidate_id
        JOIN setup_checkpoints sc ON sc.candidate_id = cc.id
        WHERE sc.session_id = :s AND e.event_type = 'EXIT' AND cc.status = 'CLOSED'""",
            **q,
        )
        or {}
    )
    pnl = (
        one(
            engine,
            """
        SELECT COALESCE(SUM(l.amount) FILTER (WHERE l.kind = 'REALIZED_PNL'), 0) AS realized_pnl,
               COALESCE(-SUM(l.amount) FILTER (WHERE l.kind = 'FEE'), 0) AS fees
        FROM shadow_wallet_ledger l JOIN candidate_current cc ON cc.id = l.candidate_id
        WHERE l.session_id = :s AND cc.status = 'CLOSED'""",
            **q,
        )
        or {}
    )
    cf = rows(
        engine,
        """
        SELECT o.outcome, COUNT(*) AS n FROM cf.counterfactual_outcomes o
        JOIN setup_checkpoints sc ON sc.candidate_id = o.candidate_id
        WHERE sc.session_id = :s GROUP BY o.outcome ORDER BY o.outcome""",
        **q,
    )
    trades = rows(
        engine,
        """
        SELECT cc.setup_key, cc.side, cc.created_at, ex.ts AS exit_at,
               ex.payload->>'exit_kind' AS exit_kind,
               (SELECT MIN(f.ts) FROM fills f JOIN orders o ON o.id = f.order_id
                WHERE o.candidate_id = cc.id AND o.leg = 'E1') AS entry_at,
               COALESCE((SELECT SUM(l.amount) FROM shadow_wallet_ledger l WHERE l.candidate_id = cc.id
                AND l.session_id = :s AND l.kind IN ('REALIZED_PNL', 'FEE')), 0) AS net_pnl
        FROM candidate_current cc JOIN setup_checkpoints sc ON sc.candidate_id = cc.id
        JOIN LATERAL (SELECT e.ts, e.payload FROM strategy_events e WHERE e.candidate_id = cc.id
                      AND e.event_type = 'EXIT' ORDER BY e.id DESC LIMIT 1) ex ON true
        WHERE sc.session_id = :s AND cc.status = 'CLOSED' ORDER BY ex.ts""",
        **q,
    )
    initial = s.get("initial_wallet_usdt")
    ambiguous = rows(
        engine,
        """
        SELECT cc.setup_key, cc.side, cc.created_at, cc.status_ts, cc.primary_reason
        FROM candidate_current cc JOIN setup_checkpoints sc ON sc.candidate_id = cc.id
        WHERE sc.session_id = :s AND cc.status = 'AMBIGUOUS_DATA_GAP'
        ORDER BY cc.status_ts DESC LIMIT 50""",
        **q,
    )
    return {
        "session": s["id"],
        "stats": px.performance_stats(
            None if initial is None else Decimal(str(initial)), trades, s.get("started_at")
        ),
        "recent_trades": [  # newest first, capped for the page
            {**t, "net_pnl_display": px.num(t["net_pnl"], 2)} for t in reversed(trades[-200:])
        ],
        "ambiguous": ambiguous,
        "confirmed": {**confirmed, **exits, **pnl},
        "counterfactual_outcomes": cf,
        "note": "AMBIGUOUS_DATA_GAP setups and AMBIGUOUS counterfactuals are excluded",
    }


def spec_info(rules_version: str, rules_sha: str, manifest_ok: bool) -> dict[str, Any]:
    return {"version": rules_version, "rules_sha256": rules_sha, "manifest_ok": manifest_ok}


def live_status(engine: Engine) -> dict[str, Any]:
    r = one(engine, "SELECT status, failing_items FROM live_automation_status")
    return r or {"status": "LIVE_AUTOMATION_DISABLED", "failing_items": []}


def wallet(engine: Engine, symbol: str) -> dict[str, Any]:
    s = one(
        engine,
        "SELECT id, started_at, initial_wallet_usdt, spec_sha256 FROM shadow_sessions"
        " WHERE symbol = :s AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
        s=symbol,
    )
    if s is None:
        return {"session": None, "balance": None, "ledger": []}
    ledger = rows(
        engine,
        "SELECT l.ts, l.kind, l.amount, l.balance_after, c.setup_key FROM"
        " shadow_wallet_ledger l LEFT JOIN candidates c ON c.id = l.candidate_id"
        " WHERE l.session_id = :s ORDER BY l.id DESC LIMIT 50",
        s=s["id"],
    )
    totals = one(
        engine,
        "SELECT COALESCE(SUM(amount) FILTER (WHERE kind = 'REALIZED_PNL'), 0)"
        " AS realized_pnl, COALESCE(-SUM(amount) FILTER (WHERE kind = 'FEE'), 0)"
        " AS fees FROM shadow_wallet_ledger WHERE session_id = :s",
        s=s["id"],
    )
    return {
        "session": s,
        "balance": ledger[0]["balance_after"] if ledger else None,
        "totals": totals,
        "ledger": ledger,
    }


# ---- setups ---------------------------------------------------------------------------


def setup_list(engine: Engine, symbol: str, bucket: str | None, limit: int) -> list[dict[str, Any]]:
    where = ""
    params: dict[str, Any] = {"n": limit, "sym": symbol}
    if bucket == "ACTIVE":
        where = "WHERE cc.status NOT IN ('" + "','".join(sorted(TERMINAL)) + "')"
    elif bucket in FILTERS:
        where = "WHERE cc.status = ANY(:st)"
        params["st"] = list(FILTERS[bucket])
    out = rows(
        engine,
        f"""
        SELECT cc.setup_key, cc.side, cc.status, cc.primary_reason, cc.reasons, cc.created_at,
               cc.status_ts, cf.outcome AS cf_outcome, cf.result_r AS cf_result_r,
               (SELECT e.payload->>'exit_kind' FROM strategy_events e WHERE e.candidate_id = cc.id
                AND e.event_type = 'EXIT' ORDER BY e.id DESC LIMIT 1) AS exit_kind
        FROM {CANDIDATES} cc
        LEFT JOIN cf.counterfactual_outcomes cf ON cf.candidate_id = cc.id
        {where} ORDER BY cc.created_at DESC LIMIT :n""",
        **params,
    )
    for r in out:
        r["cf_result_r_display"] = None if r["cf_result_r"] is None else _fmt(r["cf_result_r"])
        r["terminal"] = r["status"] in TERMINAL
        r["bucket"] = next((k for k, v in FILTERS.items() if r["status"] in v), "ACTIVE")
    return [px.candidate_row(r) for r in out]


def setup_counts(engine: Engine, symbol: str) -> dict[str, int]:
    by = {
        r["status"]: int(r["n"])
        for r in rows(
            engine, f"SELECT status, COUNT(*) AS n FROM {CANDIDATES} cc GROUP BY status", sym=symbol
        )
    }
    out = {"ALL": sum(by.values()), "ACTIVE": sum(n for k, n in by.items() if k not in TERMINAL)}
    for k, v in FILTERS.items():
        if k != "ACTIVE":
            out[k] = sum(by.get(x, 0) for x in v)
    return out


def _flow(
    status: str,
    transitions: list[dict[str, Any]],
    ctx: dict[str, Any] | None,
    exh: dict[str, Any] | None,
    events: set[str],
    e1_filled: bool,
    e2_state: str | None,
    exited: bool,
) -> list[dict[str, str]]:
    """Per-stage status for the main flow: done / current / failed / pending / skipped."""
    st: dict[str, str] = dict.fromkeys(FLOW, "pending")
    st["SPIKE"] = "done"
    if ctx is not None:
        st["CONTEXT"] = "done" if ctx["status"] == "PASS" else "failed"
    if exh is not None and st["CONTEXT"] == "done":
        st["EXHAUSTION"] = "done" if exh["status"] == "PASS" else "failed"
    if "E1_SUBMITTED" in events:
        st["E1"] = "done"
    if status == "REJECTED_RISK":
        st["E1"] = "failed"
    if "PULLBACK_START" in events:
        st["PULLBACK"] = "done"
    if e1_filled:
        st["E1_FILL"] = "done"
    elif status == "EXPIRED_NO_FILL":
        st["E1_FILL"] = "failed"
    if e2_state is not None:
        st["E2"] = e2_state
    if e1_filled:
        st["POSITION"] = "done" if exited or status == "CLOSED" else "current"
    if status == "AMBIGUOUS_DATA_GAP":
        nxt = next((s for s in FLOW if st[s] in ("pending", "current")), None)
        if nxt:
            st[nxt] = "ambiguous"
    elif status == "ERROR_HOLD":
        nxt = next((s for s in FLOW if st[s] == "pending"), None)
        if nxt:
            st[nxt] = "failed"
    elif status not in TERMINAL:
        nxt = next((s for s in FLOW if st[s] == "pending"), None)
        if nxt:
            st[nxt] = "current"
    return [{"stage": s, "status": st[s]} for s in FLOW]


def setup_detail(engine: Engine, symbol: str, key: str) -> dict[str, Any] | None:
    head = one(
        engine,
        f"SELECT cc.*, s.origin_low, s.origin_high FROM {CANDIDATES} cc"
        " JOIN spikes s ON s.id = cc.spike_id WHERE cc.setup_key = :k",
        k=key,
        sym=symbol,
    )
    if head is None:
        return None
    cid = head["id"]
    q = {"c": cid}
    transitions = rows(
        engine,
        "SELECT seq, state_from, state_to, primary_reason, reasons, ts"
        " FROM candidate_transitions WHERE candidate_id = :c ORDER BY seq",
        **q,
    )
    ctx = rows(
        engine,
        "SELECT * FROM context_snapshots WHERE candidate_id = :c ORDER BY evaluation_seq",
        **q,
    )
    exh = rows(
        engine,
        "SELECT * FROM exhaustion_snapshots WHERE candidate_id = :c ORDER BY evaluation_seq",
        **q,
    )
    revisions = rows(
        engine,
        "SELECT revision, e1, sl, r, tp, e2_reference, qty, wallet_basis,"
        " modeled_worst_loss, modeled_costs, created_at FROM e1_revisions"
        " WHERE candidate_id = :c ORDER BY revision",
        **q,
    )
    orders = rows(
        engine,
        "SELECT leg, revision_id, client_order_id, side, price, qty, status,"
        " executed_qty, created_at, updated_at FROM orders"
        " WHERE candidate_id = :c ORDER BY created_at, id",
        **q,
    )
    fills = rows(
        engine,
        "SELECT o.leg, f.ts, f.price, f.qty, f.fee FROM fills f JOIN orders o"
        " ON o.id = f.order_id WHERE o.candidate_id = :c ORDER BY f.ts",
        **q,
    )
    events = rows(
        engine,
        "SELECT ts, event_type, payload FROM strategy_events WHERE candidate_id = :c ORDER BY id",
        **q,
    )
    ckpt = one(
        engine,
        "SELECT state, terminal, checkpoint, updated_at FROM setup_checkpoints"
        " WHERE candidate_id = :c",
        **q,
    )
    cf = one(engine, "SELECT * FROM cf.counterfactual_outcomes WHERE candidate_id = :c", **q)
    kinds = {e["event_type"] for e in events}
    e1_filled = any(f["leg"] == "E1" for f in fills)
    e2_orders = [o for o in orders if o["leg"] == "E2"]
    e2_state: str | None = None
    if e2_orders:
        e2_state = "done" if e2_orders[-1]["status"] == "FILLED" else "current"
    elif "E2_SKIPPED" in kinds:
        e2_state = "skipped"
    exited = "EXIT" in kinds
    c = (ckpt or {}).get("checkpoint") or {}
    levels = c.get("levels") or (revisions[-1] if revisions else None)
    fill_window = None
    fw = [e for e in events if e["event_type"] == "FILL_WINDOW"]
    if c.get("pb_minute"):
        fill_window = {
            "pullback_minute": c["pb_minute"],
            "candle": fw[-1]["payload"]["candle"] if fw else 1,
            "of": fw[-1]["payload"]["of"] if fw else c["cfg"]["fill_window_candles"],
        }
    marks = (
        one(
            engine,
            "SELECT cl.open_time AS pgap_time, cr.open_time AS spike_time, p.quality,"
            " p.spec_version AS pgap_spec FROM spikes s"
            " JOIN pgaps p ON p.id = s.pgap_id JOIN candles_1m cl ON cl.id = p.middle_candle_id"
            " JOIN candles_1m cr ON cr.id = p.right_candle_id WHERE s.id = :sp",
            sp=head["spike_id"],
        )
        or {}
    )
    flow = _flow(
        head["status"],
        transitions,
        ctx[-1] if ctx else None,
        exh[-1] if exh else None,
        kinds,
        e1_filled,
        e2_state,
        exited,
    )
    human_flow = px.flow_human(flow, head["status"], e2_order_live=bool(e2_orders))
    e2_label = next((f["label"] for f in human_flow if f["stage"] == "E2"), "Waiting")
    return {
        "setup": head,
        "summary": px.candidate_row(
            {
                **head,
                "exit_kind": next(
                    (
                        e["payload"].get("exit_kind")
                        for e in reversed(events)
                        if e["event_type"] == "EXIT"
                    ),
                    None,
                ),
            }
        ),
        "flow_human": human_flow,
        "pgap_quality": px.pgap_quality_view(marks.get("quality"), marks.get("pgap_spec")),
        "context_summary": px.context_summary(ctx[-1] if ctx else None),
        "exhaustion_summary": px.exhaustion_summary(exh[-1] if exh else None),
        "overlays": px.overlays(
            side=head["side"],
            levels=levels,
            revisions=revisions,
            origin=head,
            pgap_time=marks.get("pgap_time"),
            spike_time=marks.get("spike_time"),
            events=events,
            fills=fills,
        ),
        "position": px.position_view(
            setup=head,
            levels=levels,
            fills=fills,
            e2_label=e2_label,
            last_price=_last_price(engine, head["symbol"]),
            now=datetime.now(UTC),
        ),
        "flow": flow,
        "levels": levels,
        "qty": c.get("qty"),
        "breakout_level": c.get("frozen"),
        "eligible_breakout_levels": c.get("eligible"),
        "breakout_locked": c.get("breakout_locked"),
        "fill_window": fill_window,
        "e1": {"filled": c.get("e1_filled"), "full": c.get("e1_full"), "order": c.get("e1_id")},
        "e2": {
            "enabled": c.get("e2_enabled"),
            "filled": c.get("e2_filled"),
            "order": c.get("e2_id"),
        },
        "transitions": transitions,
        "context": ctx,
        "exhaustion": exh,
        "context_gates": context_gates(ctx[-1] if ctx else None),
        "exhaustion_gates": exhaustion_gates(exh[-1] if exh else None),
        "e1_revisions": revisions,
        "orders": orders,
        "fills": fills,
        "events": events,
        "counterfactual": cf,
        "checkpoint_state": (ckpt or {}).get("state"),
    }


def split_close_pair(c: dict[str, Any]) -> dict[str, Any]:
    """Feed-report close pairs are 'CONN@iso'; return UTC timestamps for display."""
    out: dict[str, Any] = {"delta_s": c.get("delta_s")}
    for k in ("a", "b"):
        conn, _, ts = str(c.get(k, "")).partition("@")
        out[k] = {"conn": conn, "ts": jsonable(datetime.fromisoformat(ts)) if ts else None}
    return out


def _last_price(engine: Engine, symbol: str) -> Decimal | None:
    r = last_trade(engine, symbol)
    return None if r is None else Decimal(str(r["price"]))


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
        "continuity": continuity(engine, symbol),
    }


REPAIR_SOURCE = {
    "REST_CONTINUOUS": "Tabdeal recent trades",
    "REST_RECENT_TRADES": "Tabdeal recent trades",
    "TABDEAL_HISTORY": "Tabdeal chart history",
}
REPAIR_TYPE = {
    "REST_CONTINUOUS": "EXACT_RAW_REPAIR",
    "REST_RECENT_TRADES": "EXACT_RAW_REPAIR",
    "TABDEAL_HISTORY": "CANDLE_HISTORY_REPAIR",
}
LIQ_BARS = 21


def continuity(engine: Engine, symbol: str) -> dict[str, Any]:
    """V5.8 Diagnostics: why Context is (not) ready. Everything comes from stored canonical
    data: the trusted contiguous M5 run, the last break and its cause, the last repair and
    which fields remain UNKNOWN. Independent of process, run, socket or boot identity."""
    from sp2l.runtime.feed_report import m5_segments

    seg = m5_segments(engine, symbol)
    cur = seg["current"]
    trusted = int(seg["current_bars"])
    last_break = None
    cause = None
    if cur is not None:
        prev = one(
            engine,
            "SELECT MAX(open_time) AS t FROM candles_5m WHERE symbol = :s AND open_time < :a",
            s=symbol,
            a=cur["first"],
        )
        if prev and prev.get("t"):
            gap_from = datetime.fromisoformat(prev["t"]) + timedelta(minutes=5)
            last_break = {"from": gap_from.isoformat(), "until": cur["first"].isoformat()}
            cause = one(
                engine,
                "SELECT gap_start, gap_end, reason, status, method, failure, detail"
                " FROM gap_repairs WHERE symbol = :s AND status = 'UNRECOVERED' AND"
                " gap_end > :a AND gap_start < :b ORDER BY gap_start DESC LIMIT 1",
                s=symbol,
                a=gap_from,
                b=cur["first"],
            )
    rep = one(
        engine,
        "SELECT gap_start, gap_end, method, detail, resolved_at FROM gap_repairs"
        " WHERE symbol = :s AND status = 'REPAIRED' ORDER BY resolved_at DESC LIMIT 1",
        s=symbol,
    )
    last_repair = None
    if rep:
        last_repair = {
            "interval": [rep["gap_start"], rep["gap_end"]],
            "source": REPAIR_SOURCE.get(rep["method"], rep["method"]),
            "repair_type": REPAIR_TYPE.get(rep["method"]),
            "resolved_at": rep["resolved_at"],
            "minutes_from_history": (rep.get("detail") or {}).get("repaired_minutes")
            if rep["method"] == "TABDEAL_HISTORY"
            else 0,
        }
    tail = rows(
        engine,
        "SELECT open_time, trade_count FROM candles_5m WHERE symbol = :s"
        " ORDER BY open_time DESC LIMIT :n",
        s=symbol,
        n=LIQ_BARS,
    )
    unknown_at = [i for i, r in enumerate(tail) if r["trade_count"] is None]  # 0 = newest
    liq_missing = (LIQ_BARS - unknown_at[0]) if unknown_at else max(0, LIQ_BARS - trusted)
    liq_missing = max(liq_missing, max(0, LIQ_BARS - trusted))
    price_ready = trusted >= WARMUP_TARGET
    liquidity_ready = price_ready and liq_missing == 0
    if not price_ready:
        why = (
            f"{trusted} / {WARMUP_TARGET} trusted M5 bars since the last unrecoverable break"
            if last_break
            else f"{trusted} / {WARMUP_TARGET} trusted M5 bars"
        )
    elif not liquidity_ready:
        why = (
            "Price context ready; trade count unknown for "
            f"{liq_missing} more M5 bar(s): Liquidity is decided by volume alone"
            " (UNKNOWN only if volume is below half its median)"
        )
    else:
        why = f"Context ready: {WARMUP_TARGET} trusted M5 bars"
    return {
        "trusted_m5": trusted,
        "warmup_target": WARMUP_TARGET,
        "price_context_ready": price_ready,
        "liquidity_context_ready": liquidity_ready,
        "liquidity_bars_missing": liq_missing,
        "unknown_fields": ["trade_count"] if unknown_at else [],
        "last_break": last_break,
        "last_break_cause": cause,
        "last_repair": last_repair,
        "ready_reason": why,
    }


def leverage(engine: Engine, symbol: str) -> dict[str, Any]:
    """V5.8: strategy leverage is exactly 10x; the account's leverage is only compared.
    Only evidence read for the configured symbol counts (none yet -> unknown, Live blocked)."""
    from sp2l.marketdata.tabdeal_rest import exchange_symbol
    from sp2l.strategy.risk.engine import STRATEGY_LEVERAGE

    r = one(
        engine,
        "SELECT evidence, finished_at, passed FROM runtime_validation_runs"
        " WHERE item = 'CROSS_10X' AND evidence->'leverage'->'request'->'params'->>'symbol' = :x"
        " ORDER BY finished_at DESC, id DESC LIMIT 1",
        x=exchange_symbol(symbol),
    )
    ev = (r or {}).get("evidence") or {}
    exch = ev.get("exchange_leverage")
    if exch is None:  # evidence recorded before V5.8
        exch = (((ev.get("leverage") or {}).get("response")) or {}).get("leverage")
    try:
        exch_i = int(str(exch)) if exch is not None else None
    except ValueError:
        exch_i = None
    match = exch_i == STRATEGY_LEVERAGE
    return {
        "strategy": STRATEGY_LEVERAGE,
        "exchange": exch_i,
        "checked_at": (r or {}).get("finished_at"),
        "live_blocker": None
        if match
        else ("LEVERAGE_MISMATCH" if exch_i else "LEVERAGE_UNVERIFIED"),
        "text": (
            f"Strategy leverage: {STRATEGY_LEVERAGE}x / Exchange leverage: "
            + (f"{exch_i}x" if exch_i is not None else "not read yet")
            + (
                " / Live: allowed by leverage"
                if match
                else f" / Live: blocked — exchange leverage must be {STRATEGY_LEVERAGE}x"
            )
        ),
        "automatic_change": False,
    }


def indicators(engine: Engine, symbol: str, limit: int) -> dict[str, Any]:
    """Live indicators: the Shadow engine's snapshot per finalized M5 bar (open session)."""
    sess = open_session(engine, symbol)
    snaps = (
        rows(
            engine,
            "SELECT snapshot, recorded_at FROM indicator_snapshots WHERE session_id = :s"
            " ORDER BY open_time DESC LIMIT :n",
            s=sess["id"],
            n=limit,
        )
        if sess
        else []
    )
    return px.indicator_view(snaps, datetime.now(UTC), _last_price(engine, symbol))


def recent_pgaps(engine: Engine, symbol: str, limit: int) -> list[dict[str, Any]]:
    """V5.9 Diagnostics: every geometric P-Gap with its measured quality and outcome."""
    out = rows(
        engine,
        "SELECT p.id, p.side, p.confirmed_at, cm.open_time AS impulse_time, p.promoted,"
        " p.not_promoted_reason, p.quality, p.spec_version FROM pgaps p"
        " JOIN candles_1m cm ON cm.id = p.middle_candle_id WHERE p.symbol = :s"
        " ORDER BY p.confirmed_at DESC, p.id DESC LIMIT :n",
        s=symbol,
        n=limit,
    )
    for r in out:
        r["view"] = px.pgap_quality_view(r.pop("quality"), r["spec_version"])
        r["outcome"] = px.pgap_outcome(
            r["promoted"], r["not_promoted_reason"], r["view"]["measured"]
        )
    return out


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


def open_session(engine: Engine, symbol: str) -> dict[str, Any] | None:
    return one(
        engine,
        "SELECT s.id, s.started_at, s.initial_wallet_usdt, e.updated_at AS activity_at"
        " FROM shadow_sessions s LEFT JOIN engine_checkpoints e ON e.session_id = s.id"
        " WHERE s.symbol = :s AND s.ended_at IS NULL ORDER BY s.started_at DESC LIMIT 1",
        s=symbol,
    )


def system(
    engine: Engine,
    symbol: str,
    *,
    costs_problem: str | None,
    manifest_ok: bool,
    collector: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from sp2l.runtime.feed_report import m5_segments

    col = collector or collector_health(engine)
    run_id = (col.get("run") or {}).get("id")
    feed = px.feed_summary(
        col,
        m5_segments(engine, symbol, datetime.now(UTC) - timedelta(hours=24)),
        conflict_counts(engine, symbol, run_id),
        repair_counts(engine, symbol),
    )
    session = open_session(engine, symbol)
    active = setup_list(engine, symbol, "ACTIVE", 5)
    last = setup_list(engine, symbol, None, 1)
    cont = continuity(engine, symbol)
    lev = leverage(engine, symbol)
    status = px.system_status(
        continuity=cont,
        leverage=lev,
        collector=col,
        feed=feed,
        costs_problem=costs_problem,
        session=session,
        active=active,
        last_candidate=last[0] if last else None,
        live=live_status(engine),
        manifest_ok=manifest_ok,
        session_activity_at=(session or {}).get("activity_at"),
    )
    return {**status, "feed": feed, "continuity": cont, "leverage": lev}


def counterfactuals(engine: Engine, symbol: str, limit: int) -> list[dict[str, Any]]:
    out = rows(
        engine,
        "SELECT c.setup_key, o.label, o.rejection_stage, o.outcome, o.result_r,"
        " o.e2_filled, o.e1_filled_at, o.exit_at, o.detail, o.created_at,"
        " p.impulse_body_ratio, p.gap_body_ratio, p.gap_size_ticks"
        " FROM cf.counterfactual_outcomes o JOIN candidates c ON c.id = o.candidate_id"
        " LEFT JOIN spikes sp ON sp.id = c.spike_id LEFT JOIN pgaps p ON p.id = sp.pgap_id"
        " WHERE c.symbol = :sym ORDER BY o.id DESC LIMIT :n",
        n=limit,
        sym=symbol,
    )
    for r in out:
        r["result_r_display"] = None if r["result_r"] is None else _fmt(r["result_r"])
    return out


def candles(engine: Engine, symbol: str, tf: str, limit: int) -> list[dict[str, Any]]:
    extra = (
        "synthetic_no_trade, repair_type"
        if tf == "1m"
        else "synthetic_m1_count, synthetic_fraction, real_trade_count, history_m1_count"
    )
    out = rows(
        engine,
        f"SELECT open_time, open, high, low, close, volume, trade_count, quality, revision,"
        f" {extra} FROM candles_{tf} WHERE symbol = :s ORDER BY open_time DESC LIMIT :n",
        s=symbol,
        n=limit,
    )
    out.reverse()
    # V5.6: a minute without a canonical candle is an explicit hole, never silently skipped
    step = timedelta(minutes=1 if tf == "1m" else 5)
    filled: list[dict[str, Any]] = []
    for r in out:
        t = datetime.fromisoformat(r["open_time"])
        while filled and datetime.fromisoformat(filled[-1]["open_time"]) + step < t:
            nxt = datetime.fromisoformat(filled[-1]["open_time"]) + step
            filled.append({"open_time": nxt.isoformat(), "missing": True, "quality": "DATA_GAP"})
        filled.append(r)
    return filled


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


# ---- gate tables (server-side presentation; the frontend only renders) -----------------


def _res(flag: Any, *, bad_when: bool = True) -> str:
    if flag is None:
        return "N/A"
    return "FAIL" if bool(flag) is bad_when else "PASS"


def _trig(flag: Any) -> str:
    return "N/A" if flag is None else ("TRIGGERED" if flag else "NOT_TRIGGERED")


def _yn(flag: Any) -> str:
    return "N/A" if flag is None else ("YES" if flag else "NO")


DISPLAY_DP = Decimal("0.000001")


def _fmt(v: Any) -> str:
    """Display rendering only (6 dp); PASS/FAIL always comes from backend booleans and the
    exact value travels alongside in `exact`."""
    if v is None or v == "None":
        return "—"
    if isinstance(v, bool):
        return "true" if v else "false"
    txt = str(v)
    try:
        if "/" in txt:
            a, b = txt.split("/", 1)
            d = Decimal(a) / Decimal(b)
        else:
            d = Decimal(txt)
    except (ArithmeticError, ValueError):
        return txt
    if not d.is_finite():
        return txt
    q = d.quantize(DISPLAY_DP) if abs(d) < Decimal("1e12") else d
    out = _dec(q)
    return out if q == d or "/" not in txt and len(txt) <= 16 else f"≈{out}"


def _raw(s: dict[str, Any], key: str) -> Any:
    exact = s.get("exact") or {}
    return exact.get(key, s.get(key))


def _x(s: dict[str, Any], key: str) -> str:
    """Display value of a metric (exact value is in the row's `exact` map)."""
    return _fmt(_raw(s, key))


CONTEXT_EXACT = {
    "NetTP": ("net_tp_per_unit", "tp"),
    "ChannelEdge": ("range_position_origin",),
    "Regime": ("chop14", "adx14"),
    "RangeMiddle (E1)": ("range_position_e1",),
    "RoomToTP": ("nearest_obstacle", "room_to_tp_r"),
    "Liquidity": ("volume_ratio", "tradecount_ratio"),
}
EXHAUSTION_EXACT = {
    "StretchATR (ExtremeStretch)": ("stretch_atr", "ema20_m5", "atr14_m5"),
    "SpikeATR (ClimacticSpike)": ("spike_atr",),
    "RangePosition20 / OpposingSwingATR (AtOuterEdge)": (
        "range_position_20",
        "opposing_swing_distance_atr",
    ),
}


def _with_exact(
    s: dict[str, Any], rows_: list[dict[str, Any]], keys: dict[str, tuple[str, ...]]
) -> list[dict[str, Any]]:
    for r in rows_:
        r["exact"] = {k: _raw(s, k) for k in keys.get(r["gate"], ())}
        r["value"] = "—" if r["value"] in (None, "None") else str(r["value"]).replace("None", "—")
    return rows_


def context_gates(s: dict[str, Any] | None) -> list[dict[str, Any]]:
    if s is None:
        return []
    return _with_exact(s, _context_rows(s), CONTEXT_EXACT)


def exhaustion_gates(s: dict[str, Any] | None) -> list[dict[str, Any]]:
    if s is None:
        return []
    return _with_exact(s, _exhaustion_rows(s), EXHAUSTION_EXACT)


def _context_rows(s: dict[str, Any]) -> list[dict[str, Any]]:
    """V6.0 gates first (NetTP, then any of LevelBreak / ChannelEdge / HTFAligned), then the
    V5 measures as INFO rows (never a reason since V6.0)."""
    th = s.get("thresholds") or {}
    reasons = s.get("reasons") or []
    room = (
        "+INF (no obstacle in E1→TP path)"
        if s.get("room_to_tp_infinite")
        else _x(s, "room_to_tp_r")
    )
    net = _raw(s, "net_tp_per_unit")
    net_unknown = "CONTEXT_NET_TP_UNKNOWN" in reasons
    return [
        {
            "gate": "NetTP",
            "value": f"net/unit={_x(s, 'net_tp_per_unit')}  TP={_x(s, 'tp')}",
            "threshold": th.get("net_tp", "|TP − E1| − E1·entry_fee − TP·exit_fee > 0"),
            "result": "FAIL"
            if net_unknown or "CONTEXT_NET_TP_NOT_POSITIVE" in reasons
            else ("N/A" if net is None else "PASS"),
            "reason": next((r for r in reasons if r.startswith("CONTEXT_NET_TP")), None),
        },
        {
            "gate": "Warmup",
            "value": f"warm={_x(s, 'warm')}",
            "threshold": f"{WARMUP_TARGET} finalized M5 bars since anchor",
            "result": "FAIL" if "CONTEXT_UNKNOWN_WARMUP" in reasons else "PASS",
            "reason": "CONTEXT_UNKNOWN_WARMUP" if "CONTEXT_UNKNOWN_WARMUP" in reasons else None,
        },
        {
            "gate": "LevelBreak",
            "value": f"level={_fmt(s.get('breakout_level'))}",
            "threshold": "Spike M1 close strictly beyond a confirmed M5 swing level at origin",
            "result": _yn(s.get("breakout_context")),
            "reason": None,
        },
        {
            "gate": "ChannelEdge",
            "value": f"RP(origin)={_x(s, 'range_position_origin')}",
            "threshold": "RP ≤ 1/3 (Long) / ≥ 2/3 (Short) in the last 14 M5 bars",
            "result": _yn(s.get("range_edge_origin")),
            "reason": None,
        },
        {
            "gate": "HTFAligned",
            "value": s.get("trend"),
            "threshold": "M5 trend BULL (Long) / BEAR (Short)",
            "result": _yn(s.get("htf_alignment")),
            "reason": None,
        },
        {
            "gate": "Require one context",
            "value": "level break / channel edge / HTF aligned",
            "threshold": "at least one YES",
            "result": "FAIL"
            if "NO_VALID_CONTEXT" in reasons
            else ("N/A" if s.get("htf_alignment") is None else "PASS"),
            "reason": "NO_VALID_CONTEXT" if "NO_VALID_CONTEXT" in reasons else None,
        },
        {
            "gate": "Regime",
            "value": f"{s.get('regime')}  CHOP14={_x(s, 'chop14')}  ADX14={_x(s, 'adx14')}",
            "threshold": "info only",
            "result": "INFO",
            "reason": None,
        },
        {
            "gate": "RangeMiddle (E1)",
            "value": f"RP={_x(s, 'range_position_e1')}  "
            f"range={_fmt(s.get('range_low'))}–{_fmt(s.get('range_high'))}",
            "threshold": "info only",
            "result": "INFO",
            "reason": None,
        },
        {
            "gate": "HTF opposite without breakout",
            "value": _yn(s.get("htf_opposite_no_breakout")),
            "threshold": "info only",
            "result": "INFO",
            "reason": None,
        },
        {
            "gate": "RoomToTP",
            "value": f"obstacle={_fmt(s.get('nearest_obstacle'))}  room={room}R",
            "threshold": "info only",
            "result": "INFO",
            "reason": None,
        },
        {
            "gate": "Liquidity",
            "value": f"vol={_x(s, 'volume_ratio')}  trades={_x(s, 'tradecount_ratio')}"
            f"  ({s.get('liquidity_status')})",
            "threshold": "info only",
            "result": "INFO",
            "reason": None,
        },
        {
            "gate": "CONTEXT RESULT",
            "value": s.get("status"),
            "threshold": "NetTP AND (A OR B OR C)",
            "result": "PASS" if s.get("status") == "PASS" else "FAIL",
            "reason": ", ".join(reasons) or None,
        },
    ]


def _exhaustion_rows(s: dict[str, Any]) -> list[dict[str, Any]]:
    th = s.get("thresholds") or {}
    return [
        {
            "gate": "TrendAgeBars",
            "value": s.get("trend_age_bars"),
            "threshold": f"≥ {th.get('trend_age_bars_gte')}",
            "result": "INFO",
            "reason": None,
        },
        {
            "gate": "MicrochannelLen",
            "value": s.get("microchannel_len"),
            "threshold": f"≥ {th.get('microchannel_len_gte')}",
            "result": "INFO",
            "reason": None,
        },
        {
            "gate": "LateTrend",
            "value": "age OR microchannel",
            "threshold": "either threshold",
            "result": _trig(s.get("late_trend")),
            "reason": None,
        },
        {
            "gate": "StretchATR (ExtremeStretch)",
            "value": f"{_x(s, 'stretch_atr')}  (EMA20={_fmt(s.get('ema20_m5'))}, ATR14={_fmt(s.get('atr14_m5'))})",
            "threshold": f"≥ {th.get('stretch_atr_gte')}",
            "result": _trig(s.get("extreme_stretch")),
            "reason": None,
        },
        {
            "gate": "SpikeATR (ClimacticSpike)",
            "value": _x(s, "spike_atr"),
            "threshold": f"≥ {th.get('spike_atr_gte')}",
            "result": _trig(s.get("climactic_spike")),
            "reason": None,
        },
        {
            "gate": "RangePosition20 / OpposingSwingATR (AtOuterEdge)",
            "value": f"RP20={_x(s, 'range_position_20')}  opp="
            f"{'+INF' if s.get('opposing_swing_infinite') else _x(s, 'opposing_swing_distance_atr')}",
            "threshold": f"RP20 ≥ {th.get('rp20_long_gte')} (Long) / ≤ {th.get('rp20_short_lte')} "
            f"(Short) or opp ≤ {th.get('opposing_swing_atr_lte')}",
            "result": _trig(s.get("at_outer_edge")),
            "reason": None,
        },
        {
            "gate": "FreshBreakoutException",
            "value": f"prev regime={s.get('previous_regime_at_breakout')}  "
            f"age={(s.get('exact') or {}).get('breakout_age_m5')}",
            "threshold": f"prev RANGE/TRANSITION and age ≤ {th.get('fresh_breakout_max_age_m5')}",
            "result": _yn(s.get("fresh_breakout_exception")),
            "reason": None,
        },
        {
            "gate": "EXHAUSTION RESULT",
            "value": "LateTrend ∧ ExtremeStretch ∧ (Climactic ∨ OuterEdge) ∧ ¬Fresh",
            "threshold": "",
            "result": {"PASS": "PASS", "REJECT": "FAIL"}.get(s.get("status") or "", "FAIL"),
            "reason": ", ".join(s.get("sub_reasons") or [])
            or ("EXHAUSTION_UNKNOWN" if s.get("status") == "UNKNOWN" else None),
        },
    ]


# ---- support / resistance zones (display only; never read by any strategy code) ------------

ZONE_TFS = ("15m", "30m", "4h")
ZONE_LOOKBACK = timedelta(days=7)
_zone_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}


def sr_zones(engine: Engine, symbol: str, per_side: int = 3) -> dict[str, Any]:
    """Swing-pivot S/R zones on 15m / 30m / 4h from stored canonical M1 (analytics/sr_zones).
    Recomputed at most every 20 s: zones only change when a timeframe bar closes."""
    from sp2l.analytics.sr_zones import compute

    now = datetime.now(UTC)
    hit = _zone_cache.get(symbol)
    if hit is not None and now - hit[0] < timedelta(seconds=20):
        return hit[1]
    with engine.connect() as c:
        m1 = [
            Candle(r[0].astimezone(UTC), r[1], r[2], r[3], r[4])
            for r in c.execute(
                text(
                    "SELECT open_time, open, high, low, close FROM candles_1m WHERE symbol = :s"
                    " AND open_time >= :a AND NOT COALESCE(synthetic_no_trade, false)"
                    " ORDER BY open_time"
                ),
                {"s": symbol, "a": now - ZONE_LOOKBACK},
            )
        ]
    t = last_trade(engine, symbol)
    price = Decimal(str(t["price"])) if t else (m1[-1].close if m1 else None)
    zones = [] if price is None else compute(m1, price, now, ZONE_TFS, per_side)
    out = {
        "timeframes": list(ZONE_TFS),
        "price": None if price is None else _dec(price),
        "computed_at": now.isoformat(),
        "note": "Display only: swing-pivot zones (2 bars each side) from canonical M1;"
        " 4h bars aligned to Tehran time. Never used by the strategy.",
        "zones": [
            {
                "timeframe": z.timeframe,
                "kind": z.kind,
                "bottom": _dec(z.bottom),
                "top": _dec(z.top),
                "since": z.since.isoformat(),
                "pivots": z.pivots,
                "label": f"{z.timeframe} {'R' if z.kind == 'RESISTANCE' else 'S'}",
            }
            for z in zones
        ],
    }
    _zone_cache[symbol] = (now, out)
    return out
