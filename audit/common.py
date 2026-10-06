"""Shared inputs of the audit: config, markets, the frozen end of the history, M1 loading."""

from __future__ import annotations

import pickle
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine

from sp2l.config import RuntimeConfig
from sp2l.core.types import Candle
from sp2l.smc.history import load_bars
from sp2l.smc.model import Costs, SmcParams

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "audit"
CACHE = ROOT / "audit" / ".cache"
# Frozen end so every number in the report is reproducible while the database keeps growing.
UPTO = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)
DAYS = 270
MAIN = ["BTCUSDT", "XRPUSDT"]
# Markets with >= 165 days of history: used to get more trades for the execution checks.
EXTRA = ["ETHUSDT", "SOLUSDT", "DOGEUSDT", "ADAUSDT", "BNBUSDT", "LTCUSDT", "AVAXUSDT"]
TICKS = {
    "BTCUSDT": Decimal("0.1"),
    "XRPUSDT": Decimal("0.00001"),
    "ETHUSDT": Decimal("0.01"),
    "SOLUSDT": Decimal("0.01"),
    "DOGEUSDT": Decimal("0.00001"),
    "ADAUSDT": Decimal("0.0001"),
    "BNBUSDT": Decimal("0.01"),
    "LTCUSDT": Decimal("0.01"),
    "AVAXUSDT": Decimal("0.001"),
}


def cfg() -> RuntimeConfig:
    return RuntimeConfig.load(ROOT / "config" / "runtime.yaml")


def db() -> Engine:
    return create_engine(cfg().database_url)


def costs() -> Costs:
    return cfg().costs()


def params(symbol: str, **over: Any) -> SmcParams:
    """The parameters in force (config/runtime.yaml) with the market's tick, plus overrides."""
    base = replace(cfg().smc_params(), tick=TICKS[symbol])
    return SmcParams.from_mapping({**base.as_dict(), **over}) if over else base


# SMC-2.2 as documented in docs/SMC_STRATEGY.md (the last config that produced trades).
SMC22 = dict(
    htf_grid="tehran",
    confirm_in_zone=False,
    entry_ref="htf_fvg_ce",
    sl_mode="ob_height",
    tp_mode="hh_ll",
    tp_rr=3,
    discount_ref="target",
    require_discount=False,
    min_net_rr=0,
    max_cost_frac=0,
    liq_buffer_r=0,
)
# The current rules without the reject-only filters (same zones, entries, stops, targets).
NOFILTER = dict(require_discount=False, min_net_rr=0, max_cost_frac=0)


def m1(symbol: str, days: int = DAYS, upto: datetime = UPTO) -> list[Candle]:
    """The merged M1 series [upto - days, upto), cached on disk (pickle) for speed."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{symbol}_{days}_{int(upto.timestamp())}.pkl"
    if f.exists():
        return pickle.loads(f.read_bytes())
    bars = load_bars(db(), symbol, "1m", days * 1440, upto)
    f.write_bytes(pickle.dumps(bars))
    return bars
