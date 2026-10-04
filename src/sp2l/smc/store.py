"""Persistence of SMC signals and their lifecycle trail."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, text

from sp2l.core.types import Side
from sp2l.smc.lifecycle import State, Tracked
from sp2l.smc.model import Setup, Zone
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


def setup_detail(s: Setup) -> dict[str, Any]:
    return {
        "poi": zone_json(s.poi, s.poi_tf),
        "entry_zone": zone_json(s.entry_zone, "1m"),
        "factors": s.factors,
        "bias": s.bias,
        "trigger_event_id": s.trigger_event_id,
    }


def insert_signal(
    c: Connection,
    symbol: str,
    s: Setup,
    t: Tracked,
    params_hash: str,
    size: Size | None = None,
    wallet_id: int | None = None,
) -> int:
    sid: int | None = c.execute(
        text(
            "INSERT INTO smc_signals (symbol, key, side, state, created_at, entry, sl, tp, risk,"
            " rr, net_rr, score, tp_source, trigger_kind, poi_tf, detail, qty, notional,"
            " leverage, params_hash, wallet_id, margin, filled_at) VALUES (:sym, :key, :side,"
            " :state, :ca, :e, :sl, :tp, :risk, :rr, :nrr, :score, :src, :tk, :ptf,"
            " CAST(:d AS jsonb), :q, :n, :lev, :ph, :w, :m, :fa)"
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
            "rr": s.rr,
            "nrr": s.net_rr,
            "score": s.score,
            "src": s.tp_source,
            "tk": s.trigger_kind,
            "ptf": s.poi_tf,
            "d": json.dumps({**setup_detail(s), "market": t.market, "sl_initial": str(t.sl)}),
            "q": size.qty if size else s.qty,
            "n": size.notional if size else s.notional,
            "lev": size.leverage if size else s.leverage,
            "ph": params_hash,
            "w": wallet_id,
            "m": size.margin if size else None,
            "fa": t.filled_at,  # a market entry is filled at creation
        },
    ).scalar()
    if sid is not None:
        detail = {"sl": str(t.sl), "tp": str(t.tp)}
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
    """PENDING / OPEN signals of a market with their quantity."""
    with db.connect() as c:
        rows = c.execute(
            text(
                "SELECT id, key, side, entry, sl, tp, created_at, risk, state, filled_at, last_m1,"
                " COALESCE(qty, 0), detail"
                " FROM smc_signals WHERE symbol = :s AND state IN ('PENDING', 'OPEN')"
                " ORDER BY created_at"
            ),
            {"s": symbol},
        ).all()
    return [
        (
            int(r[0]),
            Tracked(
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
                market=bool((r[12] or {}).get("market")),
                sl0=Decimal((r[12] or {}).get("sl_initial") or r[4]),
            ),
            Decimal(r[11]),
        )
        for r in rows
    ]


def save(c: Connection, sid: int, t: Tracked) -> None:
    c.execute(
        text(
            "UPDATE smc_signals SET state = :st, filled_at = :f, closed_at = :cl, sl = :sl,"
            " exit_price = :x, result_r = :r, last_m1 = :m, updated_at = now() WHERE id = :i"
        ),
        {
            "st": t.state.value,
            "sl": t.sl,
            "f": t.filled_at,
            "cl": t.closed_at,
            "x": t.exit_price,
            "r": t.result_r,
            "m": t.last_m1,
            "i": sid,
        },
    )
