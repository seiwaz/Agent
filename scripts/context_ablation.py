"""Context V5 vs V6.0 ablation over a recorded market-event journal (READ-ONLY).

Replays the journal (market_events: TRADE / M1 / GAP, exactly as the Shadow runner applies
them) through the CURRENT engine (Context V6.0, Exhaustion advisory, 30-bar warmup) entirely in
memory: a NullRecorder, no probes, nothing is written to any database. The source database is
read inside a READ ONLY transaction.

For every Spike candidate the replay creates, the FIRST Context evaluation (the decision at
creation) is judged twice on the very same snapshot:
- V6.0: the engine's own verdict (NetTP AND (LevelBreak OR ChannelEdge OR HTFAligned)).
- V5:   rebuilt from the V5 measures V6.0 still records (warmup 150, data validity,
        RangeMiddle, HTF-opposite-without-breakout, require-one with the V5 regime conditions,
        RoomToTP >= 1R, Liquidity), plus the V5 Exhaustion gate (= the advisory verdict).
Paired by construction. Caveat: the candidate set is the V6 replay's; under V5 the capacity
(one setup at a time) could have produced a slightly different set of candidates.

    uv run python scripts/context_ablation.py --config config/runtime.yaml --symbol BTCUSDT
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from sp2l.config import RuntimeConfig
from sp2l.engine.checkpoint import load_m1
from sp2l.engine.recording import NullRecorder
from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.indicators.regime import Regime
from sp2l.indicators.trend import Trend
from sp2l.strategy.context.engine import ContextSnapshot
from sp2l.strategy.risk.engine import ExchangeFilters

V5_WARMUP = 150


class _SegLen:
    """Delegates to the engine's M5Gates and remembers the segment length at each call."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.seg_len: dict[tuple[str, datetime], int] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def evaluate(self, **kw: Any) -> Any:
        seg = self._inner.m5.segment
        self.seg_len.setdefault((str(kw["side"]), kw["eval_time"]), len(seg.bars) if seg else 0)
        return self._inner.evaluate(**kw)


