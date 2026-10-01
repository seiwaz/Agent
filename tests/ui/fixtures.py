"""Realistic API responses for UI states A–J.

Stored rows are synthetic, but every DTO the browser sees is produced by the REAL backend
presentation code (`sp2l.api.presentation` and the gate/flow builders in `sp2l.api.queries`),
so the fixtures cannot drift from what the API actually serves.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import Any

from sp2l.api import presentation as px
from sp2l.api import queries as qx

STATES = {
    "A": "warmup / no candidate",
    "B": "scanning / ready",
    "C": "candidate rejected by Context",
    "D": "candidate rejected by Exhaustion",
    "E": "E1 pending",
    "F": "E1 partial",
    "G": "E1 filled + E2 pending",
    "H": "active position",
    "I": "market-data degraded",
    "J": "AMBIGUOUS_DATA_GAP",
}
SPEC = {
    "version": "5.9",
    "rules_sha256": "020d6bceebb8d1372747f4e52696f59a8e23dec93bd0219a50e792f0711ad766",
    "manifest_ok": True,
}
LIVE = {
    "status": "LIVE_AUTOMATION_DISABLED",
    "failing_items": [
        "CANCEL_RACE_FILL",
        "CANCEL_RACE_ZERO_FILL",
        "CONTRACT_PRICE_SEMANTICS",
        "CROSS_10X",
        "DISABLE_LIVE",
        "E1_E2_AGGREGATION_REDUCE_ONLY",
        "LIQUIDATION_AFTER_E2",
        "NATIVE_SL_SINGLE_TP",
        "NO_DUPLICATE_E1",
        "PARTIAL_FILLS",
        "RESTART_RECOVERY",
        "SYMBOL_FILTERS",
        "WS_TRADE_COMPLETENESS",
    ],
}
CTX_TH = {
    "range_bars": 14,
    "range_middle": ["1/3", "2/3"],
    "regime_range": "CHOP14 >= 61.8 AND ADX14 < 20",
    "regime_trend": "CHOP14 <= 38.2 OR ADX14 >= 25",
    "room_to_tp_min_r": "1",
    "liquidity_lookback": 20,
    "liquidity_ratio_reject_below": "1/2",
}
EXH_TH = {
    "rp20_long_gte": "9/10",
    "spike_atr_gte": "3/2",
    "rp20_short_lte": "1/10",
    "stretch_atr_gte": "2",
    "trend_age_bars_gte": 20,
    "microchannel_len_gte": 8,
    "opposing_swing_atr_lte": "1/2",
    "fresh_breakout_max_age_m5": 1,
}
E1, SL, TP, E2, R = D("84152.4"), D("83981.9"), D("84322.9"), D("84067.1"), D("170.5")


def _now() -> datetime:
    return datetime.now(UTC).replace(second=0, microsecond=0)


def iso(t: datetime) -> str:
    return t.isoformat()


# ---- market ------------------------------------------------------------------------------


def candles(now: datetime) -> list[dict[str, Any]]:
    """240 M1 candles: drift, a sharp spike ~40 min ago, then a pullback toward E1."""
    rng = random.Random(7)
    out = []
    price = D("83940")
    for i in range(240):
        t = now - timedelta(minutes=240 - i)
        k = i - 196
        if 0 <= k < 6:  # the spike (P-Gap on k=2)
            drift = D(str(34 + 6 * k))
        elif 6 <= k < 26:
            drift = D(str(rng.uniform(-9, 5))).quantize(D("0.1"))
        else:
            drift = D(str(rng.uniform(-6, 6.4))).quantize(D("0.1"))
        o = price
        c = (price + drift).quantize(D("0.1"))
        hi = max(o, c) + D(str(rng.uniform(0.5, 9))).quantize(D("0.1"))
        lo = min(o, c) - D(str(rng.uniform(0.5, 9))).quantize(D("0.1"))
        out.append(
            {
                "open_time": iso(t),
                "open": str(o),
                "high": str(hi),
                "low": str(lo),
                "close": str(c),
                "volume": str(D(str(rng.uniform(0.2, 3))).quantize(D("0.001"))),
                "trade_count": rng.randint(20, 160),
                "synthetic_no_trade": False,
                "quality": "REPAIRED_TABDEAL" if i in (150, 151) else "LIVE_PROVEN_RAW",
                "revision": 0,
            }
        )
        price = c
    out[120] = {"open_time": out[120]["open_time"], "missing": True, "quality": "DATA_GAP"}
    return out


def quality(now: datetime, gap_minutes: int = 0) -> dict[str, Any]:
    tl = []
    counts = {"OK": 0, "SYNTHETIC": 0, "DATA_GAP": 0}
    for i in range(180):
        t = now - timedelta(minutes=180 - i)
        st = "DATA_GAP" if 120 <= i < 120 + gap_minutes else ("SYNTHETIC" if i % 97 == 5 else "OK")
        counts[st] += 1
        tl.append(
            {
                "minute": iso(t),
                "status": st,
                "trades": None if st == "DATA_GAP" else (0 if st == "SYNTHETIC" else 40 + i % 30),
            }
        )
    gaps = (
        [
            {
                "gap_start": iso(now - timedelta(minutes=60)),
                "gap_end": iso(now - timedelta(minutes=60 - gap_minutes)),
                "reason": "DISCONNECTED",
                "timeframe": "trades",
            }
        ]
        if gap_minutes
        else []
    )
    return {"window_minutes": 180, "counts": counts, "timeline": tl, "gaps": gaps, "m5": []}


def collector(
    now: datetime, *, b_up: bool = True, conflicts: int = 0, connected: bool = True
) -> dict[str, Any]:
    run = {
        "id": 7,
        "started_at": iso(now - timedelta(hours=26)),
        "ended_at": None,
        "pid": 22052,
        "host": "trading-mac.local",
        "symbol": "BTCUSDT",
        "mode": "COLLECT",
        "spec_version": "5.9",
        "spec_sha256": SPEC["rules_sha256"],
        "exit_reason": None,
    }
    conns = {
        "A": {
            "received": 41822,
            "sessions": 27,
            "confirmed": True,
            "connected": True,
            "last_close": iso(now - timedelta(minutes=13)),
            "disconnects": 26,
            "coverage_lag_ms": 1402,
            "last_close_reason": "CLOSE_1000:please reconnect",
        },
        "B": {
            "received": 39310 if b_up else 38777,
            "sessions": 26,
            "confirmed": b_up,
            "connected": b_up,
            "last_close": iso(now - timedelta(minutes=3 if not b_up else 23)),
            "disconnects": 25 if b_up else 26,
            "coverage_lag_ms": 1611 if b_up else None,
            "last_close_reason": "No route to host" if not b_up else "CLOSE_1000:please reconnect",
        },
    }
    hb = {
        "id": 3121,
        "run_id": 7,
        "ts": iso(now - timedelta(seconds=12)),
        "connected": connected,
        "healthy_until": iso(now - timedelta(seconds=14)),
        "coverage_lag_ms": 1402,
        "last_trade_exch_ts": iso(now - timedelta(seconds=13)),
        "trades_total": 41822,
        "late_total": 3,
        "m1_ok": 1511,
        "m1_synthetic": 7,
        "m1_data_gap": 4,
        "m1_unanchored": 1,
        "gaps_total": 2,
        "reconnects": 51,
        "detail": {
            "orphans": 0,
            "conflicts": conflicts,
            "duplicates": 39310,
            "connections": conns,
            "merged_status": "COVERED" if connected else "GAP",
        },
    }
    runs = [
        {**run, "clean_exit": False, "run_status": "RUNNING"},
        {
            "id": 6,
            "started_at": iso(now - timedelta(hours=40)),
            "ended_at": iso(now - timedelta(hours=26, minutes=1)),
            "exit_reason": "SIGTERM",
            "pid": 19920,
            "mode": "COLLECT",
            "clean_exit": True,
            "run_status": "CLEAN_EXIT",
        },
    ]
    return {
        "status": "CONNECTED" if connected else "DISCONNECTED",
        "heartbeat_age_s": 12,
        "run": run,
        "heartbeat": hb,
        "runs": runs,
        "diagnostics": {"host_sleeps": [], "long_gaps": [], "long_gap_threshold_s": 60},
    }


def feed(now: datetime, col: dict[str, Any], bars: int, conflicts_24h: int = 0) -> dict[str, Any]:
    closes = [
        {
            "ts": iso(now - timedelta(minutes=13 + 60 * i)),
            "reason": "CLOSE_1000:please reconnect",
            "reconnect_s": 0.4,
        }
        for i in range(3)
    ]
    return {
        "symbol": "BTCUSDT",
        "since": iso(now - timedelta(hours=24)),
        "until": iso(now),
        "connections": {
            "A": {
                "disconnects": 26,
                "closes": closes,
                "reconnect_s": [0.4, 0.5],
                "lifetimes_s": [3600.1],
            },
            "B": {
                "disconnects": 25,
                "closes": closes[:2],
                "reconnect_s": [0.3],
                "lifetimes_s": [3599.8],
            },
        },
        "correlated_closes": [
            {
                "a": {"conn": "B", "ts": iso(now - timedelta(hours=4, minutes=18))},
                "b": {"conn": "A", "ts": iso(now - timedelta(hours=4, minutes=18))},
                "delta_s": 0.1,
            }
        ],
        "correlation_window_s": 10,
        "merged_gaps": [
            {
                "start": iso(now - timedelta(hours=9)),
                "end": iso(now - timedelta(hours=9) + timedelta(seconds=122)),
                "reason": "DISCONNECTED",
                "seconds": 122.2,
            }
        ],
        "merged_uncovered_seconds": 122.2,
        "merged_m1": {"OK": 1431, "DATA_GAP": 3},
        "merged_data_gap_minutes": 3,
        "dedup": {
            "duplicates": 39310,
            "unique_trades": 41822,
            "duplicate_rate": 0.9399,
            "orphans": 0,
        },
        "conflicts": conflicts_24h,
        "multi_fill_sequences": 212,
        "conflicts_in_process": col["heartbeat"]["detail"]["conflicts"],
        "m5": {
            "segments": 3,
            "longest": {
                "bars": max(bars, 108),
                "first": iso(now - timedelta(minutes=5 * bars)),
                "last": iso(now),
            },
            "current": {"bars": bars},
            "current_bars": bars,
            "warmup_target": 150,
            "target_reached": bars >= 150,
        },
        "live": col["heartbeat"],
    }


# ---- setups --------------------------------------------------------------------------------


def ctx_snap(*, ok: bool = True, regime: str = "TREND", middle: bool = False) -> dict[str, Any]:
    reasons = [] if ok else ["RANGE_MIDDLE"]
    return {
        "status": "PASS" if ok else "REJECT",
        "regime": regime,
        "trend": "BULL",
        "chop14": 33.41,
        "adx14": 31.87,
        "range_high": "84330.0",
        "range_low": "83790.5",
        "range_position_e1": "0.6719" if not middle else "0.5214",
        "range_position_origin": "0.3548",
        "range_middle_reject": middle,
        "breakout_level": "84102.6" if ok else None,
        "breakout_context": ok,
        "htf_alignment": regime != "RANGE",
        "range_edge_origin": False,
        "htf_opposite_no_breakout": False,
        "nearest_obstacle": None if ok else "84290.0",
        "room_to_tp_r": None if ok else "0.81",
        "room_to_tp_infinite": ok,
        "room_pass": ok,
        "volume_ratio": "1.84",
        "tradecount_ratio": "1.62",
        "liquidity_status": "PASS",
        "primary_reason": None if ok else "RANGE_MIDDLE",
        "reasons": reasons if ok else ["RANGE_MIDDLE", "ROOM_TO_TP_INSUFFICIENT"],
        "thresholds": CTX_TH,
        "exact": {
            "warm": True,
            "chop14": "33.41",
            "adx14": "31.87",
            "range_position_e1": "1045/2004" if middle else "1347/2005",
            "room_to_tp_r": None if ok else "138/170",
            "volume_ratio": "46/25",
            "tradecount_ratio": "81/50",
        },
    }


def exh_snap(*, reject: bool = False) -> dict[str, Any]:
    return {
        "status": "REJECT" if reject else "PASS",
        "trend_age_bars": 34 if reject else 6,
        "microchannel_len": 11 if reject else 3,
        "ema20_m5": "83990.1",
        "atr14_m5": "71.4",
        "spike_extreme": "84204.2",
        "stretch_atr": "2.41" if reject else "0.94",
        "spike_atr": "1.78" if reject else "0.88",
        "range_position_20": "0.97" if reject else "0.64",
        "opposing_swing_distance_atr": "1.9",
        "opposing_swing_infinite": False,
        "late_trend": reject,
        "extreme_stretch": reject,
        "climactic_spike": reject,
        "at_outer_edge": reject,
        "fresh_breakout_exception": False,
        "previous_regime_at_breakout": "TREND",
        "sub_reasons": [
            "LATE_TREND_AGE",
            "LONG_MICROCHANNEL",
            "EXTREME_STRETCH",
            "CLIMACTIC_SPIKE",
            "OUTER_EDGE_20",
        ]
        if reject
        else [],
        "thresholds": EXH_TH,
        "exact": {"stretch_atr": "172/71.4" if reject else "0.94", "breakout_age_m5": None},
    }


def _order(
    leg: str, rev: int, price: D, qty: str, status: str, executed: str, t: datetime
) -> dict[str, Any]:
    return {
        "leg": leg,
        "revision_id": rev,
        "client_order_id": f"sp2l-BTCUSDT-{leg}-{rev}",
        "side": "BUY",
        "price": str(price),
        "qty": qty,
        "status": status,
        "executed_qty": executed,
        "created_at": iso(t),
        "updated_at": iso(t),
    }


def setup(now: datetime, kind: str, key: str, created_min_ago: int = 38) -> dict[str, Any]:
    """Build a setup detail exactly like queries.setup_detail() does, from stored-row shapes."""
    t0 = now - timedelta(minutes=created_min_ago)
    status = {
        "C": "REJECTED_CONTEXT",
        "D": "REJECTED_EXHAUSTION",
        "E": "E1_PENDING",
        "F": "E1_PARTIAL",
        "G": "E2_PENDING",
        "H": "POSITION_ACTIVE",
        "J": "AMBIGUOUS_DATA_GAP",
        "T": "CLOSED",
    }[kind]
    primary = {"C": "RANGE_MIDDLE", "D": "EXHAUSTION_RISK", "J": "AMBIGUOUS_DATA_GAP"}.get(kind)
    reasons = {
        "C": ["RANGE_MIDDLE", "ROOM_TO_TP_INSUFFICIENT"],
        "D": ["EXHAUSTION_RISK"],
        "J": ["AMBIGUOUS_DATA_GAP", "LIQ_UNVERIFIED"],
    }.get(kind, ["LIQ_UNVERIFIED"])
    head = {
        "id": key,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "mode": "SHADOW",
        "spike_id": 1,
        "spec_version": "5.9",
        "spec_sha256": SPEC["rules_sha256"],
        "created_at": iso(t0),
        "setup_key": key,
        "status": status,
        "primary_reason": primary,
        "reasons": reasons,
        "status_ts": iso(now - timedelta(minutes=3)),
        "origin_low": "83982.0",
        "origin_high": "84041.3",
    }
    ctx = ctx_snap(ok=kind != "C", regime="RANGE" if kind == "C" else "TREND", middle=kind == "C")
    exh = None if kind == "C" else exh_snap(reject=kind == "D")
    rejected = kind in ("C", "D")
    events: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    revisions: list[dict[str, Any]] = []
    fill_window = None
    if not rejected:
        revisions = [
            {
                "revision": 0,
                "e1": "84131.7",
                "sl": str(SL),
                "r": "149.8",
                "tp": "84281.5",
                "e2_reference": "84056.8",
                "qty": "0.006",
                "wallet_basis": "100",
                "modeled_worst_loss": "0.97",
                "modeled_costs": "0.07",
                "created_at": iso(t0 + timedelta(minutes=1)),
            },
            {
                "revision": 1,
                "e1": str(E1),
                "sl": str(SL),
                "r": str(R),
                "tp": str(TP),
                "e2_reference": str(E2),
                "qty": "0.005",
                "wallet_basis": "100",
                "modeled_worst_loss": "0.96",
                "modeled_costs": "0.06",
                "created_at": iso(t0 + timedelta(minutes=3)),
            },
        ]
        events += [
            {
                "ts": iso(t0 + timedelta(minutes=1)),
                "event_type": "E1_SUBMITTED",
                "payload": {"rev": 0},
            },
            {
                "ts": iso(t0 + timedelta(minutes=3)),
                "event_type": "E1_SUBMITTED",
                "payload": {"rev": 1},
            },
        ]
        orders.append(
            _order("E1", 0, D("84131.7"), "0.006", "CANCELED", "0", t0 + timedelta(minutes=1))
        )
    pb = now - timedelta(minutes=9)
    if kind in ("F", "G", "H", "J", "T"):
        events.append(
            {"ts": iso(pb), "event_type": "PULLBACK_START", "payload": {"minute": iso(pb)}}
        )
        fill_window = {"pullback_minute": iso(pb), "candle": 2, "of": 4}
        part = kind == "F"
        orders.append(
            _order(
                "E1",
                1,
                E1,
                "0.005",
                "PARTIALLY_FILLED" if part else "FILLED",
                "0.002" if part else "0.005",
                pb,
            )
        )
        fills.append(
            {
                "leg": "E1",
                "ts": iso(pb + timedelta(seconds=20)),
                "price": str(E1),
                "qty": "0.002" if part else "0.005",
                "fee": "0.03",
            }
        )
    elif kind == "E":
        orders.append(_order("E1", 1, E1, "0.005", "NEW", "0", t0 + timedelta(minutes=3)))
    if kind in ("G", "H", "J", "T"):
        orders.append(
            _order(
                "E2",
                0,
                E2,
                "0.005",
                "FILLED" if kind in ("H", "J", "T") else "NEW",
                "0.005" if kind in ("H", "J", "T") else "0",
                pb,
            )
        )
        if kind in ("H", "J", "T"):
            fills.append(
                {
                    "leg": "E2",
                    "ts": iso(now - timedelta(minutes=6, seconds=30)),
                    "price": str(E2),
                    "qty": "0.005",
                    "fee": "0.03",
                }
            )
    if kind == "T":
        events.append(
            {
                "ts": iso(now - timedelta(minutes=2)),
                "event_type": "EXIT",
                "payload": {"exit_kind": "TP", "price": str(TP)},
            }
        )
    kinds = {e["event_type"] for e in events}
    e2_orders = [o for o in orders if o["leg"] == "E2"]
    e2_state = ("done" if e2_orders[-1]["status"] == "FILLED" else "current") if e2_orders else None
    flow = qx._flow(
        status,
        [],
        ctx,
        exh,
        kinds,
        bool([f for f in fills if f["leg"] == "E1"]),
        e2_state,
        "EXIT" in kinds,
    )
    human = px.flow_human(flow, status, e2_order_live=bool(e2_orders))
    levels = (
        None
        if rejected
        else {"e1": str(E1), "sl": str(SL), "tp": str(TP), "e2": str(E2), "r": str(R)}
    )
    c = candles(now)
    spike_t, pgap_t = c[199]["open_time"], c[198]["open_time"]
    return qx.jsonable(
        {
            "setup": head,
            "summary": px.candidate_row({**head, "exit_kind": "TP" if kind == "T" else None}),
            "flow_human": human,
            "pgap_quality": px.pgap_quality_view(REAL_PGAPS["LONG_VALID"]["quality"], "5.9"),
            "context_summary": px.context_summary(ctx),
            "exhaustion_summary": px.exhaustion_summary(exh),
            "overlays": px.overlays(
                side="LONG",
                levels=levels,
                revisions=revisions,
                origin=head,
                pgap_time=pgap_t,
                spike_time=spike_t,
                events=events,
                fills=fills,
            ),
            "position": px.position_view(
                setup=head,
                levels=levels,
                fills=fills,
                e2_label=next(f["label"] for f in human if f["stage"] == "E2"),
                last_price=D(c[-1]["close"]),
                now=now,
            ),
            "flow": flow,
            "levels": levels,
            "qty": None if rejected else "0.005",
            "breakout_level": {"price": "84102.6"} if not rejected else None,
            "eligible_breakout_levels": None,
            "breakout_locked": kind in ("F", "G", "H"),
            "fill_window": fill_window,
            "e1": {},
            "e2": {},
            "transitions": [],
            "context": [ctx],
            "exhaustion": [exh] if exh else [],
            "context_gates": qx.context_gates(ctx),
            "exhaustion_gates": qx.exhaustion_gates(exh),
            "e1_revisions": revisions,
            "orders": orders,
            "fills": fills,
            "events": events,
            "counterfactual": {
                "rejection_stage": "CONTEXT",
                "outcome": "SL",
                "result_r": "-1",
                "e2_filled": True,
            }
            if kind == "C"
            else None,
            "checkpoint_state": status,
        }
    )


def _list_row(d: dict[str, Any]) -> dict[str, Any]:
    s = d["summary"]
    return {
        **s,
        "cf_outcome": None,
        "cf_result_r": None,
        "cf_result_r_display": None,
        "terminal": s["status"] in qx.TERMINAL,
        "bucket": next((k for k, v in qx.FILTERS.items() if s["status"] in v), "ACTIVE"),
    }


# ---- whole-state bundles ------------------------------------------------------------------------


def bundle(state: str) -> dict[str, Any]:
    now = _now()
    warm = {"A": 42, "J": 11}.get(state, 188)  # J: the recent gap restarted warmup
    col = collector(now, b_up=state != "I", conflicts=2 if state == "I" else 0)
    fd = feed(now, col, warm, conflicts_24h=2 if state == "I" else 0)
    session = (
        None
        if state == "A"
        else {
            "id": "8b0c",
            "started_at": iso(now - timedelta(hours=11)),
            "initial_wallet_usdt": "100",
            "activity_at": iso(now - timedelta(minutes=1)),
        }
    )
    details: dict[str, dict[str, Any]] = {}
    history_kinds = {
        "B": ["C", "T", "T"],
        "C": ["C", "T", "T"],
        "D": ["D", "C", "T", "T"],
        "E": ["E", "C", "T"],
        "F": ["F", "C", "T"],
        "G": ["G", "C", "T"],
        "H": ["H", "D", "C", "T"],
        "I": ["C", "T"],
        "J": ["J", "C", "T"],
    }.get(state, [])
    for i, k in enumerate(history_kinds):
        key = f"BTCUSDT-8b0c-{40 - i}"
        details[key] = setup(now, k, key, created_min_ago=38 + i * 55)
    rows = [_list_row(d) for d in details.values()]
    active = [r for r in rows if not r["terminal"]]
    feed_sum = px.feed_summary(
        col,
        fd["m5"],
        {"current_run": 2 if state == "I" else 0, "last_24h": 2 if state == "I" else 0},
    )
    system = px.system_status(
        collector=col,
        feed=feed_sum,
        costs_problem="costs.maker_fee is missing" if state == "A" else None,
        session=session,
        active=active,
        last_candidate=rows[0] if rows else None,
        live=LIVE,
        manifest_ok=True,
        session_activity_at=session and session["activity_at"],
    )
    system = {**system, "feed": feed_sum}
    trades = [
        {
            "setup_key": f"BTCUSDT-8b0c-{30 - i}",
            "side": "LONG",
            "created_at": iso(now - timedelta(hours=10 - i)),
            "exit_at": iso(now - timedelta(hours=9 - i, minutes=10)),
            "exit_kind": k,
            "entry_at": iso(now - timedelta(hours=9 - i, minutes=30)),
            "net_pnl": v,
        }
        for i, (k, v) in enumerate(
            [("TP", "0.91"), ("SL", "-1.04"), ("TP", "0.88"), ("TP", "0.93"), ("SL", "-1.02")]
        )
    ]
    perf = (
        None
        if session is None
        else {
            "session": session["id"],
            "stats": px.performance_stats(D("100"), trades, session["started_at"]),
            "recent_trades": [
                {**t, "net_pnl_display": px.num(t["net_pnl"], 2)} for t in reversed(trades)
            ],
            "ambiguous": [
                {
                    "setup_key": r["setup_key"],
                    "side": "LONG",
                    "created_at": r["created_at"],
                    "status_ts": r["status_ts"],
                    "primary_reason": "AMBIGUOUS_DATA_GAP",
                }
                for r in rows
                if r["status"] == "AMBIGUOUS_DATA_GAP"
            ],
            "confirmed": {
                "closed": len(trades),
                "ambiguous_excluded": 1 if state == "J" else 0,
                "error_hold": 0,
                "tp": 3,
                "sl": 2,
            },
            "counterfactual_outcomes": [{"outcome": "SL", "n": 4}, {"outcome": "TP", "n": 2}],
            "note": "AMBIGUOUS_DATA_GAP setups and AMBIGUOUS counterfactuals are excluded",
        }
    )
    bal = "101.66" if session else None
    wallet = {
        "session": session,
        "balance": bal,
        "totals": {"realized_pnl": "1.92", "fees": "0.26"} if session else None,
        "ledger": [
            {
                "ts": t["exit_at"],
                "kind": "REALIZED_PNL",
                "amount": t["net_pnl"],
                "balance_after": str(
                    D("100") + sum((D(x["net_pnl"]) for x in trades[: n + 1]), D(0))
                ),
                "setup_key": t["setup_key"],
            }
            for n, t in reversed(list(enumerate(trades)))  # newest first, running balance
        ]
        if session
        else [],
    }
    c = candles(now)
    last = c[-1]
    market = {
        "symbol": "BTC/USDT",
        "timeframe": "1m",
        "last_price": last["close"],
        "last_trade_at": iso(now - timedelta(seconds=2)),
        "age_s": 2,
        "last_candle_open": last["open_time"],
        "quality": {
            "window": "last 60 min",
            "label": "Complete" if state != "J" else "4 gap minutes",
            "tone": "ok" if state != "J" else "warn",
            "counts": {
                "OK": 60 if state != "J" else 56,
                "SYNTHETIC": 0,
                "DATA_GAP": 0 if state != "J" else 4,
            },
        },
    }
    counts = {"ALL": len(rows), "ACTIVE": len(active)}
    for k, v in qx.FILTERS.items():
        if k != "ACTIVE":
            counts[k] = sum(1 for r in rows if r["status"] in v)
    cf = {
        "label": "COUNTERFACTUAL",
        "items": [
            {
                "setup_key": r["setup_key"],
                "label": "COUNTERFACTUAL",
                "rejection_stage": "CONTEXT",
                "outcome": "SL",
                "result_r": "-1",
                "result_r_display": "-1",
                "e2_filled": True,
                "e1_filled_at": None,
                "exit_at": None,
                "detail": {},
                "created_at": r["created_at"],
            }
            for r in rows
            if r["status"] == "REJECTED_CONTEXT"
        ],
    }
    validation = {
        "live": LIVE,
        "items": [
            {
                "item": i,
                "passed": i in ("BALANCE", "POSITIONS") or None,
                "finished_at": iso(now - timedelta(days=1))
                if i in ("BALANCE", "POSITIONS")
                else None,
                "api_version": "v1",
                "notes": "read-only check" if i in ("BALANCE", "POSITIONS") else None,
                "evidence": {},
            }
            for i in ["BALANCE", "POSITIONS", *LIVE["failing_items"]]
        ],
    }
    overview = {
        "symbol": "BTCUSDT",
        "symbol_display": "BTC/USDT",
        "system": system,
        "market": market,
        "spec": SPEC,
        "live": LIVE,
        "collector": col,
        "wallet": wallet,
        "active": active,
        "quality": {"counts": market["quality"]["counts"], "window_minutes": 60},
        "performance": perf or {"session": None},
    }
    return qx.jsonable(
        {
            "/api/overview": overview,
            "setups": {
                "buckets": list(qx.FILTERS),
                "primary": list(qx.PRIMARY_FILTERS),
                "counts": counts,
                "items": rows,
            },
            "details": details,
            "/api/market/candles": {"tf": "1m", "items": c},
            "/api/market/quality": quality(now, 4 if state == "J" else 0),
            "/api/market/zones": zones_fixture(c),
            "/api/counterfactuals": cf,
            "/api/validation": validation,
            "/api/collector": col,
            "/api/feed": fd,
            "/api/performance": perf or {"session": None},
            "/api/integrity": integrity(now),
            "/api/indicators": indicators(now),
            "/api/pgaps": pgaps(),
            "/api/live/snapshot": live_events(c)[0],
            "live_stream": live_events(c),
        }
    )


def zones_fixture(c: list[dict[str, Any]]) -> dict[str, Any]:
    """S/R zones exactly as the backend builds them (analytics/sr_zones) from the fixture
    candles, plus one 4h zone per side around the range so every timeframe is shown."""
    from datetime import datetime
    from decimal import Decimal

    from sp2l.analytics.sr_zones import compute
    from sp2l.core.types import Candle

    bars = [
        Candle(
            datetime.fromisoformat(x["open_time"]),
            *(Decimal(str(x[k])) for k in ("open", "high", "low", "close")),
        )
        for x in c
        if not x.get("missing")
    ]
    last = bars[-1]
    zs = compute(bars, last.close, last.open_time + timedelta(minutes=1), ("15m", "30m"), 3)
    hi, lo = max(b.high for b in bars), min(b.low for b in bars)
    out = [
        {"timeframe": z.timeframe, "kind": z.kind, "bottom": z.bottom, "top": z.top,
         "since": z.since, "pivots": z.pivots, "label": f"{z.timeframe} {z.kind[0]}"}
        for z in zs
    ] + [
        {"timeframe": "4h", "kind": "RESISTANCE", "bottom": hi, "top": hi + 20, "since": bars[0].open_time - timedelta(days=1), "pivots": 1, "label": "4h R"},
        {"timeframe": "4h", "kind": "SUPPORT", "bottom": lo - 20, "top": lo, "since": bars[0].open_time - timedelta(days=1), "pivots": 1, "label": "4h S"},
    ]  # fmt: skip
    return {"timeframes": ["15m", "30m", "4h"], "price": last.close, "computed_at": last.open_time,
            "note": "Display only", "zones": out}  # fmt: skip


# real P-Gaps from the V5.9 replay of the server's canonical data (docs/pgap_quality_report.json)
REAL_PGAPS: dict[str, Any] = __import__("json").loads(
    (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "docs"
        / "pgap_quality_report.json"
    ).read_text()
)["examples"]


def pgaps() -> dict[str, Any]:
    items = []
    for key in (
        "LONG_VALID",
        "LONG_WEAK_BODY",
        "LONG_WEAK_GAP",
        "LONG_WRONG_DIRECTION",
        "SHORT_VALID",
        "SHORT_WEAK_BODY",
        "SHORT_WEAK_GAP",
        "SHORT_WRONG_DIRECTION",
    ):
        ex = REAL_PGAPS[key]
        q = ex["quality"]
        reason = None if q["final_pgap_pass"] else q["failure_reasons"][0]
        items.append(
            {
                "id": len(items) + 1,
                "side": key.split("_")[0],
                "confirmed_at": ex["c3"]["t"],
                "impulse_time": ex["c2"]["t"],
                "promoted": q["final_pgap_pass"],
                "not_promoted_reason": reason,
                "spec_version": "5.9",
                "view": px.pgap_quality_view(q, "5.9"),
                "outcome": px.pgap_outcome(q["final_pgap_pass"], reason),
            }
        )
    items.append(
        {
            "id": 99,
            "side": "LONG",
            "confirmed_at": REAL_PGAPS["LONG_VALID"]["c3"]["t"],
            "impulse_time": REAL_PGAPS["LONG_VALID"]["c2"]["t"],
            "promoted": True,
            "not_promoted_reason": None,
            "spec_version": "5.8",
            "view": px.pgap_quality_view(None, "5.8"),
            "outcome": px.pgap_outcome(True, None),
        }
    )
    return {"items": items}


def live_events(c: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the SSE stream sends: snapshot, a burst of forming updates for the minute after
    the last stored candle, its final canonical candle, then the next minute forming."""
    last = c[-1]
    t_prev = __import__("datetime").datetime.fromisoformat(last["open_time"])
    m1 = (t_prev + timedelta(minutes=1)).isoformat()
    m2 = (t_prev + timedelta(minutes=2)).isoformat()
    base = D(last["close"])
    evs: list[dict[str, Any]] = [
        {"type": "snapshot", "forming": [], "price": str(base), "last_final": last["open_time"]}
    ]
    o = h = lo = cl = base
    v = D(0)
    for i in range(200):  # rapid trades: the chart must update in place, never be recreated
        cl = (base + D(str((i % 17) - 8)) / 2).quantize(D("0.1"))
        h, lo, v = max(h, cl), min(lo, cl), v + D("0.001")
        evs.append(
            {
                "type": "trade",
                "t": m1,
                "o": str(o),
                "h": str(h),
                "l": str(lo),
                "c": str(cl),
                "v": str(v),
                "n": i + 1,
                "price": str(cl),
                "recv_ts": iso(_now()),
                "src": "WS",
            }
        )
    evs.append(
        {
            "type": "final",
            "t": m1,
            "status": "OK",
            "quality": "LIVE_RECONCILED",
            "o": str(o),
            "h": str(h + D("0.5")),  # REST reconciliation added one higher trade
            "l": str(lo),
            "c": str(cl),
            "v": str(v + D("0.002")),
            "n": 201,
        }
    )
    nxt = cl + D("1.5")
    evs.append(
        {
            "type": "trade",
            "t": m2,
            "o": str(nxt),
            "h": str(nxt),
            "l": str(nxt),
            "c": str(nxt),
            "v": "0.004",
            "n": 1,
            "price": str(nxt),
            "recv_ts": iso(_now()),
            "src": "WS",
        }
    )
    return evs


