"""Exact JSON checkpoints of engine state for continuous persistence and restart recovery.

Decimals are stored as strings, datetimes as ISO-8601, enums by value, so a restore is
bit-identical. Ports and gate evaluators are not serialized; they are re-attached on
restore.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sp2l.core.types import Candle, Side
from sp2l.engine.model import (
    ExecutionPort,
    ExitEvent,
    ExitKind,
    FillEvent,
    Leg,
    OrderStatus,
    SetupState,
)
from sp2l.engine.setup_machine import (
    CancelIntent,
    Gates,
    MachineConfig,
    SetupMachine,
    _PendingCancel,
)
from sp2l.execution.shadow_broker import ShadowBroker, _Order, _Protection
from sp2l.marketdata.m1_builder import M1Result, M1Status, Quality
from sp2l.marketdata.m5_aggregator import M5Aggregator
from sp2l.strategy.context.levels import EligibleLevel, FrozenLevel
from sp2l.strategy.levels import SetupLevels
from sp2l.strategy.risk.engine import CostModel
from sp2l.strategy.spike import RunTracker, Spike, _Run

J = dict[str, Any]


def _d(x: Decimal | None) -> str | None:
    return None if x is None else str(x)


def _D(x: str | None) -> Decimal | None:  # noqa: N802
    return None if x is None else Decimal(x)


def _t(x: datetime | None) -> str | None:
    return None if x is None else x.isoformat()


def _T(x: str | None) -> datetime | None:  # noqa: N802
    return None if x is None else datetime.fromisoformat(x)


def _req(x: Any) -> Any:
    assert x is not None
    return x


# ---- value objects -------------------------------------------------------------------


def dump_candle(c: Candle) -> J:
    return {
        "t": c.open_time.isoformat(),
        "o": str(c.open),
        "h": str(c.high),
        "l": str(c.low),
        "c": str(c.close),
        "v": str(c.volume),
        "n": c.trade_count,
        "syn": c.synthetic,
    }


def load_candle(j: J) -> Candle:
    return Candle(
        datetime.fromisoformat(j["t"]),
        Decimal(j["o"]),
        Decimal(j["h"]),
        Decimal(j["l"]),
        Decimal(j["c"]),
        Decimal(j["v"]),
        None if j["n"] is None else int(j["n"]),
        bool(j["syn"]),
    )


def dump_levels(lv: SetupLevels) -> J:
    return {
        "side": lv.side.value,
        **{k: str(getattr(lv, k)) for k in ("e1", "sl", "r", "tp", "e2_mid", "e2", "d2")},
    }


def load_levels(j: J) -> SetupLevels:
    return SetupLevels(
        Side(j["side"]), *(Decimal(j[k]) for k in ("e1", "sl", "r", "tp", "e2_mid", "e2", "d2"))
    )


def dump_spike(s: Spike) -> J:
    return {
        "side": s.side.value,
        "candles": [dump_candle(c) for c in s.candles],
        "extendable": s.extendable,
    }


def load_spike(j: J) -> Spike:
    return Spike(Side(j["side"]), [load_candle(c) for c in j["candles"]], bool(j["extendable"]))


def dump_m1(m: M1Result) -> J:
    return {
        "t": m.open_time.isoformat(),
        "status": m.status.value,
        "candle": dump_candle(m.candle) if m.candle else None,
        "quality": m.quality.value if m.quality else None,
    }


def load_m1(j: J) -> M1Result:
    return M1Result(
        datetime.fromisoformat(j["t"]),
        M1Status(j["status"]),
        load_candle(j["candle"]) if j["candle"] else None,
        Quality(j["quality"]) if j.get("quality") else None,
    )


def _dump_fill(f: FillEvent) -> J:
    return {
        "cid": f.client_order_id,
        "ts": f.ts.isoformat(),
        "price": str(f.price),
        "qty": str(f.qty),
        "fee": str(f.fee),
        "cum": str(f.cumulative_qty),
        "oq": str(f.order_qty),
    }


def _load_fill(j: J) -> FillEvent:
    return FillEvent(
        j["cid"],
        datetime.fromisoformat(j["ts"]),
        Decimal(j["price"]),
        Decimal(j["qty"]),
        Decimal(j["fee"]),
        Decimal(j["cum"]),
        Decimal(j["oq"]),
    )


def _dump_exit(e: ExitEvent) -> J:
    return {
        "kind": e.kind.value,
        "ts": e.ts.isoformat(),
        "price": str(e.price),
        "qty": str(e.qty),
        "fee": str(e.fee),
        "pnl": str(e.pnl),
    }


def _load_exit(j: J) -> ExitEvent:
    return ExitEvent(
        ExitKind(j["kind"]),
        datetime.fromisoformat(j["ts"]),
        Decimal(j["price"]),
        Decimal(j["qty"]),
        Decimal(j["fee"]),
        Decimal(j["pnl"]),
    )


# ---- setup machine ---------------------------------------------------------------------


def dump_machine(m: SetupMachine) -> J:
    p = m.pending
    return {
        "cfg": {
            "setup_id": m.cfg.setup_id,
            "tick": str(m.cfg.tick),
            "fill_window_candles": m.cfg.fill_window_candles,
            "protection_attempts": m.cfg.protection_attempts,
            "cancel_wait_closes": m.cfg.cancel_wait_closes,
        },
        "candidate_id": m.candidate_id,
        "spike": dump_spike(m.spike),
        "state": m.state.value,
        "levels": dump_levels(m.levels),
        "qty": _d(m.qty),
        "revision": m.revision,
        "ever_armed": m.ever_armed,
        "e1_id": m.e1_id,
        "e1_filled": str(m.e1_filled),
        "e1_full": m.e1_full,
        "e2_id": m.e2_id,
        "e2_filled": str(m.e2_filled),
        "e2_enabled": m.e2_enabled,
        "pb_minute": _t(m.pb_minute),
        "pending": None
        if p is None
        else {
            "intent": p.intent.value,
            "reject_state": p.reject_state.value if p.reject_state else None,
            "reasons": p.reasons,
            "new_levels": dump_levels(p.new_levels) if p.new_levels else None,
            "new_qty": _d(p.new_qty),
            "waited": p.waited,
        },
        "primary_reason": m.primary_reason,
        "reasons": m.reasons,
        "frozen": None
        if m.frozen is None
        else {
            "side": m.frozen.side.value,
            "price": str(m.frozen.price),
            "t": m.frozen.pivot_open_time.isoformat(),
        },
        "eligible": None
        if m.eligible_levels is None
        else [
            {"price": str(e.price), "t": e.pivot_open_time.isoformat()} for e in m.eligible_levels
        ],
        "breakout_locked": m.breakout_locked,
        "eval_count": m.eval_count,
        "created_at": _t(m.created_at),
        "transitions": [
            [t.isoformat(), f.value if f else None, s.value] for t, f, s in m.transitions
        ],
        "submissions": [
            [c, leg.value, rev, str(pr), str(q), t.isoformat()]
            for c, leg, rev, pr, q, t in m.submissions
        ],
        "fills": [_dump_fill(f) for f in m.fills],
        "exits": [_dump_exit(e) for e in m.exits],
    }


def load_machine(j: J, port: ExecutionPort, gates: Gates) -> SetupMachine:
    c = j["cfg"]
    cfg = MachineConfig(
        c["setup_id"],
        Decimal(c["tick"]),
        c["fill_window_candles"],
        c["protection_attempts"],
        c["cancel_wait_closes"],
    )
    fz = j["frozen"]
    frozen = (
        None
        if fz is None
        else FrozenLevel(Side(fz["side"]), Decimal(fz["price"]), datetime.fromisoformat(fz["t"]))
    )
    m = SetupMachine(cfg, port, gates, load_spike(j["spike"]), frozen)
    m.candidate_id = j["candidate_id"]
    m.state = SetupState(j["state"])
    m.levels = load_levels(j["levels"])
    m.qty = _D(j["qty"])
    m.revision = int(j["revision"])
    m.ever_armed = bool(j["ever_armed"])
    m.e1_id, m.e1_filled, m.e1_full = j["e1_id"], Decimal(j["e1_filled"]), bool(j["e1_full"])
    m.e2_id, m.e2_filled, m.e2_enabled = j["e2_id"], Decimal(j["e2_filled"]), bool(j["e2_enabled"])
    m.pb_minute = _T(j["pb_minute"])
    p = j["pending"]
    m.pending = (
        None
        if p is None
        else _PendingCancel(
            CancelIntent(p["intent"]),
            SetupState(p["reject_state"]) if p["reject_state"] else None,
            list(p["reasons"]),
            load_levels(p["new_levels"]) if p["new_levels"] else None,
            _D(p["new_qty"]),
            int(p["waited"]),
        )
    )
    m.primary_reason = j["primary_reason"]
    m.reasons = list(j["reasons"])
    m.eligible_levels = (
        None
        if j["eligible"] is None
        else tuple(
            EligibleLevel(Decimal(e["price"]), datetime.fromisoformat(e["t"]))
            for e in j["eligible"]
        )
    )
    m.breakout_locked = bool(j["breakout_locked"])
    m.eval_count = int(j["eval_count"])
    m.created_at = _T(j["created_at"])
    m.transitions = [
        (datetime.fromisoformat(t), SetupState(f) if f else None, SetupState(s))
        for t, f, s in j["transitions"]
    ]
    m.submissions = [
        (c, Leg(leg), int(rev), Decimal(pr), Decimal(q), datetime.fromisoformat(t))
        for c, leg, rev, pr, q, t in j["submissions"]
    ]
    m.fills = [_load_fill(f) for f in j["fills"]]
    m.exits = [_load_exit(e) for e in j["exits"]]
    return m


# ---- shadow broker ---------------------------------------------------------------------


def dump_broker(b: ShadowBroker) -> J:
    return {
        "orders": [
            {
                "cid": o.client_order_id,
                "side": o.side.value,
                "price": str(o.price),
                "qty": str(o.qty),
                "executed": str(o.executed),
                "status": o.status.value,
                "after": o.submitted_after_trade,
            }
            for o in b._orders.values()
        ],
        "position": str(b._position),
        "entry_cost": str(b._entry_cost),
        "protection": None
        if b._protection is None
        else {
            "side": b._protection.side.value,
            "sl": str(b._protection.sl),
            "tp": str(b._protection.tp),
        },
        "protection_fails": b._protection_fails,
        "last_price": _d(b.last_price),
        "realized": str(b.realized_pnl),
        "fees": str(b.fees),
        "trade_seq": b.trade_seq,
        "fill_audit": [list(x) for x in b.fill_audit],
    }


def load_broker(j: J, costs: CostModel) -> ShadowBroker:
    b = ShadowBroker(costs, protection_fails=int(j["protection_fails"]))
    for o in j["orders"]:
        b._orders[o["cid"]] = _Order(
            o["cid"],
            Side(o["side"]),
            Decimal(o["price"]),
            Decimal(o["qty"]),
            Decimal(o["executed"]),
            OrderStatus(o["status"]),
            int(o["after"]),
        )
    b._position, b._entry_cost = Decimal(j["position"]), Decimal(j["entry_cost"])
    pr = j["protection"]
    b._protection = (
        None if pr is None else _Protection(Side(pr["side"]), Decimal(pr["sl"]), Decimal(pr["tp"]))
    )
    b.last_price = _D(j["last_price"])
    b.realized_pnl, b.fees = Decimal(j["realized"]), Decimal(j["fees"])
    b.trade_seq = int(j["trade_seq"])
    b.fill_audit = [(str(a), int(t), int(s)) for a, t, s in j["fill_audit"]]
    return b


# ---- market-structure trackers ---------------------------------------------------------


def dump_aggregator(a: M5Aggregator) -> J:
    return {"bucket": _t(a._bucket), "minutes": [dump_m1(m) for m in a._minutes]}


def load_aggregator(j: J) -> M5Aggregator:
    a = M5Aggregator()
    a._bucket = _T(j["bucket"])
    a._minutes = [load_m1(m) for m in j["minutes"]]
    return a


def dump_runs(r: RunTracker) -> J:
    return {
        "runs": {
            s.value: {"candles": [dump_candle(c) for c in run.candles], "spent": run.spent}
            for s, run in r._runs.items()
        },
        "last": dump_candle(r._last) if r._last else None,
    }


def load_runs(j: J, tick: Decimal) -> RunTracker:
    r = RunTracker(tick)
    r._runs = {
        Side(s): _Run([load_candle(c) for c in v["candles"]], bool(v["spent"]))
        for s, v in j["runs"].items()
    }
    r._last = load_candle(j["last"]) if j["last"] else None
    return r
