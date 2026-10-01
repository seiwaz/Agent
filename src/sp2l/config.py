"""Runtime configuration (non-strategy) with validation.

Costs (V5.1 B16) are never hardcoded; Shadow refuses to start until they are provided:
- maker_fee: fraction charged on resting limit fills (E1, E2; E1 is never marketable, B15);
- taker_fee: fraction charged on exits (SL is taker; the single TP is also charged taker,
  conservatively, until Tabdeal's positionSlTp execution type is runtime-validated);
- slippage_allowance: fraction by which the modeled SL exit may be worse than SL.
Values are fractions (0.0002 = 0.02 %). Values >= 0.01 are rejected as a likely percent/
fraction unit mistake.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from sp2l.strategy.risk.engine import CostModel

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

    def shadow_costs(self) -> CostModel:
        """Validated CostModel; raises ConfigError naming every missing/invalid field."""
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
            raise ConfigError(
                "Shadow cannot start; fee/slippage values are never hardcoded (B16): "
                + "; ".join(problems)
            )
        evidence = costs.get("evidence_id")
        return CostModel(
            entry_fee_rate=values["maker_fee"],
            exit_fee_rate=values["taker_fee"],
            sl_slippage_rate=values["slippage_allowance"],
            evidence_id=str(evidence) if evidence else None,
        )
