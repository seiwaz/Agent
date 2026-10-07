"""Plan 3 study (docs/trend/plan3.md): six fixed variants, selection on discovery (to
2022-12-31), one holdout run (2023-01-01 ->) for the selected ones. Writes
docs/trend/plan3/results.json and prints the tables.

    uv run python scripts/trend_plan3.py            # discovery + selection only
    uv run python scripts/trend_plan3.py --holdout  # also the holdout of the candidates
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sp2l.trend import backtest as tb
from sp2l.trend import ensemble as en
from sp2l.trend.data import load_csv
from sp2l.trend.model import TrendParams

FEE, SLIP = 0.00095, 0.000326  # Tabdeal taker + measured slippage (config/runtime.yaml)
DISC = (datetime(2017, 9, 6, tzinfo=UTC), datetime(2022, 12, 31, tzinfo=UTC))
HOLD = (datetime(2023, 1, 1, tzinfo=UTC), datetime(2026, 10, 6, tzinfo=UTC))
UNIVERSE = Path("data/universe")
OUT = Path("docs/trend/plan3")


def btc_bars() -> Any:
    return load_csv(Path("data/binance_vision_BTCUSDT_1d.csv"))[0]


def curve(times: list[datetime], eq: list[float], gross: list[float]) -> dict[str, Any]:
    return {"times": times, "equity": eq, "gross": gross}


def system_b(p: TrendParams) -> dict[str, Any]:
    r = tb.run(btc_bars(), p, tb.Fees(FEE, SLIP))
    gross = [0.0] * len(r.times)
    for t in r.trades:
        end = len(r.times) - 1 if t.exit_time is None else r.times.index(t.exit_time)
        for i in range(t.entry_i, end + (1 if t.reason == "OPEN" else 0)):
            gross[i] = t.qty * r.close[i] / r.equity[i]
    return curve(r.times, r.equity, gross) | {"trades": len(r.trades)}


def ensemble(p: en.EnsembleParams, portfolio: bool) -> dict[str, Any]:
    if portfolio:
        bars = {f.stem: load_csv(f)[0] for f in sorted(UNIVERSE.glob("*USDT.csv"))}
    else:
        bars = {"BTCUSDT": btc_bars()}
    r = en.run_ensemble(en.series_from(bars), p, FEE, SLIP)
    return curve(r.times, r.equity, r.gross) | {
        "trades": r.trades,
        "costs": round(r.costs, 2),
        "traded": round(r.traded, 2),
        "forced_exits": [(s, d.date().isoformat()) for s, d in r.forced_exits],
        "members": {d.date().isoformat(): m for d, m in r.members.items()},
    }


def stats(c: dict[str, Any], a: datetime, b: datetime) -> dict[str, Any]:
    idx = [i for i, t in enumerate(c["times"]) if a <= t <= b]
    i0, i1 = idx[0], idx[-1]
    st = tb.curve_stats(c["times"][i0 : i1 + 1], c["equity"][i0 : i1 + 1])
    st["exposure_pct"] = round(100 * sum(c["gross"][i0 : i1 + 1]) / (i1 - i0 + 1), 1)
    return st


def variants(cash_yield: float = 0.0) -> dict[str, Any]:
    v0 = TrendParams(sizing="risk", risk_pct=0.03, cash_yield=cash_yield)
    ens = en.EnsembleParams(cash_yield=cash_yield)
    port = en.EnsembleParams(
        target_vol=0.5, slots=10, universe_size=10, band=0.01, cash_yield=cash_yield
    )
    return {
        "V0": lambda: system_b(v0),
        "V1": lambda: system_b(
            TrendParams(
                sizing="risk", risk_pct=0.03, max_units=4, add_atr=0.5, cash_yield=cash_yield
            )
        ),
        "V2": lambda: ensemble(ens, False),
        "V3": lambda: ensemble(en.EnsembleParams(target_vol=0.5, cash_yield=cash_yield), False),
        "P1": lambda: ensemble(port, True),
        "P2": lambda: ensemble(
            en.EnsembleParams(
                target_vol=1.0, slots=10, universe_size=10, band=0.01, cash_yield=cash_yield
            ),
            True,
        ),
    }


def buy_hold(a: datetime, b: datetime) -> dict[str, Any]:
    bars = [x for x in btc_bars() if a <= x.open_time <= b]
    q = 1 / (float(bars[0].close) * (1 + FEE + SLIP))
    return tb.curve_stats([x.open_time for x in bars], [q * float(x.close) for x in bars])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    runs = {k: f() for k, f in variants().items()}
    out: dict[str, Any] = {"buy_hold": {"discovery": buy_hold(*DISC)}, "variants": {}}
    for k, c in runs.items():
        out["variants"][k] = {
            "discovery": stats(c, *DISC),
            **{x: c[x] for x in ("trades", "costs", "traded", "forced_exits") if x in c},
        }
        if "members" in c:
            (OUT / f"{k}_universe.json").write_text(json.dumps(c["members"], indent=0))
    d0 = out["variants"]["V0"]["discovery"]
    cands = [
        k
        for k, v in out["variants"].items()
        if k != "V0"
        and v["discovery"]["sharpe"] >= d0["sharpe"] + 0.10
        and (v["discovery"]["calmar"] or 0) >= (d0["calmar"] or 0)
    ]
    out["candidates"] = cands
    # descriptive overlay (no decision): 4 %/year on idle cash, discovery
    yld = {k: f() for k, f in variants(0.04).items()}
    out["cash_yield_4pct_discovery"] = {k: stats(c, *DISC) for k, c in yld.items()}
    if a.holdout:
        bh = buy_hold(*HOLD)
        out["buy_hold"]["holdout"] = bh
        h0 = stats(runs["V0"], *HOLD)
        out["variants"]["V0"]["holdout"] = h0
        for k in cands:
            h = stats(runs[k], *HOLD)
            h["pass"] = bool(
                h["sharpe"] >= h0["sharpe"]
                and h["cagr_pct"] > 0
                and h["max_drawdown_pct"] <= bh["max_drawdown_pct"] / 2
            )
            out["variants"][k]["holdout"] = h
        out["cash_yield_4pct_holdout"] = {k: stats(yld[k], *HOLD) for k in ["V0", *cands]}
    (OUT / "results.json").write_text(json.dumps(out, indent=1, default=str))
    for k, v in out["variants"].items():
        for part in ("discovery", "holdout"):
            if part in v:
                s = v[part]
                print(
                    f"{k} {part:9s} CAGR {s['cagr_pct']:6.1f} DD {s['max_drawdown_pct']:5.1f} "
                    f"Sharpe {s['sharpe']} Calmar {s['calmar']} expo {s['exposure_pct']}"
                    + (f" pass={s['pass']}" if "pass" in s else "")
                )
    print("buy & hold", json.dumps(out["buy_hold"]))
    print("candidates", cands)


if __name__ == "__main__":
    main()
