"""Runtime configuration with validation.

Costs are never hardcoded; the SMC runner refuses to start until they are provided:
- maker_fee: fraction charged on resting limit entries;
- taker_fee: fraction charged on market entries and on every exit (TP charged taker,
  conservatively);
- slippage_allowance: fraction by which a stop exit may be worse than the stop price.
Values are fractions (0.0002 = 0.02 %). Values >= 0.01 are rejected as a likely percent/
fraction unit mistake.

The `smc` section overrides SmcParams defaults (see smc/model.py); unknown keys are errors.
"""

from __future__ import annotations

from dataclasses import dataclass
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
        for key in ("database_url", "symbol"):
            if not data.get(key):
                raise ConfigError(f"{path}: `{key}` is required")
        return cls(data)

    @property
    def database_url(self) -> str:
        return str(self.raw["database_url"])

    @property
    def symbol(self) -> str:
        return str(self.raw["symbol"])

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
        if problems:
            raise ConfigError("fee/slippage values are never hardcoded: " + "; ".join(problems))
        return Costs(values["maker_fee"], values["taker_fee"], values["slippage_allowance"])
