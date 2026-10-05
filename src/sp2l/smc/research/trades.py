"""Trade-level research: the engine's own setups and evaluation (strategy.zone_setups,
find_arming, evaluate), walked on numpy M1 arrays with switchable cost and fill models.

`path` finds what happened to an order with no costs involved (fill, exit minute, exit kind,
exit level); `result` prices that path under a cost model. With the defaults (maker entry,
market take-profit, Tabdeal costs) the R equals the engine lifecycle's R
(tests/smc/test_research.py). Options:
- tp="maker_through": the target is a resting limit (maker fee, exact price) that fills only
  when price trades at least one tick through it (hypothetical: Tabdeal futures has no
  reduce-only orders; its TP is a trigger executed at market);
- entry="market": the entry pays the taker fee and the slippage allowance instead of maker.
Gross = the move to the exit level in stop distances, no fee, no slippage.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

import numpy as np

from sp2l.core.types import Side
from sp2l.smc.backtest import orders
from sp2l.smc.diagnostics.core import bias_ignored, utc_grid
from sp2l.smc.model import Analysis, Costs, Setup, SmcParams
from sp2l.smc.research.data import Market
from sp2l.smc.structure import analyze
from sp2l.smc.timeframes import MINUTE, aggregate

TFS = ("5m", "15m", "1h", "4h", "1d")


def contexts(m: Market, p: SmcParams, grid: str = "tehran") -> dict[str, Analysis]:
    """Every timeframe the variants use (1m carries the bars only)."""
    end = m.bars[-1].open_time + MINUTE

    def build() -> dict[str, Analysis]:
        ctx = {tf: analyze(aggregate(m.bars, tf, end), tf, p) for tf in TFS}
        ctx["1m"] = Analysis(tf="1m", bars=m.bars, atr=[None] * len(m.bars))
        return ctx

    if grid == "utc":
        with utc_grid():
            return build()
    return build()


@contextmanager
def _bias(off: bool) -> Iterator[None]:
    if off:
        with bias_ignored({}):
            yield
    else:
        yield


def setups(
    ctx: dict[str, Analysis], p: SmcParams, costs: Costs, bias_off: bool = False
) -> list[Setup]:
    with _bias(bias_off):
        return orders(ctx, p, costs)


@dataclass
class Path:
    setup: Setup
    filled: int  # M1 index of the fill (-1: none)
    exit: int  # M1 index of the exit minute (-1: none / still open at the data's end)
    kind: str  # TP / SL / TIMEOUT / EXPIRED / MISSED / OPEN
    level: float  # exit level before slippage (stop, target or the minute's close)
    end_ts: float = float("inf")  # when the order stopped occupying capacity (epoch s)

    @property
    def closed(self) -> bool:
        return self.kind in ("TP", "SL", "TIMEOUT")


def _first(mask: np.ndarray) -> int:
    if mask.size == 0:
        return -1
    k = int(mask.argmax())
    return k if mask[k] else -1


def path(s: Setup, m: Market, p: SmcParams, tp_through: bool = False) -> Path:
    assert s.entry is not None and s.sl is not None and s.tp is not None
    long = s.direction is Side.LONG
    e, sl, tp = float(s.entry), float(s.sl), float(s.tp.price)
    tpx = tp + (float(m.tick) if long else -float(m.tick)) if tp_through else tp
    t0 = s.created_at.timestamp()
    i0 = m.index_at(t0)
    if s.market:
        f = i0
    else:
        iexp = m.index_at(t0 + p.pending_expiry_min * 60)  # the minute it expires on
        w = slice(i0, min(iexp + 1, len(m.t)))
        fill = _first(m.lo[w] <= e if long else m.h[w] >= e)
        miss = _first(m.h[w] >= tp if long else m.lo[w] <= tp)
        if fill < 0 or (miss >= 0 and miss < fill):
            if miss >= 0:
                return Path(s, -1, i0 + miss, "MISSED", tp)
            return Path(s, -1, -1 if iexp >= len(m.t) else iexp, "EXPIRED", tp)
        f = i0 + fill
    if (m.lo[f] <= sl) if long else (m.h[f] >= sl):  # the fill minute: its stop counts
        return Path(s, f, f, "SL", sl)
    ito = m.index_at(m.t[f] + p.max_hold_min * 60)
    w = slice(f + 1, min(ito + 1, len(m.t)))
    st = _first(m.lo[w] <= sl if long else m.h[w] >= sl)
    tg = _first(m.h[w] >= tpx if long else m.lo[w] <= tpx)
    if st >= 0 and (tg < 0 or st <= tg):
        return Path(s, f, f + 1 + st, "SL", sl)
    if tg >= 0:
        return Path(s, f, f + 1 + tg, "TP", tp)
    if ito < len(m.t):
        return Path(s, f, ito, "TIMEOUT", float(m.c[ito]))
    return Path(s, f, -1, "OPEN", float("nan"))


def result(
    pt: Path, costs: Costs, entry: str = "maker", tp: str = "market"
) -> tuple[float, float] | None:
    """(gross in stop distances, net R) of a closed path under a cost model."""
    if not pt.closed:
        return None
    s = pt.setup
    assert s.entry is not None and s.sl is not None
    long = s.direction is Side.LONG
    sgn = 1.0 if long else -1.0
    e, sl = float(s.entry), float(s.sl)
    mk, tk, sp = float(costs.maker_fee), float(costs.taker_fee), float(costs.slippage)
    d = abs(e - sl)
    fill_in = e + sgn * e * sp if entry == "market" else e  # a market entry also slips
    fee_in = tk if entry == "market" else mk
    sl_fill = sl - sgn * sl * sp
    risk = abs(fill_in - sl) + fill_in * fee_in + sl * sp + sl_fill * tk
    if pt.kind == "TP" and tp == "maker_through":
        x, fee_out = pt.level, mk
    else:  # stop, market TP, timeout: market exits (the timeout at the minute's close)
        x = pt.level - sgn * pt.level * sp if pt.kind != "TIMEOUT" else pt.level
        fee_out = tk
    net = (x - fill_in) * sgn - fill_in * fee_in - x * fee_out
    gross = (pt.level - e) * sgn / d
    return gross, net / risk


def capacity(paths: Sequence[Path], max_active: int = 1) -> list[Path]:
    """Orders in time order, at most `max_active` at once (as backtest.run)."""
    out: list[Path] = []
    busy: list[float] = []
    for pt in sorted(paths, key=lambda x: x.setup.created_at):
        t = pt.setup.created_at.timestamp()
        busy = [b for b in busy if b > t]
        if len(busy) >= max_active:
            continue
        out.append(pt)
        busy.append(pt.end_ts)
    return out


def _ended(pt: Path, m: Market) -> Path:
    pt.end_ts = float(m.t[pt.exit]) + 60 if pt.exit >= 0 else float("inf")
    return pt


def trades(ss: Sequence[Setup], m: Market, p: SmcParams, tp_through: bool = False) -> list[Path]:
    return capacity([_ended(path(s, m, p, tp_through), m) for s in ss if s.accepted], p.max_active)


def random_baseline(
    ps: Sequence[Path], m: Market, p: SmcParams, start: datetime, end: datetime, seed: int
) -> list[Path]:
    """As many random market entries as `ps` has filled trades: same side mix, same stop and
    target distances in % of price, random minutes of the same market and period."""
    rng = np.random.default_rng(seed)
    out: list[Path] = []
    lo_i, hi_i = m.index_at(start.timestamp()), m.index_at(end.timestamp()) - 1
    for pt in ps:
        if pt.filled < 0 or hi_i <= lo_i:
            continue
        s = pt.setup
        assert s.entry is not None and s.sl is not None and s.tp is not None
        i = int(rng.integers(lo_i, hi_i))
        px = Decimal(str(m.c[i]))
        k_sl = (s.sl - s.entry) / s.entry
        k_tp = (s.tp.price - s.entry) / s.entry
        r = replace(
            s,
            created_at=datetime.fromtimestamp(float(m.t[i]) + 60, s.created_at.tzinfo),
            entry=px,
            sl=px * (1 + k_sl),
            tp=replace(s.tp, price=px * (1 + k_tp)),
            market=True,
        )
        out.append(_ended(path(r, m, p), m))
    return out


def stats(rows: Sequence[tuple[float, float]], seed: int = 3) -> dict[str, Any]:
    """trades, win rate, gross, net, PF and the mean net R with a 95 % bootstrap interval."""
    if not rows:
        return {"n": 0}
    g = np.array([x[0] for x in rows])
    r = np.array([x[1] for x in rows])
    wins = r[r > 0].sum()
    loss = -r[r <= 0].sum()
    rng = np.random.default_rng(seed)
    means = r[rng.integers(0, r.size, size=(2000, r.size))].mean(axis=1)
    gm = g[rng.integers(0, g.size, size=(2000, g.size))].mean(axis=1)
    return {
        "n": int(r.size),
        "win": float((r > 0).mean()),
        "gross": float(g.sum()),
        "gross_avg": float(g.mean()),
        "gross_ci": (float(np.percentile(gm, 2.5)), float(np.percentile(gm, 97.5))),
        "net": float(r.sum()),
        "pf": float(wins / loss) if loss > 0 else float("inf"),
        "avg": float(r.mean()),
        "ci": (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))),
    }
