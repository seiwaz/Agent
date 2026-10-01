"""Human-first presentation DTOs (UI-02: the backend stays authoritative).

Pure functions: stored rows in, display DTOs out. Nothing here evaluates a strategy rule —
every PASS/FAIL, state and reason comes from values the engine/collector already recorded;
this module only names them for people (human label + tone) and keeps the exact internal
code alongside, so technical detail is never lost. Tones: ok (healthy/ready), warn
(attention/degraded), bad (genuine fault/unsafe), info (in progress), neutral (waiting).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

# ---- vocabulary ------------------------------------------------------------------------

REASONS: dict[str, str] = {
    # Context
    "CONTEXT_UNKNOWN_WARMUP": "Not enough market history yet (Context warmup)",
    "CONTEXT_NET_TP_NOT_POSITIVE": "Even a win at target would not cover trading costs",
    "CONTEXT_NET_TP_UNKNOWN": "Trading costs unknown: net profit at target cannot be checked",
    "ADVISORY_WOULD_REJECT": "Exhaustion would have rejected (advisory only, not a gate)",
    "ADVISORY_UNKNOWN": "Exhaustion could not be evaluated (advisory only, not a gate)",
    "CONTEXT_INVALID_DATA": "Market structure data is inconsistent (conflicting swing points)",
    "RANGE_MIDDLE": "Setup formed in the middle of a range",
    "HTF_OPPOSITE_NO_BREAKOUT": "Against the higher-timeframe trend without a confirmed breakout",
    "NO_VALID_CONTEXT": "No supporting context: no level break, channel edge or trend alignment",
    "ROOM_TO_TP_INSUFFICIENT": "Not enough space to target",
    "LOW_LIQUIDITY": "Market activity too low",
    "LIQUIDITY_UNKNOWN": "Market activity could not be measured",
    "CONTEXT_INVALIDATED_BEFORE_FILL": "Context became invalid before the entry filled",
    # Exhaustion
    "EXHAUSTION_RISK": "Move appears overextended late in the trend",
    "EXHAUSTION_UNKNOWN": "Exhaustion could not be evaluated (missing data)",
    "EXHAUSTION_INVALIDATED_BEFORE_FILL": "Move became exhausted before the entry filled",
    "LATE_TREND_AGE": "Trend is already old",
    "LONG_MICROCHANNEL": "Long one-directional run without a pause",
    "SHORT_MICROCHANNEL": "Long one-directional run without a pause",
    "EXTREME_STRETCH": "Price is stretched far from its average",
    "CLIMACTIC_SPIKE": "Spike is unusually large (climactic)",
    "OUTER_EDGE_20": "Price is at the outer edge of its recent range",
    "OPPOSING_SWING_NEAR": "An opposing swing level is close",
    "FRESH_BREAKOUT_EXCEPTION": "Fresh breakout — exhaustion exception applied",
    # Risk
    "RISK_OVER_BUDGET": "Planned loss would exceed the risk budget",
    "RISK_NET_TP_NOT_POSITIVE": "Even a win at target would not cover trading costs",
    "RISK_QTY_BELOW_MIN": "Position size would be below the exchange minimum",
    "RISK_QTY_ZERO": "Position size rounds down to zero",
    "RISK_NOTIONAL_BELOW_MIN": "Order value would be below the exchange minimum",
    "RISK_COSTS_NOT_CONFIGURED": "Trading costs are not configured",
    "RISK_COSTS_UNVERIFIED": "Trading costs are not verified",
    "RISK_FILTERS_UNVERIFIED": "Exchange order filters are not verified",
    "RISK_LIQUIDATION_UNSAFE": "Liquidation would be too close to the stop",
    "RISK_MARGIN_MODEL_UNVERIFIED": "Margin model is not verified",
    "RISK_WALLET_INVALID": "Wallet balance is unavailable or invalid",
    "RISK_INVALIDATED_BEFORE_FILL": "Risk check failed before the entry filled",
    "MARGIN_CAPPED_QTY": "Size reduced to fit available margin",
    "LIQ_UNVERIFIED": "Liquidation price not verified in Shadow",
    "COSTS_PROVISIONAL": "Trading costs are provisional",
    "FILTERS_PROVISIONAL": "Exchange filters are provisional",
    # Entry / orders
    "E1_NEVER_ARMED": "Entry order was never placed",
    "E1_NOT_REARMED": "Entry order was not placed again",
    "E1_NOT_SAFE": "Entry could not be placed safely (price already beyond the entry)",
    "E1_SUBMIT_REJECTED": "Entry order was rejected",
    "FILL_WINDOW_EXPIRED": "Price reached the entry but it did not fill in time",
    "FILL_DURING_CANCEL": "Entry filled while it was being cancelled",
    "CANCEL_UNCONFIRMED": "An order cancel could not be confirmed",
    "E1_REMAINDER_CANCEL_UNCONFIRMED": "Cancel of the unfilled entry remainder not confirmed",
    "E2_SKIPPED": "Second entry skipped",
    "E2_REJECTED": "Second entry order was rejected",
    "PROTECTION_FAILED": "Stop/target protection could not be placed",
    "PROTECTION_UNVERIFIED": "Stop/target protection could not be verified",
    "NOT_FLAT_AFTER_CLOSE": "Position was not flat after close",
    "EMERGENCY_CLOSE": "Emergency close",
    "EMERGENCY_CLOSED": "Closed by emergency procedure",
    "CAPACITY_BUSY": "Another setup already holds the slots",
    "NOT_FIRST_IN_RUN": "Not the first gap of this run",
    # Data
    "DATA_GAP_WHILE_ACTIVE": "Market data gap while the setup was active",
    "AMBIGUOUS_DATA_GAP": "Result uncertain due to missing market data",
    # Live
    "LIVE_AUTOMATION_DISABLED": "Real trading disabled",
}

# status -> (human result, tone, stage where the candidate is/stopped)
STATUS: dict[str, tuple[str, str, str]] = {
    "BASE_SPIKE_CONFIRMED": ("Checking", "info", "Spike"),
    "E1_PREPARING": ("Preparing entry", "info", "E1"),
    "E1_PENDING": ("Entry order placed", "info", "E1"),
    "E1_REPRICING": ("Updating entry", "info", "E1"),
    "PULLBACK_DETECTED": ("Pullback reached entry", "info", "Pullback"),
    "E1_PARTIAL": ("Entry partly filled", "info", "E1 Fill"),
    "E1_FILLED": ("Entry filled", "info", "E1 Fill"),
    "E2_VALIDATING": ("Checking second entry", "info", "E2"),
    "E2_PENDING": ("Second entry placed", "info", "E2"),
    "E2_PARTIAL": ("Second entry partly filled", "info", "E2"),
    "POSITION_ACTIVE": ("Position open", "info", "Position"),
    "FINALIZING": ("Closing", "info", "Position"),
    "CLOSED": ("Traded", "ok", "Position"),
    "REJECTED_CONTEXT": ("Rejected", "bad", "Context"),
    "REJECTED_EXHAUSTION": ("Rejected", "bad", "Exhaustion"),
    "REJECTED_RISK": ("Rejected", "bad", "E1"),
    "EXPIRED_UNARMED": ("Expired", "neutral", "E1"),
    "EXPIRED_NO_FILL": ("Expired — no fill", "neutral", "E1 Fill"),
    "AMBIGUOUS_DATA_GAP": ("Uncertain", "warn", "Position"),
    "ERROR_HOLD": ("On hold — error", "bad", "Position"),
    "SESSION_ENDED": ("Session ended", "neutral", "—"),  # display only (queries.SESSION_ENDED)
}
ENTERED = {
    "E1_PARTIAL",
    "E1_FILLED",
    "E2_VALIDATING",
    "E2_PENDING",
    "E2_PARTIAL",
    "POSITION_ACTIVE",
    "FINALIZING",
}

STAGE_NAMES = {
    "SPIKE": "Spike",
    "CONTEXT": "Context",
    "EXHAUSTION": "Exhaustion",
    "E1": "E1",
    "PULLBACK": "Pullback",
    "E1_FILL": "E1 Fill",
    "E2": "E2",
    "POSITION": "Position",
}
STAGE_HELP = {
    "SPIKE": "A strong directional move (P-Gap) was confirmed",
    "CONTEXT": "Does a win cover costs, and does the spike break a level, start at a channel"
    " edge or follow the trend?",
    "EXHAUSTION": "Is the move already too stretched or too late? (advisory, not a gate)",
    "E1": "First entry order at the end of the spike",
    "PULLBACK": "Price came back to the entry",
    "E1_FILL": "First entry filled within its fill window",
    "E2": "Optional second entry halfway to the stop",
    "POSITION": "Open position protected by one stop and one target",
}
REGIME = {
    "TREND": "Trending",
    "RANGE": "Ranging",
    "TRANSITION": "Transitioning",
    "UNKNOWN": "Unknown",
}
TREND = {
    "BULL": "Bullish",
    "BEAR": "Bearish",
    "NEUTRAL": "Neutral",
    "NEUTRAL_INSUFFICIENT_STRUCTURE": "Neutral (not enough structure)",
    "INVALID_DUAL_PIVOT": "Invalid (conflicting swing points)",
}


def reason(code: str | None) -> dict[str, str] | None:
    if not code:
        return None
    return {"code": code, "label": REASONS.get(code, code.replace("_", " ").capitalize())}


def reasons(codes: list[str] | None) -> list[dict[str, str]]:
    return [r for r in (reason(c) for c in (codes or [])) if r is not None]


def _state(key: str, label: str, tone: str, detail: str | None = None) -> dict[str, Any]:
    return {"state": key, "label": label, "tone": tone, "detail": detail}


def num(v: Any, dp: int = 2) -> str | None:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except ArithmeticError:
        return str(v)
    if not d.is_finite():
        return str(v)
    return f"{d:,.{dp}f}"


def _minute(iso: str) -> str:
    return datetime.fromisoformat(iso).replace(second=0, microsecond=0).isoformat()


def _duration(seconds: float) -> str:
    m = int(seconds // 60)
    h, m = divmod(m, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


# ---- system status ---------------------------------------------------------------------


def feed_summary(
    collector: dict[str, Any],
    m5: dict[str, Any],
    conflicts: dict[str, int],
    repairs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hb = collector.get("heartbeat") or {}
    detail = hb.get("detail") or {}
    conns = detail.get("connections") or {}
    fresh = collector.get("status") in ("CONNECTED", "DISCONNECTED")
    items = []
    for name in sorted(conns):
        c = conns[name]
        up = bool(fresh and c.get("connected") and c.get("confirmed"))
        items.append(
            {
                "name": f"Connection {name}",
                "label": "Healthy" if up else "Reconnecting",
                "tone": "ok" if up else "warn",
            }
        )
    up_n = sum(1 for i in items if i["tone"] == "ok")
    covered = fresh and detail.get("merged_status") == "COVERED"
    cur, tgt = int(m5.get("current_bars") or 0), int(m5.get("warmup_target") or 0)
    remaining = max(tgt - cur, 0)
    return {
        "connections_up": up_n,
        "connections_total": len(items),
        "connections": items,
        "coverage": {
            "label": "Complete" if covered else "Gap",
            "tone": "ok" if covered else "bad",
        },
        "warmup": {
            "current": cur,
            "target": tgt,
            "percent": round(min(cur, tgt) * 100 / tgt, 1) if tgt else None,
            "complete": tgt > 0 and cur >= tgt,
            "remaining_text": (
                f"≈ {_duration(remaining * 300)} of uninterrupted data still needed"
                if remaining
                else None
            ),
        },
        "conflicts": conflicts,
        "repair": _repair_summary(detail.get("repair") or {}, repairs or {}, m5),
    }


def _repair_summary(
    live: dict[str, Any], rec: dict[str, Any], m5: dict[str, Any]
) -> dict[str, Any]:
    """V5.6: what the collector is doing about gaps right now, and what it recently did."""
    last_unrec = rec.get("last_unrecovered")
    seg_first = ((m5.get("current") or {}).get("first")) if m5 else None
    return {
        "enabled": bool(live.get("enabled")),
        "pending": bool(live.get("gap_pending", live.get("pending"))),
        "synchronizing_history": bool(live.get("synchronizing_history")),
        "repaired_last_hour": int(rec.get("repaired_last_hour") or 0),
        "unrecovered_last_hour": int(rec.get("unrecovered_last_hour") or 0),
        "last_repaired": rec.get("last_repaired"),
        "last_unrecovered": last_unrec,
        # the current warmup segment began because of an unrecoverable gap
        "warmup_after_unrecovered": bool(
            last_unrec and seg_first and str(last_unrec.get("end")) >= str(seg_first)[:19]
        ),
    }


def system_status(
    *,
    continuity: dict[str, Any] | None = None,
    leverage: dict[str, Any] | None = None,
    collector: dict[str, Any],
    feed: dict[str, Any],
    costs_problem: str | None,
    session: dict[str, Any] | None,
    active: list[dict[str, Any]],
    last_candidate: dict[str, Any] | None,
    live: dict[str, Any],
    manifest_ok: bool,
    session_activity_at: str | None,
) -> dict[str, Any]:
    """Four separate concepts (DATA / STRATEGY / SHADOW / LIVE) plus the current blocker.
    CONNECTED alone never means the strategy is ready."""
    notes: list[dict[str, str]] = []
    blockers: list[dict[str, Any]] = []

    # DATA
    cstat = collector.get("status")
    if cstat in (None, "NO_DATA"):
        data = _state("OFFLINE", "No data", "bad", "The market-data collector has not reported")
        blockers.append(
            {
                "code": "COLLECTOR_NO_DATA",
                "text": "Market-data collector is not running",
                "tone": "bad",
            }
        )
    elif cstat == "STALE":
        age = collector.get("heartbeat_age_s")
        data = _state("OFFLINE", "Offline", "bad", f"Last collector report {age} s ago")
        blockers.append(
            {
                "code": "COLLECTOR_STALE",
                "text": "Market-data collector stopped reporting",
                "tone": "bad",
            }
        )
    elif feed["repair"]["synchronizing_history"]:
        data = _state(
            "SYNCHRONIZING_HISTORY",
            "Synchronizing history…",
            "info",
            "Restoring missed minutes from Tabdeal's validated chart history; indicators are kept",
        )
    elif feed["repair"]["pending"] and feed["connections_up"] > 0:
        data = _state(
            "REPAIRING",
            "Repairing market data…",
            "info",
            "Recovering missed trades from Tabdeal; indicators are kept",
        )
    elif feed["connections_up"] == 0:
        data = _state("RECONNECTING", "Reconnecting", "bad", "No live connection to the exchange")
        blockers.append(
            {
                "code": "FEED_DOWN",
                "text": "Market data is reconnecting — coverage is interrupted",
                "tone": "bad",
            }
        )
    elif feed["coverage"]["label"] != "Complete":
        data = _state("GAP", "Gap detected", "bad", "Merged coverage has a gap")
        blockers.append(
            {
                "code": "MERGED_GAP",
                "text": "Market data coverage has a gap — warmup restarts after it",
                "tone": "bad",
            }
        )
    elif feed["connections_up"] < feed["connections_total"]:
        data = _state(
            "DEGRADED",
            "Degraded",
            "warn",
            f"{feed['connections_up']} / {feed['connections_total']} connections · merged coverage still complete",
        )
        notes.append(
            {
                "code": "FEED_DEGRADED",
                "text": "One market-data connection is reconnecting; coverage is still complete",
                "tone": "warn",
            }
        )
    else:
        redundant = feed["connections_total"] > 1
        data = _state(
            "HEALTHY",
            "Healthy",
            "ok",
            f"{feed['connections_up']} / {feed['connections_total']} connections · "
            + ("redundant coverage" if redundant else "coverage complete"),
        )
    rp = feed["repair"]
    if rp["repaired_last_hour"]:
        n = rp["repaired_last_hour"]
        notes.append(
            {
                "code": "GAPS_REPAIRED",
                "text": f"{n} short gap{'s' if n != 1 else ''} repaired exactly from Tabdeal in the last hour — indicators kept",
                "tone": "ok",
            }
        )
        last = rp["last_repaired"] or {}
        if data["state"] == "HEALTHY" and last.get("recent"):
            if last.get("method") == "TABDEAL_HISTORY":
                data = {**data, "state": "HISTORY_RESTORED", "label": "History restored"}
            else:
                data = {**data, "state": "REPAIRED", "label": "Repaired"}
    if rp["unrecovered_last_hour"]:
        n = rp["unrecovered_last_hour"]
        notes.append(
            {
                "code": "GAPS_UNRECOVERED",
                "text": f"{n} gap{'s' if n != 1 else ''} could not be repaired in the last hour — see Diagnostics",
                "tone": "warn",
            }
        )
        if data["tone"] == "ok" and rp["last_unrecovered"] and rp["last_unrecovered"].get("recent"):
            data = {
                **data,
                "state": "UNRECOVERED_GAP",
                "label": "Unrecoverable gap",
                "tone": "warn",
            }
    cf = feed["conflicts"]
    if cf.get("current_run"):
        if data["tone"] == "ok":
            data = {**data, "state": "DEGRADED", "label": "Degraded", "tone": "warn"}
        notes.append(
            {
                "code": "PAYLOAD_CONFLICTS",
                "text": f"{cf['current_run']} payload conflicts in the current collector run — check Diagnostics",
                "tone": "warn",
            }
        )
    elif cf.get("last_24h"):
        notes.append(
            {
                "code": "PAYLOAD_CONFLICTS_24H",
                "text": f"{cf['last_24h']} payload conflicts recorded in the last 24 h (none in the current run)",
                "tone": "warn",
            }
        )

    # STRATEGY
    warm = feed["warmup"]
    active_now = [a for a in active if a.get("status") not in ("ERROR_HOLD",)]
    held = [a for a in active if a.get("status") == "ERROR_HOLD"]
    if not manifest_ok:
        strategy = _state("BLOCKED", "Blocked", "bad", "Spec integrity check failed")
        blockers.insert(
            0,
            {
                "code": "SPEC_MANIFEST_MISMATCH",
                "text": "Spec files do not match their pinned hashes",
                "tone": "bad",
            },
        )
    elif held:
        strategy = _state("BLOCKED", "Blocked", "bad", "A setup is on hold after an error")
        blockers.append(
            {
                "code": "ERROR_HOLD",
                "text": "A setup is on hold after an error — review it before continuing",
                "tone": "bad",
            }
        )
    elif not warm["complete"] and feed["repair"]["synchronizing_history"]:
        strategy = _state(
            "SYNCHRONIZING", "Synchronizing history…", "info", "Context waits for the repair"
        )
        blockers.append(
            {
                "code": "HISTORY_SYNC",
                "text": "Synchronizing history… — Context is restored when the repair completes",
                "tone": "info",
            }
        )
    elif not warm["complete"]:
        strategy = _state(
            "WARMING_UP", "Warming up", "neutral", f"{warm['current']} / {warm['target']} M5 bars"
        )
        after_gap = feed["repair"]["warmup_after_unrecovered"]
        blockers.append(
            {
                "code": "CONTEXT_WARMUP",
                "text": (
                    f"Rebuilding Context history — {warm['current']} / {warm['target']} (after an unrecoverable gap)"
                    if after_gap
                    else f"Building Context history — {warm['current']} / {warm['target']} M5 bars"
                ),
                "tone": "neutral",
            }
        )
    elif session is None:
        strategy = _state("READY", "Context ready", "ok", "Warmup complete · Shadow not started")
    elif active_now:
        strategy = _state(
            "SETUP_ACTIVE", "Setup active", "info", STATUS.get(active_now[0]["status"], ("",))[0]
        )
    else:
        strategy = _state(
            "SCANNING", "Scanning", "ok", "Context ready · looking for the next valid setup"
        )
    if continuity is not None:
        strategy["context"] = {
            "price_ready": continuity["price_context_ready"],
            "liquidity_ready": continuity["liquidity_context_ready"],
            "reason": continuity["ready_reason"],
        }
        if continuity["price_context_ready"] and not continuity["liquidity_context_ready"]:
            notes.append(
                {
                    "code": "LIQUIDITY_UNKNOWN_AFTER_HISTORY",
                    "text": continuity["ready_reason"],
                    "tone": "info",
                }
            )

    # SHADOW
    if session is None:
        if costs_problem:
            shadow = _state(
                "NOT_STARTED", "Not started", "neutral", "Trading costs are not configured"
            )
            blockers.append(
                {
                    "code": "SHADOW_COSTS_NOT_CONFIGURED",
                    "text": "Shadow costs are not configured",
                    "tone": "warn",
                }
            )
        else:
            shadow = _state("NOT_STARTED", "Not started", "neutral", "No Shadow session")
            blockers.append(
                {
                    "code": "SHADOW_NOT_STARTED",
                    "text": "Shadow has not been started",
                    "tone": "neutral",
                }
            )
    elif any(a.get("status") in ENTERED for a in active):
        shadow = _state("POSITION_OPEN", "Position open", "info", "Shadow position is open")
    else:
        shadow = _state(
            "SESSION_OPEN",
            "Session open",
            "ok",
            "Engine liveness is not reported; see last recorded activity",
        )
        shadow["activity_at"] = session_activity_at

    # LIVE
    failing = live.get("failing_items") or []
    if live.get("status") == "LIVE_AUTOMATION_DISABLED":
        live_s = (
            _state(
                "VALIDATION_REQUIRED",
                "Disabled",
                "neutral",
                f"Real trading disabled · {len(failing)} validation checks not passed",
            )
            if failing
            else _state("READY_DISABLED", "Ready but disabled", "neutral", "Real trading disabled")
        )
    else:
        live_s = _state(str(live.get("status")), "Review required", "bad", str(live.get("status")))
    live_s["code"] = live.get("status")
    if leverage is not None:
        live_s["leverage"] = leverage
        if leverage.get("live_blocker"):
            live_s["detail"] = f"{live_s['detail']} · {leverage['text']}"
            notes.append(
                {"code": leverage["live_blocker"], "text": leverage["text"], "tone": "warn"}
            )

    if not blockers:
        if active_now:
            blockers.append(
                {
                    "code": "SETUP_ACTIVE",
                    "text": "Setup in progress — see the strategy flow",
                    "tone": "info",
                }
            )
        elif (
            last_candidate
            and last_candidate.get("primary_reason")
            and str(last_candidate.get("status", "")).startswith("REJECTED")
        ):
            r = reason(last_candidate["primary_reason"]) or {"label": ""}
            blockers.append(
                {
                    "code": "LAST_REJECTED",
                    "text": f"No valid SP2L setup right now · last candidate rejected: {r['label']}",
                    "tone": "neutral",
                }
            )
        else:
            blockers.append(
                {"code": "SCANNING", "text": "No valid SP2L setup right now", "tone": "neutral"}
            )
    for b in blockers:  # informational "right now" lines are not blockers
        b["blocking"] = b["code"] not in ("SETUP_ACTIVE", "SCANNING", "LAST_REJECTED")
    return {
        "data": data,
        "strategy": strategy,
        "shadow": shadow,
        "live": live_s,
        "blocker": blockers[0],
        "other_blockers": blockers[1:],
        "notes": notes,
    }


# ---- candidates ------------------------------------------------------------------------


def candidate_row(r: dict[str, Any]) -> dict[str, Any]:
    status = r["status"]
    result, tone, stage = STATUS.get(status, (status, "neutral", "—"))
    if status == "CLOSED" and r.get("exit_kind"):
        result = {"TP": "Traded — target hit", "SL": "Traded — stop hit"}.get(
            r["exit_kind"], f"Traded — {r['exit_kind']}"
        )
        tone = {"TP": "ok", "SL": "bad"}.get(r["exit_kind"], "neutral")
    if status in ("REJECTED_CONTEXT", "REJECTED_EXHAUSTION", "REJECTED_RISK"):
        where = f"Rejected at {stage}"
    elif status in STATUS and status not in (
        "CLOSED",
        "AMBIGUOUS_DATA_GAP",
        "ERROR_HOLD",
        "SESSION_ENDED",
    ):
        where = f"Expired at {stage}" if status.startswith("EXPIRED") else f"Current stage: {stage}"
    else:
        ends: dict[str, str] = {
            "CLOSED": "Position closed",
            "AMBIGUOUS_DATA_GAP": "Position — result uncertain",
            "ERROR_HOLD": "Held at Position",
            "SESSION_ENDED": "Stopped when its Shadow session was replaced",
        }
        where = ends[status] if status in ends else str(stage)
    return {
        **r,
        "stage_label": where,
        "result": {"code": status, "label": result, "tone": tone},
        "reason_human": reason(r.get("primary_reason")),
    }


def flow_human(
    flow: list[dict[str, str]], status: str, e2_order_live: bool = False
) -> list[dict[str, Any]]:
    out = []
    for f in flow:
        s, stage = f["status"], f["stage"]
        label, tone = {
            "pending": ("Waiting", "neutral"),
            "failed": ("Rejected", "bad"),
            "skipped": ("Skipped", "neutral"),
            "ambiguous": ("Uncertain", "warn"),
        }.get(s, ("", ""))
        if s == "done":
            label, tone = {
                "E1": ("Placed", "ok"),
                "E1_FILL": ("Filled", "ok"),
                "E2": ("Filled", "ok"),
                "POSITION": ("Completed", "ok"),
            }.get(stage, ("Passed", "ok"))
        elif s == "current":
            label, tone = (
                ("Checking", "info")
                if stage in ("CONTEXT", "EXHAUSTION")
                else ("Active", "info")
                if stage == "POSITION" or (stage == "E2" and e2_order_live)
                else ("Waiting", "info")
            )
        elif s == "failed" and status.startswith("EXPIRED"):
            label, tone = "Expired", "neutral"
        elif s == "pending" and status in STATUS and STATUS[status][1] != "info":
            label, tone = "Not reached", "neutral"  # the candidate already ended
        out.append(
            {
                **f,
                "name": STAGE_NAMES.get(stage, stage),
                "help": STAGE_HELP.get(stage),
                "label": label,
                "tone": tone,
            }
        )
    return out


# ---- Context / Exhaustion: human rows over the recorded booleans ------------------------


def _row(name: str, value: str, tone: str, code: str | None = None) -> dict[str, Any]:
    return {"name": name, "value": value, "tone": tone, "code": code}


def _snap(s: dict[str, Any], key: str) -> Any:
    """A recorded snapshot value: its DB column, else the exact JSON (V6.0 fields)."""
    if key in s and s[key] is not None:
        return s[key]
    return (s.get("exact") or {}).get(key)


def context_summary(s: dict[str, Any] | None) -> dict[str, Any] | None:
    """V6.0: NetTP AND (LevelBreak OR ChannelEdge OR HTFAligned). The V5 measures below
    the result are INFO only (they never reject since V6.0)."""
    if s is None:
        return None
    yn = {True: "Yes", False: "No", None: "Not evaluated"}
    net, pos = _snap(s, "net_tp_per_unit"), _snap(s, "net_tp_positive")
    reasons_ = list(s.get("reasons") or [])
    if net is None:
        unknown = "CONTEXT_NET_TP_UNKNOWN" in reasons_
        net_row = _row(
            "Net profit at TP",
            "Costs unknown" if unknown else "Not evaluated (recorded before V6.0)",
            "bad" if unknown else "neutral",
            "CONTEXT_NET_TP_UNKNOWN" if unknown else None,
        )
    else:
        net_row = _row(
            "Net profit at TP",
            f"{num(net, 4)} per unit after fees",
            "ok" if pos else "bad",
            None if pos else "CONTEXT_NET_TP_NOT_POSITIVE",
        )

    def cond(name: str, key: str) -> dict[str, Any]:
        v = _snap(s, key)
        return _row(name, yn.get(v, str(v)), "ok" if v else "neutral")

    regime = s.get("regime")
    room = (
        "No obstacle before target"
        if s.get("room_to_tp_infinite")
        else f"{num(s.get('room_to_tp_r'))} R"
        if s.get("room_to_tp_r") is not None
        else "Not measured"
    )
    liq = s.get("liquidity_status")
    rows = [
        net_row,
        cond("Level break", "breakout_context"),
        cond("Channel edge", "range_edge_origin"),
        cond("HTF aligned", "htf_alignment"),
        _row(
            "Result",
            "At least one condition and a positive net"
            if s.get("status") == "PASS"
            else ", ".join(reasons_) or str(s.get("status")),
            "ok" if s.get("status") == "PASS" else "bad",
        ),
        # INFO (V5 measures; never a gate since V6.0)
        _row("Market regime (info)", REGIME.get(str(regime), str(regime)), "neutral"),
        _row(
            "Higher-timeframe trend (info)",
            TREND.get(str(s.get("trend")), str(s.get("trend"))),
            "neutral",
        ),
        _row("Middle of the range (info)", yn.get(s.get("range_middle_reject"), "—"), "neutral"),
        _row(
            "Against trend without breakout (info)",
            yn.get(s.get("htf_opposite_no_breakout"), "—"),
            "neutral",
        ),
        _row("Room to target (info)", room, "neutral"),
        _row(
            "Liquidity (info)",
            {"PASS": "Normal", "LOW_LIQUIDITY": "Low"}.get(str(liq), "Unknown"),
            "neutral",
        ),
    ]
    passed = s.get("status") == "PASS"
    return {
        "question": STAGE_HELP["CONTEXT"],
        "result": {
            "code": s.get("status"),
            "label": "Passed" if passed else "Rejected",
            "tone": "ok" if passed else "bad",
        },
        "reasons": reasons(s.get("reasons")),
        "rows": rows,
    }


def exhaustion_summary(s: dict[str, Any] | None) -> dict[str, Any] | None:
    if s is None:
        return None

    def trig(flag: Any, yes: str, no: str, code: str) -> tuple[str, str, str | None]:
        if flag is None:
            return "Not evaluated", "neutral", None
        return (yes, "warn", code) if flag else (no, "ok", None)

    age = s.get("trend_age_bars")
    micro = s.get("microchannel_len")
    rows = []
    v, t, c = trig(s.get("late_trend"), "Late in the trend", "Not late", "late_trend")
    rows.append(_row("Trend maturity", f"{v} · age {age} M5 bars, run {micro} bars", t, c))
    v, t, c = trig(s.get("extreme_stretch"), "Extremely stretched", "Normal", "extreme_stretch")
    rows.append(_row("Stretch from average", f"{v} · {num(s.get('stretch_atr'))} ATR", t, c))
    v, t, c = trig(s.get("climactic_spike"), "Climactic", "Normal", "climactic_spike")
    rows.append(_row("Spike size", f"{v} · {num(s.get('spike_atr'))} ATR", t, c))
    v, t, c = trig(s.get("at_outer_edge"), "At the outer edge", "Not at the edge", "at_outer_edge")
    rows.append(_row("Position in recent range", v, t, c))
    fresh = s.get("fresh_breakout_exception")
    rows.append(
        _row(
            "Fresh breakout exception",
            {True: "Applies", False: "Does not apply", None: "Not evaluated"}[fresh],
            "ok" if fresh else "neutral",
            "FRESH_BREAKOUT_EXCEPTION" if fresh else None,
        )
    )
    st = s.get("status")
    subs = list(s.get("sub_reasons") or [])
    if "ADVISORY_WOULD_REJECT" in subs:  # V6.0: advisory, never a gate
        label, tone = "Advisory: would have rejected (not a gate)", "warn"
    elif "ADVISORY_UNKNOWN" in subs:
        label, tone = "Advisory: could not evaluate (not a gate)", "warn"
    else:
        label, tone = {
            "PASS": ("Passed (advisory, not a gate)", "ok"),
            "REJECT": ("Rejected — move looks exhausted", "bad"),
        }.get(str(st), ("Unknown — could not evaluate", "warn"))
    return {
        "question": STAGE_HELP["EXHAUSTION"],
        "result": {"code": st, "label": label, "tone": tone},
        "reasons": reasons(s.get("sub_reasons")),
        "rows": rows,
    }


# ---- chart overlays ----------------------------------------------------------------------


def overlays(
    *,
    side: str,
    levels: dict[str, Any] | None,
    revisions: list[dict[str, Any]],
    origin: dict[str, Any] | None,
    pgap_time: str | None,
    spike_time: str | None,
    events: list[dict[str, Any]],
    fills: list[dict[str, Any]],
) -> dict[str, Any]:
    """Price lines and time markers for the chart, straight from recorded values."""
    lines: list[dict[str, Any]] = []
    lv = levels or {}
    for kind, label, key in (
        ("E1", "E1", "e1"),
        ("SL", "SL", "sl"),
        ("TP", "TP", "tp"),
        ("E2", "E2", "e2"),
    ):
        v = lv.get(key) if key != "e2" else lv.get("e2") or lv.get("e2_reference")
        if v is not None:
            lines.append({"kind": kind, "label": label, "price": str(v)})
    for r in revisions[:-1]:
        lines.append({"kind": "E1_PREV", "label": f"E1 r{r['revision']}", "price": str(r["e1"])})
    if origin:
        o = origin.get("origin_low") if side == "LONG" else origin.get("origin_high")
        if o is not None:
            lines.append({"kind": "ORIGIN", "label": "Origin", "price": str(o)})
    long = side == "LONG"
    markers: list[dict[str, Any]] = []
    if pgap_time:
        markers.append(
            {
                "kind": "PGAP",
                "time": pgap_time,
                "label": "P-Gap",
                "position": "below" if long else "above",
            }
        )
    if spike_time:
        markers.append(
            {
                "kind": "SPIKE",
                "time": spike_time,
                "label": "Spike",
                "position": "below" if long else "above",
            }
        )
    for e in events:
        p = e.get("payload") or {}
        if e["event_type"] == "PULLBACK_START":
            markers.append(
                {
                    "kind": "PULLBACK",
                    "time": p.get("minute") or e["ts"],
                    "label": "Pullback",
                    "position": "above" if long else "below",
                }
            )
        elif e["event_type"] == "EXIT":
            markers.append(
                {
                    "kind": "EXIT",
                    "time": e["ts"],
                    "label": f"Exit {p.get('exit_kind', '')}".strip(),
                    "position": "above" if long else "below",
                    "price": p.get("price"),
                }
            )
    for f in fills:
        markers.append(
            {
                "kind": "FILL",
                "time": f["ts"],
                "label": f"{f['leg']} fill",
                "position": "below" if long else "above",
                "price": f["price"],
            }
        )
    for m in markers:  # chart markers attach to the M1 bar that contains the event
        m["time"] = _minute(str(m["time"]))
    markers.sort(key=lambda m: str(m["time"]))
    return {"lines": lines, "markers": markers}


# ---- active position ----------------------------------------------------------------------


def position_view(
    *,
    setup: dict[str, Any],
    levels: dict[str, Any] | None,
    fills: list[dict[str, Any]],
    e2_label: str,
    last_price: Decimal | None,
    now: datetime,
) -> dict[str, Any] | None:
    """Current Shadow position from recorded fills (no fill -> no position)."""
    entries = [f for f in fills if f["leg"] in ("E1", "E2")]
    if not entries or setup["status"] not in ENTERED:
        return None
    qty = sum((Decimal(f["qty"]) for f in entries), Decimal(0))
    avg = sum((Decimal(f["price"]) * Decimal(f["qty"]) for f in entries), Decimal(0)) / qty
    sign = 1 if setup["side"] == "LONG" else -1
    lv = levels or {}
    upnl = move_r = None
    if last_price is not None:
        upnl = (last_price - avg) * qty * sign
        if lv.get("r") and lv.get("e1"):
            move_r = (last_price - Decimal(str(lv["e1"]))) * sign / Decimal(str(lv["r"]))
    since = min(datetime.fromisoformat(f["ts"]) for f in entries)
    liq = (
        "Not verified in Shadow" if "LIQ_UNVERIFIED" in (setup.get("reasons") or []) else "Unknown"
    )
    return {
        "side": setup["side"],
        "setup_key": setup["setup_key"],
        "e1": lv.get("e1"),
        "e2_state": e2_label,
        "avg_entry": num(avg, 2),
        "qty": str(qty.normalize()),
        "sl": lv.get("sl"),
        "tp": lv.get("tp"),
        "last_price": None if last_price is None else str(last_price.normalize()),
        "unrealized_pnl": num(upnl, 2),
        "unrealized_tone": None
        if upnl is None
        else ("ok" if upnl > 0 else "bad" if upnl < 0 else "neutral"),
        "move_r": num(move_r, 2),
        "opened_at": since.isoformat(),
        "duration": _duration((now - since).total_seconds()),
        "liquidation": liq,
    }


# ---- confirmed performance --------------------------------------------------------------


def performance_stats(
    initial: Decimal | None, trades: list[dict[str, Any]], started_at: str | None = None
) -> dict[str, Any]:
    """Confirmed (CLOSED) trades only; each trade carries its own net PnL (pnl + fees)."""
    nets = [Decimal(str(t["net_pnl"])) for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    total = sum(nets, Decimal(0))
    equity: list[dict[str, str]] = []
    peak = bal = initial if initial is not None else Decimal(0)
    max_dd = max_dd_pct = Decimal(0)
    if initial is not None:
        equity.append({"ts": started_at or "start", "balance": str(initial.normalize())})
    for t, net in zip(trades, nets, strict=True):
        bal += net
        equity.append({"ts": t["exit_at"], "balance": str(bal.normalize())})
        peak = max(peak, bal)
        dd = peak - bal
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd * 100 / peak if peak else Decimal(0)
    n = len(nets)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": num(Decimal(len(wins)) * 100 / n, 1) if n else None,
        "net_pnl": num(total, 2),
        "net_pnl_tone": "ok" if total > 0 else "bad" if total < 0 else "neutral",
        "expectancy": num(total / n, 2) if n else None,
        "avg_win": num(sum(wins, Decimal(0)) / len(wins), 2) if wins else None,
        "avg_loss": num(sum(losses, Decimal(0)) / len(losses), 2) if losses else None,
        "max_drawdown": num(max_dd, 2),
        "max_drawdown_pct": num(max_dd_pct, 2),
        "equity": equity,
        "unit": "USDT",
    }


# ---- live indicators (display of backend values; UI-02) ----------------------------------

INDICATORS: tuple[tuple[str, str, str, int], ...] = (
    # key, label, meaning, display decimals
    ("atr14", "ATR 14", "Average true range of the last 14 M5 bars (Wilder)", 2),
    ("adx14", "ADX 14", "Trend strength (Wilder, TA-Lib seed); ≥ 25 = trending, < 20 = weak", 2),
    ("plus_di", "+DI 14", "Upward directional movement", 2),
    ("minus_di", "−DI 14", "Downward directional movement", 2),
    ("dx", "DX", "Directional index before smoothing", 2),
    ("chop14", "CHOP 14", "Choppiness; ≥ 61.8 = choppy, ≤ 38.2 = directional", 2),
    ("ema20", "EMA 20", "20-bar exponential moving average of M5 closes", 2),
    ("range14_high", "Range 14 high", "Highest high of the last 14 M5 bars", 1),
    ("range14_low", "Range 14 low", "Lowest low of the last 14 M5 bars", 1),
    ("range_position_close", "Range position", "Close inside Range 14: 0 = low, 1 = high", 3),
    ("swing_high", "Last swing high", "Latest confirmed 2L/2R pivot high", 1),
    ("swing_low", "Last swing low", "Latest confirmed 2L/2R pivot low", 1),
    ("volume_ratio", "Volume ratio", "Bar volume ÷ median of the previous 20 bars", 3),
    ("tradecount_ratio", "Trade-count ratio", "Bar trades ÷ median of the previous 20 bars", 3),
)
STATES: tuple[tuple[str, str], ...] = (
    ("regime", "Regime"),
    ("trend", "Trend"),
    ("liquidity_status", "Liquidity"),
)


def _dnum(v: Any, dp: int) -> str | None:
    if v is None:
        return None
    return f"{Decimal(str(v)).quantize(Decimal(1).scaleb(-dp)):,}"


def indicator_view(
    snaps: list[dict[str, Any]], now: datetime, last_price: Decimal | None
) -> dict[str, Any]:
    """Latest finalized-M5 indicators with the change versus the previous bar and a short
    history per indicator. The WebUI renders this as-is (no computation client-side)."""
    if not snaps:
        return {
            "available": False,
            "text": "No indicator snapshot yet — waiting for the next M5 close",
        }
    cur = snaps[0]["snapshot"]
    prev = snaps[1]["snapshot"] if len(snaps) > 1 else None
    hist = [x["snapshot"] for x in reversed(snaps)]
    items = []
    for key, label, meaning, dp in INDICATORS:
        v, pv = cur.get(key), (prev or {}).get(key)
        change, delta = None, None
        if v is not None and pv is not None:
            d = Decimal(v) - Decimal(pv)
            change = "up" if d > 0 else "down" if d < 0 else "same"
            delta = f"{'+' if d > 0 else ''}{_dnum(d, dp)}" if d != 0 else None
        items.append(
            {
                "key": key,
                "label": label,
                "meaning": meaning,
                "exact": v,
                "display": _dnum(v, dp) if v is not None else "—",
                "previous_display": _dnum(pv, dp) if pv is not None else None,
                "delta_display": delta,
                "change": change,
                "history": [h.get(key) for h in hist],
                "history_display": [
                    _dnum(h.get(key), dp) if h.get(key) is not None else "—" for h in hist
                ],
            }
        )
    states = [
        {
            "key": key,
            "label": label,
            "value": cur.get(key) or "UNKNOWN",
            "previous": (prev or {}).get(key),
            "changed": prev is not None and cur.get(key) != prev.get(key),
        }
        for key, label in STATES
    ]
    close = datetime.fromisoformat(cur["close_time"])
    nxt = close + timedelta(minutes=5)
    while nxt <= now:
        nxt += timedelta(minutes=5)
    rec = snaps[0].get("recorded_at")
    return {
        "available": True,
        "bar_open": cur["open_time"],
        "bar_close": cur["close_time"],
        "recorded_at": rec,
        "next_bar_close": nxt.isoformat(),
        "seconds_to_next": int((nxt - now).total_seconds()),
        "last_price": None if last_price is None else f"{last_price:,}",
        "items": items,
        "states": states,
        "regime_rule": cur.get("regime_rule"),
        "trend_age_bars": cur.get("trend_age_bars"),
        "readiness": {
            "price": cur.get("price_context_ready"),
            "liquidity": cur.get("liquidity_context_ready"),
            "liquidity_bars_missing": cur.get("liquidity_bars_missing"),
            "segment_bars": cur.get("segment_bars"),
            "warmup_target": cur.get("warmup_target"),
        },
        "candle": cur.get("candle"),
        "history_times": [h["open_time"] for h in hist],
        "note": "Finalized M5 bars only — the exact values the Context and Exhaustion gates use."
        " Values change once per M5 close; the forming bar is never used.",
    }


# ---- V5.9 P-Gap quality (human language; thresholds come from the stored record) ---------

PGAP_REASON_TEXT = {
    "PGAP_IMPULSE_WRONG_DIRECTION": "impulse candle closed against the P-Gap direction",
    "PGAP_IMPULSE_BODY_TOO_WEAK": "impulse candle is dominated by shadows",
    "PGAP_GAP_TOO_SMALL_RELATIVE_TO_BODY": "gap is too small compared with the impulse body",
    "PGAP_GAP_TOO_SMALL_IN_TICKS": "gap is smaller than the minimum number of ticks",
}
PGAP_OUTCOME_TEXT = {
    "NOT_FIRST_IN_RUN": "Valid, but not the first valid P-Gap of its run",
    "CAPACITY_BUSY": "Valid, but a setup was already active",
    "DATA_WARMUP": "Valid, but Context history was still warming up",
    "REPAIRED_HISTORY": "Valid, but on repaired history (never a retroactive setup)",
}

PGAP_OUTCOME_UNMEASURED = {
    "NOT_FIRST_IN_RUN": "Not the first P-Gap of its run",
    "CAPACITY_BUSY": "A setup was already active",
    "DATA_WARMUP": "Context history was still warming up",
    "REPAIRED_HISTORY": "On repaired history (never a retroactive setup)",
}


def _pct(v: Any) -> str | None:
    if v is None:
        return None
    return f"{(Decimal(str(v)) * 100).quantize(Decimal(1))}%"


def pgap_quality_view(q: dict[str, Any] | None, spec_version: str | None) -> dict[str, Any]:
    """P-Gap quality card. Records made before V5.9 were never measured: said so, never
    re-evaluated under the new rules."""
    if not q:
        return {
            "measured": False,
            "result": "Not measured",
            "text": f"Recorded under spec {spec_version or 'before 5.9'} — P-Gap quality was not part of the rules then",
            "rows": [],
        }
    th = q.get("thresholds") or {}
    ok_imp, ok_gap, ok = q["strong_impulse_pass"], q["strong_gap_pass"], q["final_pgap_pass"]
    reasons = list(q.get("failure_reasons") or [])
    direction = {"BULLISH": "bullish", "BEARISH": "bearish", "DOJI": "doji"}.get(
        str(q.get("impulse_direction")), "—"
    )
    ticks = q.get("gap_size_ticks")
    rows = [
        {
            "label": "Impulse candle",
            "value": ("Strong" if ok_imp else "Weak") + f" · {direction}",
            "ok": ok_imp,
        },
        {
            "label": "Body / candle",
            "value": _pct(q.get("impulse_body_ratio")) or "— (no range)",
            "required": f"≥ {_pct(th.get('body_ratio_min', '0.60'))}",
            "ok": "PGAP_IMPULSE_BODY_TOO_WEAK" not in reasons,
        },
        {
            "label": "Gap / impulse body",
            "value": _pct(q.get("gap_body_ratio")) or "—",
            "required": f"≥ {_pct(th.get('gap_body_ratio_min', '0.15'))}",
            "ok": "PGAP_GAP_TOO_SMALL_RELATIVE_TO_BODY" not in reasons,
        },
        {
            "label": "Gap size",
            "value": (f"{Decimal(str(ticks)).normalize():f} ticks" if ticks is not None else "—"),
            "required": f"≥ {th.get('gap_ticks_min', 2)} ticks",
            "ok": "PGAP_GAP_TOO_SMALL_IN_TICKS" not in reasons,
        },
    ]
    enforced = q.get("enforced", True)  # records before V5.12 were filtered by these values
    if enforced:
        result = (
            "Valid P-Gap"
            if ok
            else "Rejected — " + (PGAP_REASON_TEXT.get(reasons[0], reasons[0]) if reasons else "")
        )
    else:  # V5.12: informational only
        result = (
            "Strong P-Gap (measured)"
            if ok
            else "Measured only — "
            + (PGAP_REASON_TEXT.get(reasons[0], reasons[0]) if reasons else "")
        )
    return {
        "measured": True,
        "valid": ok,
        "enforced": enforced,
        "result": result,
        "reason_codes": reasons,
        "strong_impulse": ok_imp,
        "strong_gap": ok_gap,
        "rows": rows,
        "exact": {k: v for k, v in q.items() if k != "thresholds"},
    }


def pgap_outcome(promoted: bool, reason: str | None, measured: bool = True) -> dict[str, Any]:
    if promoted:
        return {"label": "Created a candidate", "tone": "ok", "code": None}
    if reason in PGAP_REASON_TEXT:
        return {"label": "Rejected P-Gap", "tone": "warn", "code": reason}
    label = PGAP_OUTCOME_TEXT.get(reason or "", reason or "Not promoted")
    if not measured:  # recorded before V5.9: its quality was never assessed
        label = PGAP_OUTCOME_UNMEASURED.get(reason or "", label)
    return {"label": label, "tone": "neutral", "code": reason}
