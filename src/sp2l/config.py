"""Runtime configuration with validation.

Costs are never hardcoded; the SMC runner refuses to start until they are provided:
- maker_fee: fraction charged on resting limit entries;
- taker_fee: fraction charged on market entries and on every exit (TP charged taker,
  conservatively);
- slippage_allowance: fraction by which a stop exit may be worse than the stop price.
Values are fractions (0.0002 = 0.02 %). Values >= 0.01 are rejected as a likely percent/
fraction unit mistake.

The `smc` section overrides SmcParams defaults (see smc/model.py); unknown keys are errors.
`symbols` lists the markets traded together on one shared wallet (`smc.account_usdt`);
`instruments` gives each market's price tick and quantity step.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from sp2l.smc.model import Costs, SmcParams

COST_FIELDS = ("maker_fee", "taker_fee", "slippage_allowance")
MAX_RATE = Decimal("0.01")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> RuntimeConfig:
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: not a mapping")
        if not data.get("database_url"):
            raise ConfigError(f"{path}: `database_url` is required")
        if not data.get("symbols") and not data.get("symbol"):
            raise ConfigError(f"{path}: `symbols` is required")
        return cls(data)

    def with_symbol(self, symbol: str) -> RuntimeConfig:
        """The same configuration restricted to one market (per-market commands)."""
        if symbol not in self.symbols:
            raise ConfigError(f"{symbol} is not one of the configured symbols {self.symbols}")
        return RuntimeConfig({**self.raw, "symbol": symbol, "symbols": [symbol]})

    @property
    def database_url(self) -> str:
        return str(self.raw["database_url"])

    @property
    def symbols(self) -> list[str]:
        v = self.raw.get("symbols") or [self.raw["symbol"]]
        return [str(x) for x in v]

    @property
    def symbol(self) -> str:
        """The first market (per-market commands take `--symbol`)."""
        return str(self.raw.get("symbol") or self.symbols[0])

    def instrument(self, symbol: str) -> tuple[Decimal, Decimal]:
        """(price tick, quantity step) of a market."""
        m = self.section("instruments").get(symbol) or {}
        try:
            return Decimal(str(m.get("tick", "0.01"))), Decimal(str(m.get("step", "0.001")))
        except InvalidOperation as e:
            raise ConfigError(f"instruments.{symbol}: invalid tick/step") from e

    def symbol_params(self, symbol: str) -> SmcParams:
        """Strategy parameters with the market's own price tick."""
        return replace(self.smc_params(), tick=self.instrument(symbol)[0])

    def section(self, name: str) -> dict[str, Any]:
        v = self.raw.get(name) or {}
        if not isinstance(v, dict):
            raise ConfigError(f"`{name}` must be a mapping")
        return v

    def smc_params(self) -> SmcParams:
        s = self.section("smc")
        unknown = sorted(set(s) - set(SmcParams.__dataclass_fields__))
        if unknown:
            raise ConfigError(f"smc: unknown parameter(s) {', '.join(unknown)}")
        try:
            return SmcParams.from_mapping(s)
        except (ValueError, InvalidOperation, TypeError) as e:
            raise ConfigError(f"smc: invalid value: {e}") from e

    def costs(self) -> Costs:
        """Validated costs; raises ConfigError naming every missing/invalid field."""
        costs = self.section("costs")
        problems: list[str] = []
        values: dict[str, Decimal] = {}
        for f in COST_FIELDS:
            v = costs.get(f)
            if v is None:
                problems.append(f"costs.{f} is missing")
                continue
            try:
                d = Decimal(str(v))
            except InvalidOperation:
                problems.append(f"costs.{f}={v!r} is not a number")
                continue
            if not d.is_finite() or d < 0:
                problems.append(f"costs.{f}={v!r} must be a finite non-negative fraction")
            elif d >= MAX_RATE:
                problems.append(f"costs.{f}={v!r} >= {MAX_RATE}: use a fraction (0.0002 = 0.02 %)")
            else:
                values[f] = d
        # funding (optional: Tabdeal publishes no rate; 0 = none)
        funding, interval = Decimal(0), 8
        try:
            funding = Decimal(str(costs.get("funding_rate", 0)))
            if not funding.is_finite() or abs(funding) >= MAX_RATE:
                problems.append(f"costs.funding_rate={funding} must be a fraction below {MAX_RATE}")
        except InvalidOperation:
            problems.append(f"costs.funding_rate={costs.get('funding_rate')!r} is not a number")
        try:
            interval = int(costs.get("funding_interval_h", 8))
            if interval < 0:
                problems.append("costs.funding_interval_h must be >= 0")
        except (TypeError, ValueError):
            problems.append(f"costs.funding_interval_h={costs.get('funding_interval_h')!r}")
        if problems:
            raise ConfigError("fee/slippage values are never hardcoded: " + "; ".join(problems))
        return Costs(
            values["maker_fee"],
            values["taker_fee"],
            values["slippage_allowance"],
            funding,
            interval,
        )
