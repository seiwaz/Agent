"""Backtest of the SMC model on a stored 1-minute series (same code path as the runner).

Every timeframe is aggregated from the M1 series and analysed in one causal pass; each M1
trigger is evaluated as of its close and accepted signals are walked through the following
M1 bars with the live lifecycle rules (capacity `max_active` applies in time order).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sp2l.core.types import Candle
from sp2l.smc.context import needed_tfs
from sp2l.smc.lifecycle import Tracked, advance, risk_unit
from sp2l.smc.model import Analysis, Costs, Setup, SmcParams
from sp2l.smc.strategy import find_setups
from sp2l.smc.structure import analyze
from sp2l.smc.timeframes import MINUTE, aggregate


def build_context(m1: Sequence[Candle], p: SmcParams) -> dict[str, Analysis]:
    upto = m1[-1].open_time + MINUTE
    return {tf: analyze(aggregate(m1, tf, upto), tf, p) for tf in needed_tfs(p)}


def run(
    m1: Sequence[Candle],
    p: SmcParams,
    costs: Costs,
    ctx: dict[str, Analysis] | None = None,
) -> dict[str, Any]:
    """`ctx` may be passed in to reuse one analysis for many parameter sets that share the
    structure parameters (only the timeframes the parameters name are read from it)."""
    ctx = build_context(m1, p) if ctx is None else {tf: ctx[tf] for tf in needed_tfs(p)}
    setups = find_setups(ctx, p, costs)
    index = {c.open_time: i for i, c in enumerate(m1)}
    trades: list[tuple[Setup, Tracked]] = []
    busy_until: list[datetime] = []
    used: set[str] = set()  # a POI is traded once
    for s in sorted((s for s in setups if s.accepted), key=lambda s: s.created_at):
        busy_until = [b for b in busy_until if b > s.created_at]
        if len(busy_until) >= p.max_active or s.poi is None or s.poi.id in used:
            continue
        used.add(s.poi.id)
        assert s.entry is not None and s.sl is not None and s.tp is not None
        mkt = p.entry_mode == "market"
        t = Tracked(
            s.key,
            s.direction,
            s.entry,
            s.sl,
            s.tp,
            s.created_at,
            risk_unit(s.direction, s.entry, s.sl, costs, market=mkt),
            market=mkt,
        )
        i = index.get(s.created_at)
        while i is not None and i < len(m1) and t.active:
            advance(t, m1[i], p, costs)
            i += 1
        busy_until.append(t.closed_at or m1[-1].open_time + MINUTE)
        trades.append((s, t))
    return {"context": ctx, "setups": setups, "trades": trades, "stats": stats(trades, setups)}


def stats(trades: Sequence[tuple[Setup, Tracked]], setups: Sequence[Setup]) -> dict[str, Any]:
    closed = [t for _, t in trades if t.result_r is not None]
    rs = [t.result_r for t in closed if t.result_r is not None]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    eq, peak, dd = Decimal(0), Decimal(0), Decimal(0)
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    reasons: Counter[str] = Counter()
    for st in setups:
        for code in st.reasons:
            reasons[code] += 1
    return {
        "triggers": len(setups),
        "accepted": sum(1 for s in setups if s.accepted),
        "signals": len(trades),
        "states": dict(Counter(t.state.value for _, t in trades)),
        "closed": len(rs),
        "wins": len(wins),
        "win_rate": None if not rs else round(len(wins) / len(rs), 3),
        "total_r": str(round(sum(rs, start=Decimal(0)), 2)),
        "avg_r": None if not rs else str(round(sum(rs, start=Decimal(0)) / len(rs), 3)),
        "profit_factor": None
        if not losses or sum(losses) == 0
        else str(round(sum(wins, start=Decimal(0)) / -sum(losses, start=Decimal(0)), 2)),
        "max_drawdown_r": str(round(dd, 2)),
        "rejections": dict(reasons.most_common()),
    }
