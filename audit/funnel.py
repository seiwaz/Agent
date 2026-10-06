"""Independent re-check of the two rejections that decide most setups under the current
rules: ZONE_INVALID (an execution-TF close beyond the OB's far edge while waiting for the
confirmation) and NOT_FRESH (the OB was touched before the order). For every such setup of
BTC / XRP the arming minute, the closes and the breaks are recomputed with plain loops.

Usage: uv run python -m audit.funnel -> docs/audit/funnel.json
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from audit.common import MAIN, OUT, m1, params
from audit.execution import population
from sp2l.core.types import Side
from sp2l.smc.structure import analyze
from sp2l.smc.timeframes import MINUTE, aggregate

M5 = timedelta(minutes=5)


def main() -> None:
    out: dict[str, Any] = {}
    for sym in MAIN:
        p = params(sym)
        bars = m1(sym)
        up = bars[-1].open_time + MINUTE
        x5 = aggregate(bars, p.exec_tf, up, p.htf_grid)
        a5 = analyze(x5, p.exec_tf, p)
        i5 = {b.open_time: i for i, b in enumerate(x5)}
        res = {"zone_invalid": 0, "zi_time_matches": 0, "zi_break_before": 0, "zi_examples": [],
               "not_fresh": 0, "nf_ob_touched_before_order": 0, "nf_origin_inside_ob": 0}
        for s in population(sym, "current")["setups"]:
            zs = s.zone
            long = s.direction is Side.LONG
            if s.reasons == ("ZONE_INVALID",):
                res["zone_invalid"] += 1
                # the arming minute: first minute after the setup was known that trades into the FVG
                arm = next(b for b in bars if b.open_time >= zs.confirmed_at and (b.low <= zs.gap[1] if long else b.high >= zs.gap[0]))
                k = i5[arm.open_time - (arm.open_time - x5[0].open_time) % M5]
                far = zs.ob.bottom if long else zs.ob.top
                while not (x5[k].close < far if long else x5[k].close > far):
                    k += 1
                t = x5[k].open_time + M5
                res["zi_time_matches"] += t == s.created_at
                brk = [e for e in a5.events if e.direction is s.direction and arm.open_time <= e.break_time + M5 <= t - M5]
                res["zi_break_before"] += bool(brk)
                if len(res["zi_examples"]) < 5:
                    res["zi_examples"].append({"key": s.key, "armed": arm.open_time.isoformat(), "invalid_at": t.isoformat(),
                                               "minutes": int((t - arm.open_time) / MINUTE), "breaks_between": len(brk)})
            if s.reasons == ("NOT_FRESH",):
                res["not_fresh"] += 1
                touched = any((b.low <= zs.ob.top if long else b.high >= zs.ob.bottom) for b in bars
                              if zs.confirmed_at <= b.open_time < s.created_at)
                res["nf_ob_touched_before_order"] += touched
                if s.confirm is not None:
                    res["nf_origin_inside_ob"] += zs.ob.bottom <= s.confirm.origin <= zs.ob.top
        out[sym] = res
        print(sym, res)
    (OUT / "funnel.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__" and len(__import__("sys").argv) == 1:
    main()


def not_fresh_counterfactual() -> dict[str, Any]:
    """Setups rejected NOT_FRESH whose confirmation reacted from inside the OB, re-evaluated
    with the OB treated as fresh (audit-only copy; the engine is not changed), walked with the
    reference lifecycle."""
    from dataclasses import replace

    from audit.common import EXTRA, costs
    from audit.execution import Rules, ref_walk
    from sp2l.smc.backtest import build_context
    from sp2l.smc.model import Order
    from sp2l.smc.strategy import evaluate_order

    res: dict[str, Any] = {}
    for v, over in (("current", {}), ("nofilter", {"require_discount": False, "min_net_rr": 0, "max_cost_frac": 0})):
        rows = []
        for sym in MAIN + EXTRA:
            p = params(sym, **over)
            nf = [s for s in population(sym, v)["setups"] if s.reasons == ("NOT_FRESH",)]
            if not nf:
                continue
            bars = m1(sym)
            ctx = build_context(bars, p)
            index = {b.open_time: i for i, b in enumerate(bars)}
            for s in nf:
                zs = replace(s.zone, ob=replace(s.zone.ob, tested_idx=None))
                again = evaluate_order(zs, ctx, p, costs(), Order(s.created_at, None, s.confirm))
                row = {"sym": sym, "key": s.key, "reasons": list(again.reasons)}
                if again.accepted:
                    w = ref_walk(again.direction, again.entry, again.sl, again.tp.price, again.created_at,
                                 False, bars, index[again.created_at], p, costs(), p.tick, Rules())
                    row.update(state=w.state, r=None if w.r is None else float(round(w.r, 3)))
                rows.append(row)
        acc = [r for r in rows if not r["reasons"]]
        res[v] = {"not_fresh": len(rows), "accepted_if_fresh": len(acc),
                  "closed": sum(1 for r in acc if r.get("r") is not None),
                  "total_r": round(sum(r["r"] for r in acc if r.get("r") is not None), 3), "rows": rows}
        print(v, {k: x for k, x in res[v].items() if k != "rows"})
    return res


if __name__ == "__main__" and len(__import__("sys").argv) > 1:
    out = json.loads((OUT / "funnel.json").read_text())
    out["not_fresh_counterfactual"] = not_fresh_counterfactual()
    (OUT / "funnel.json").write_text(json.dumps(out, indent=1, default=str))
