"""Persistence of SMC signals and their lifecycle trail."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, text

from sp2l.core.types import Side
from sp2l.smc.lifecycle import ACTIVE_SQL, Part, State, Tracked
from sp2l.smc.model import VERSION, Setup, Target, Zone, ZoneSetup
from sp2l.smc.timeframes import length
from sp2l.smc.wallet import Size


def zone_json(z: Zone | None, tf: str | None = None) -> dict[str, Any] | None:
    if z is None:
        return None
    return {
        "id": z.id,
        "tf": tf or z.id.split(":", 1)[0],
        "kind": z.kind,
        "direction": z.direction.value,
        "top": str(z.top),
        "bottom": str(z.bottom),
        "time": z.time.isoformat(),
    }


def setup_json(s: ZoneSetup) -> dict[str, Any]:
    return {
        "key": s.key,
        "tf": s.tf,
        "direction": s.direction.value,
        "ob": zone_json(s.ob, s.tf),
        "fvg": {
            "bottom": str(s.gap[0]),
            "top": str(s.gap[1]),
            # the gap's middle bar (the bar after the block when adjacent)
            "time": (
                s.ob.time + length(s.tf) * ((s.ob.gap_idx or s.ob.idx + 1) - s.ob.idx)
            ).isoformat(),
        },
        "sweep": {
            "level": str(s.sweep.level),
            "wick": str(s.sweep.wick),
            "time": s.sweep.time.isoformat(),
        },
        "event": {
            "id": s.event.id,
            "kind": s.event.kind,
            "level": str(s.event.level),
            "from": s.event.level_time.isoformat(),
            "to": s.event.break_time.isoformat(),
        },
        "confirmed_at": s.confirmed_at.isoformat(),
    }


def target_json(x: Target | None) -> dict[str, Any] | None:
    if x is None:
        return None
    return {
        "price": str(x.price),
        "source": x.source,
        "net_r": str(round(x.net_r, 4)),
    }


def setup_detail(s: Setup) -> dict[str, Any]:
    return {"setup": setup_json(s.zone), "bias": s.bias}


def _parts_json(t: Tracked) -> str:
    return json.dumps(
        [
            {
                "kind": x.kind,
                "price": str(x.price),
                "frac": str(x.frac),
                "at": x.at.isoformat(),
                "r": str(x.r),
            }
            for x in t.parts
        ]
    )


def insert_signal(
    c: Connection,
    symbol: str,
    s: Setup,
    t: Tracked,
    params_hash: str,
    size: Size | None = None,
    wallet_id: int | None = None,
) -> int:
    targets = {"tp1": target_json(s.tp1), "tp2": target_json(s.tp2), "tp3": target_json(s.tp3)}
    tp2 = s.tp2
    gross = None if tp2 is None or s.entry is None else abs(tp2.price - s.entry) / t.risk
    sid: int | None = c.execute(
        text(
            "INSERT INTO smc_signals (symbol, key, side, state, created_at, entry, sl, tp, risk,"
            " rr, net_rr, score, tp_source, trigger_kind, poi_tf, detail, qty, notional,"
            " leverage, params_hash, wallet_id, margin, filled_at, tp1, tp2, tp3, targets,"
            " qty_open, realized_r, version) VALUES (:sym, :key, :side, :state, :ca, :e, :sl,"
            " :tp, :risk, :rr, :nrr, 0, :src, :tk, :ptf, CAST(:d AS jsonb), :q, :n, :lev, :ph,"
            " :w, :m, :fa, :tp1, :tp2, :tp3, CAST(:tg AS jsonb), :q, 0, :ver)"
            " ON CONFLICT (symbol, key) DO NOTHING RETURNING id"
        ),
        {
            "sym": symbol,
            "key": s.key,
            "side": s.direction.value,
            "state": t.state.value,
            "ca": s.created_at,
            "e": t.entry,
            "sl": t.sl,
            "tp": t.tp,
            "risk": t.risk,
            "rr": gross,
            "nrr": s.net_rr,
            "src": None if s.tp3 is None else s.tp3.source,
            "tk": s.zone.event.kind,
            "ptf": s.zone.tf,
            "d": json.dumps(
                {
                    **setup_detail(s),
                    "market": t.market,
                    "sl_initial": str(t.sl),
                    "frac1": str(t.frac1),
                    "frac2": str(t.frac2),
                    "time_stop_min": t.time_stop_min,
                }
            ),
            "q": size.qty if size else s.qty,
            "n": size.notional if size else s.notional,
            "lev": size.leverage if size else s.leverage,
            "ph": params_hash,
            "w": wallet_id,
            "m": size.margin if size else None,
            "fa": t.filled_at,  # a market entry is filled at creation
            "tp1": t.tp1,
            "tp2": t.tp2,
            "tp3": t.tp,
            "tg": json.dumps(targets),
            "ver": VERSION,
        },
    ).scalar()
    if sid is not None:
        detail: dict[str, Any] = {"sl": str(t.sl), **targets}
        if size is not None:
            detail.update(qty=str(size.qty), margin=str(size.margin))
            if size.note:
                detail["note"] = size.note
        event(c, sid, s.created_at, "CREATED", t.entry, detail)
    return int(sid or 0)


def event(
    c: Connection, sid: int, ts: datetime, kind: str, price: Decimal | None, detail: Any = None
) -> None:
    c.execute(
        text(
            "INSERT INTO smc_signal_events (signal_id, ts, kind, price, detail)"
            " VALUES (:i, :t, :k, :p, CAST(:d AS jsonb))"
        ),
        {"i": sid, "t": ts, "k": kind, "p": price, "d": json.dumps(detail) if detail else None},
    )


def active(db: Engine, symbol: str) -> list[tuple[int, Tracked, Decimal]]:
    """Active signals of a market with their quantity."""
    with db.connect() as c:
        rows = c.execute(
            text(
                "SELECT id, key, side, entry, sl, tp, created_at, risk, state, filled_at, last_m1,"
                " COALESCE(qty, 0), detail, tp1, tp2, parts"
                " FROM smc_signals WHERE symbol = :s AND state IN "
                + ACTIVE_SQL
                + " ORDER BY created_at"
            ),
            {"s": symbol},
        ).all()
    out = []
    for r in rows:
        d = r[12] or {}
        t = Tracked(
            key=r[1],
            side=Side(r[2]),
            entry=r[3],
            sl=r[4],
            tp=r[5],
            created_at=r[6],
            risk=r[7],
            state=State(r[8]),
            # rows written before filled_at was stored: an OPEN market entry filled at creation
            filled_at=r[9] or (r[6] if r[8] == "OPEN" else None),
            last_m1=r[10],
            market=bool(d.get("market")),
            sl0=Decimal(d.get("sl_initial") or r[4]),
            tp1=r[13],
            tp2=r[14],
            frac1=Decimal(d.get("frac1") or 0),
            frac2=Decimal(d.get("frac2") or 0),
            time_stop_min=int(d.get("time_stop_min") or 0),
            parts=[
                Part(
                    x["kind"],
                    Decimal(x["price"]),
                    Decimal(x["frac"]),
                    datetime.fromisoformat(x["at"]),
                    Decimal(x["r"]),
                )
                for x in (r[15] or [])
            ],
        )
        out.append((int(r[0]), t, Decimal(r[11])))
    return out


def save(c: Connection, sid: int, t: Tracked) -> None:
    c.execute(
        text(
            "UPDATE smc_signals SET state = :st, filled_at = :f, closed_at = :cl, sl = :sl,"
            " exit_price = :x, result_r = :r, last_m1 = :m, parts = CAST(:pt AS jsonb),"
            " qty_open = qty * :of, realized_r = :rr, updated_at = now() WHERE id = :i"
        ),
        {
            "st": t.state.value,
            "sl": t.sl,
            "f": t.filled_at,
            "cl": t.closed_at,
            "x": t.exit_price,
            "r": t.result_r,
            "m": t.last_m1,
            "pt": _parts_json(t),
            "of": t.open_frac if t.active else Decimal(0),
            "rr": t.realized_r,
            "i": sid,
        },
    )