def indicators(now: datetime) -> dict[str, Any]:
    """Real engine snapshots over a synthetic M5 series (backend presentation code)."""
    from dataclasses import replace

    from sp2l.indicators.m5_state import M5State
    from sp2l.indicators.snapshot import indicator_snapshot
    from sp2l.marketdata.m5_aggregator import M5Result, M5Status
    from tests.conftest import random_walk

    end = now.replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % 5)
    bars = random_walk(200, 7, 84000.0)
    st, snaps = M5State(150), []
    for i, c in enumerate(bars):
        t = end - timedelta(minutes=5 * (len(bars) - i))
        c = replace(c, open_time=t, volume=D("1") + D(i % 7) / 10, trade_count=40 + i % 9)
        st.add(M5Result(t, M5Status.OK, c))
        snaps.append(
            {
                "snapshot": indicator_snapshot(st),
                "recorded_at": iso(t + timedelta(minutes=5, seconds=3)),
            }
        )
    return px.indicator_view(list(reversed(snaps[-48:])), now, D("84012.5"))


def integrity(now: datetime) -> dict[str, Any]:
    g = now - timedelta(minutes=47)
    return {
        "repairs": [
            {
                "gap_start": iso(g),
                "gap_end": iso(g + timedelta(seconds=56)),
                "reason": "A:CLOSED_NO_FRAME",
                "status": "REPAIRED",
                "method": "REST_RECENT_TRADES",
                "failure": None,
                "trades_recovered": 25,
                "detail": {"matched_live": 3},
                "resolved_at": iso(g + timedelta(seconds=58)),
            },
            {
                "gap_start": iso(g - timedelta(hours=2)),
                "gap_end": iso(g - timedelta(hours=2, minutes=-26)),
                "reason": "B:CLOSED_NO_FRAME",
                "status": "UNRECOVERED",
                "method": "NONE",
                "failure": "GAP_TOO_LONG_FOR_REPAIR",
                "trades_recovered": 0,
                "detail": {},
                "resolved_at": iso(g - timedelta(hours=1, minutes=30)),
            },
        ],
        "revisions": [
            {
                "timeframe": "1m",
                "open_time": iso(now - timedelta(hours=9)),
                "revision": 1,
                "old": {
                    "high": "84006.9",
                    "low": "84002.7",
                    "close": "84005.7",
                    "volume": "0.11204",
                    "trade_count": 30,
                },
                "new": {
                    "high": "84007.4",
                    "low": "84002.7",
                    "close": "84005.7",
                    "volume": "0.1178",
                    "trade_count": 32,
                },
                "old_quality": "LIVE_PROVEN_RAW",
                "new_quality": "CONFLICTED",
                "source": "reconcile-history",
                "reason": "PRE_FIX_SEQUENCE_DEDUP_DROPPED_MULTIFILLS; DROPPED_MULTIFILLS_RECOVERED_FROM_CONFLICT_LOG",
                "reconciled_at": iso(now - timedelta(hours=1)),
            },
        ],
        "latest_canonical_minute": iso(now - timedelta(minutes=1)),
        "latency_1h": [
            {"stage": "finalize", "n": 60, "p50": 3100, "p90": 3600, "p99": 5200},
            {"stage": "strategy_eval", "n": 60, "p50": 3300, "p90": 3900, "p99": 5500},
        ],
        "revision_total": 280,
        "revision_counts": [
            {"timeframe": "1m", "reason": "PRE_FIX_SEQUENCE_DEDUP_DROPPED_MULTIFILLS", "n": 233}
        ],
        "conflict_audit": [
            {"run_id": 6, "classification": "MULTI_FILL_SAME_SEQUENCE", "n": 25, "sequences": 19},
            {"run_id": 7, "classification": "MULTI_FILL_SAME_SEQUENCE", "n": 403, "sequences": 289},
        ],
        "quality_24h": [
            {"quality": "CONFLICTED", "n": 239},
            {"quality": "LIVE_PROVEN_RAW", "n": 1102},
            {"quality": "REPAIRED_TABDEAL", "n": 6},
        ],
    }
