"""V5.9 P-Gap qualification: replay of stored canonical M1 UNDER THE NEW SPEC (read-only).

Labelled explicitly as a replay: nothing is written, recorded Shadow outcomes and their
spec_version are never touched. Reports, on the same dataset:
- every geometric P-Gap inside a run (B04) and how the V5.9 rules classify it;
- first-P-Gap-per-run candidates before (any geometric P-Gap) and after (first QUALIFYING);
- a full engine replay (Context/Exhaustion/Risk unchanged) counting created candidates under
  the old rule vs the new one;
- real examples of each class (valid / weak body / weak gap / wrong direction, Long and Short).

    uv run python scripts/pgap_quality_report.py --config config/server.yaml \
        --since 2026-09-27T12:00:00+00:00
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from sqlalchemy import create_engine, text

from sp2l.config import RuntimeConfig
from sp2l.core.types import Candle
from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.marketdata.m1_builder import M1Result, M1Status, Quality
from sp2l.marketdata.tabdeal_rest import provisional_filters
from sp2l.strategy import spike as spike_mod
from sp2l.strategy.pgap import assess_pgap
from sp2l.strategy.spike import RunTracker


def load(db: Any, symbol: str, since: datetime) -> list[M1Result]:
    with db.connect() as c:
        rows = c.execute(
            text(
                "SELECT open_time, open, high, low, close, volume, trade_count,"
                " synthetic_no_trade, quality FROM candles_1m WHERE symbol = :s"
                " AND open_time >= :a ORDER BY open_time"
            ),
            {"s": symbol, "a": since},
        ).all()
    out = []
    for r in rows:
        t = r[0].astimezone(UTC)
        c = Candle(t, r[1], r[2], r[3], r[4], r[5], r[6], synthetic=bool(r[7]))
        st = M1Status.SYNTHETIC_NO_TRADE if r[7] else M1Status.OK
        out.append(M1Result(t, st, c, Quality(r[8])))
    return out


def with_holes(m1s: list[M1Result]) -> list[M1Result]:
    """Missing minutes become DATA_GAP results (as the engine sees them live)."""
    from datetime import timedelta

    out: list[M1Result] = []
    for m in m1s:
        while out and m.open_time - out[-1].open_time > timedelta(minutes=1):
            out.append(M1Result(out[-1].open_time + timedelta(minutes=1), M1Status.DATA_GAP, None))
        out.append(m)
    return out


def cls(q: Any) -> str:
    if q.final_pgap_pass:
        return "VALID"
    if "PGAP_IMPULSE_WRONG_DIRECTION" in q.reasons:
        return "WRONG_DIRECTION"
    if not q.strong_impulse_pass:
        return "WEAK_BODY"
    return "WEAK_GAP"


def candle_json(c: Candle) -> dict[str, str]:
    return {
        "t": c.open_time.isoformat(),
        "o": str(c.open),
        "h": str(c.high),
        "l": str(c.low),
        "c": str(c.close),
    }


def engine_candidates(m1s: list[M1Result], cfg: RuntimeConfig, old_rule: bool) -> dict[str, Any]:
    filters = provisional_filters(cfg.symbol)
    eng = ShadowSymbolEngine(
        cfg.symbol, tick=filters.tick, costs=cfg.shadow_costs(), filters=filters
    )
    ctx = (
        mock.patch.object(
            spike_mod,
            "assess_pgap",
            lambda *a: replace(assess_pgap(*a), strong_impulse_pass=True, strong_gap_pass=True),
        )
        if old_rule
        else mock.patch.object(spike_mod, "assess_pgap", assess_pgap)
    )
    with ctx:
        for m in m1s:
            eng.on_m1(m)
    reasons = Counter(p.reason or "PROMOTED" for p in eng.pgaps)
    return {
        "candidates_created": sum(1 for p in eng.pgaps if p.promoted),
        "pgap_outcomes": dict(reasons),
        "finished_states": dict(Counter(str(m.state) for m in eng.finished)),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/runtime.yaml")
    ap.add_argument("--since", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    cfg = RuntimeConfig.load(Path(a.config))
    db = create_engine(cfg.database_url)
    tick = provisional_filters(cfg.symbol).tick
    m1s = with_holes(load(db, cfg.symbol, datetime.fromisoformat(a.since)))
    rt = RunTracker(tick)
    counts: Counter[str] = Counter()
    first_old = first_new = 0
    runs_old: set[tuple[str, str]] = set()
    examples: dict[str, dict[str, Any]] = {}
    for m in m1s:
        for s in rt.on_m1(m):
            k = cls(s.quality)
            counts[k] += 1
            counts[f"{s.side}:{k}"] += 1
            run_id = (str(s.side), s.origin.open_time.isoformat())
            if run_id not in runs_old:  # old rule: any geometric P-Gap was the first
                runs_old.add(run_id)
                first_old += 1
            first_new += int(s.first_in_run)
            ex_key = f"{s.side.value}_{k}"
            if ex_key not in examples:
                examples[ex_key] = {
                    "c1": candle_json(s.left),
                    "c2": candle_json(s.middle),
                    "c3": candle_json(s.right),
                    "quality": s.quality.as_dict(),
                }
    geometric = sum(v for k, v in counts.items() if ":" not in k)
    report = {
        "label": "REPLAY UNDER SPEC 5.9 (read-only; recorded outcomes untouched)",
        "generated_at": datetime.now(UTC).isoformat(),
        "since": a.since,
        "minutes": len(m1s),
        "tick": str(tick),
        "geometric_pgaps_in_runs": geometric,
        "classification": dict(counts),
        "first_pgap_per_run": {"old_rule": first_old, "new_rule_first_qualifying": first_new},
        "engine_replay": {
            "old_rule": engine_candidates(m1s, cfg, old_rule=True),
            "new_rule": engine_candidates(m1s, cfg, old_rule=False),
        },
        "examples": examples,
    }
    s = json.dumps(report, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(s + "\n")
    print(s)


if __name__ == "__main__":
    main()