def v5_reasons(c: ContextSnapshot, side: str, seg_len: int) -> list[str]:
    """The V5 (rev 5.12) Context verdict rebuilt from the recorded measures."""
    if c.ctx_open_time is None or seg_len < V5_WARMUP:
        return ["CONTEXT_UNKNOWN_WARMUP"]
    if (
        c.regime in (None, Regime.UNKNOWN)
        or c.trend is Trend.INVALID_DUAL_PIVOT
        or c.range_high is None
        or c.range_low is None
        or c.range_high == c.range_low
    ):
        return ["CONTEXT_INVALID_DATA"]
    out = []
    if c.range_middle_reject:
        out.append("RANGE_MIDDLE")
    if c.htf_opposite_no_breakout:
        out.append("HTF_OPPOSITE_NO_BREAKOUT")
    aligned = Trend.BULL if side == "LONG" else Trend.BEAR
    htf_v5 = c.trend is aligned and c.regime is not Regime.RANGE
    edge_v5 = c.regime is Regime.RANGE and bool(c.range_edge_origin)
    if not (c.breakout_context or htf_v5 or edge_v5):
        out.append("NO_VALID_CONTEXT")
    if c.room_pass is False:
        out.append("ROOM_TO_TP_INSUFFICIENT")
    if c.liquidity_status != "PASS":
        out.append(str(c.liquidity_status))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/runtime.yaml")
    ap.add_argument("--source-url", default=None, help="journal DB (read-only); default config")
    ap.add_argument("--symbol", default=None, help="journal symbol; default config symbol")
    ap.add_argument("--tick", default="0.1")
    ap.add_argument("--out", default=None, help="optional JSON report path")
    a = ap.parse_args()
    cfg = RuntimeConfig.load(Path(a.config))
    symbol = a.symbol or cfg.symbol
    db = create_engine(a.source_url or cfg.database_url)
    with db.connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        events = c.execute(
            text(
                "SELECT seq, kind, ts, payload FROM market_events WHERE symbol = :s"
                " AND kind IN ('TRADE', 'M1', 'GAP') ORDER BY seq"
            ),
            {"s": symbol},
        ).all()
    if not events:
        print(json.dumps({"symbol": symbol, "error": "no journal events"}))
        return
    tick = Decimal(a.tick)
    eng = ShadowSymbolEngine(
        symbol,
        tick=tick,
        costs=cfg.shadow_costs(),
        filters=ExchangeFilters(tick, Decimal("0.00001"), None, None, verified=False),
        warmup_bars=int(cfg.section("shadow").get("warmup_m5_bars", 30)),
        recorder=NullRecorder(),
        probes=False,
    )
    wrap = _SegLen(eng.gates)
    eng.gates = wrap
    for _seq, kind, ts, p in events:
        if kind == "TRADE":
            eng.feed_trade(
                Decimal(p["price"]), datetime.fromisoformat(p["exch_ts"]), p.get("source", "WS")
            )
        elif kind == "M1":
            eng.on_m1(load_m1(p))
        elif kind == "GAP":
            eng.on_gap(datetime.fromisoformat(p["start"]), None, ts, p["reason"])

    machines = list(eng.finished) + ([eng.active] if eng.active else [])
    rows = []
    for m in machines:
        if not m.evaluations:
            continue
        _, et, res = m.evaluations[0]
        ctx, exh = res.context, res.exhaustion
        if ctx is None:
            continue
        side = str(m.side)
        old = v5_reasons(ctx, side, wrap.seg_len.get((side, et), 0))
        subs = list(exh.sub_reasons) if exh else []
        rows.append(
            {
                "setup": m.cfg.setup_id,
                "side": side,
                "at": et.isoformat(),
                "v6_status": ctx.status,
                "v6_reasons": list(ctx.reasons),
                "v5_reasons": old,
                "v5_exhaustion_rejects": "ADVISORY_WOULD_REJECT" in subs
                or "ADVISORY_UNKNOWN" in subs,
                "net_tp_per_unit": None
                if ctx.net_tp_per_unit is None
                else str(ctx.net_tp_per_unit),
                "net_tp_positive": ctx.net_tp_positive,
                "conditions": {
                    "level_break": ctx.level_break,
                    "channel_edge": ctx.channel_edge,
                    "htf_aligned": ctx.htf_aligned,
                },
                "final_state": str(m.state),
                "r_bps": float(m.levels.r / m.levels.e1 * 10000),
            }
        )
    v6_rej = [r for r in rows if r["v6_status"] != "PASS"]
    v5_rej = [r for r in rows if r["v5_reasons"]]
    v5_rej_exh = [r for r in rows if r["v5_reasons"] or r["v5_exhaustion_rejects"]]
    newly = [
        r
        for r in rows
        if r["v6_status"] == "PASS" and (r["v5_reasons"] or r["v5_exhaustion_rejects"])
    ]
    lost = [
        r
        for r in rows
        if r["v6_status"] != "PASS" and not (r["v5_reasons"] or r["v5_exhaustion_rejects"])
    ]
    report = {
        "source": "market_events (read-only)",
        "symbol": symbol,
        "journal_events": len(events),
        "journal_from": events[0][2].isoformat(),
        "journal_to": events[-1][2].isoformat(),
        "candidates": len(rows),
        "v5_context_rejected": len(v5_rej),
        "v5_context_or_exhaustion_rejected": len(v5_rej_exh),
        "v6_context_rejected": len(v6_rej),
        "v5_primary_reason": dict(Counter(r["v5_reasons"][0] for r in v5_rej)),
        "v5_all_reasons": dict(Counter(x for r in v5_rej for x in r["v5_reasons"])),
        "v6_primary_reason": dict(Counter(r["v6_reasons"][0] for r in v6_rej)),
        "v6_all_reasons": dict(Counter(x for r in v6_rej for x in r["v6_reasons"])),
        "newly_passing": len(newly),
        "newly_passing_with_net_tp_positive": sum(1 for r in newly if r["net_tp_positive"]),
        "newly_rejected_by_v6": len(lost),
        "newly_rejected_reasons": dict(Counter(r["v6_reasons"][0] for r in lost)),
        "exhaustion_would_reject_all": sum(1 for r in rows if r["v5_exhaustion_rejects"]),
        "exhaustion_would_reject_among_v6_pass": sum(
            1 for r in rows if r["v6_status"] == "PASS" and r["v5_exhaustion_rejects"]
        ),
        "v6_conditions_among_pass": {
            k: sum(1 for r in rows if r["v6_status"] == "PASS" and r["conditions"][k])
            for k in ("level_break", "channel_edge", "htf_aligned")
        },
        "v6_any_condition": sum(1 for r in rows if any(r["conditions"].values())),
        "v6_any_condition_but_net_tp_not_positive": sum(
            1 for r in rows if any(r["conditions"].values()) and r["net_tp_positive"] is False
        ),
        "r_bps_median": sorted(r["r_bps"] for r in rows)[len(rows) // 2] if rows else None,
        "r_bps_max": max((r["r_bps"] for r in rows), default=None),
        "v6_replay_final_states": dict(Counter(r["final_state"] for r in rows)),
        "caveat": "paired on the V6 replay's candidates; V5 capacity could differ slightly",
    }
    print(json.dumps(report, indent=2, default=str))
    if a.out:
        Path(a.out).write_text(json.dumps({"report": report, "rows": rows}, indent=2, default=str))


if __name__ == "__main__":
    main()
