"""Load the authoritative SP2L_RULES.yaml, hash-pinned and deep-frozen (GOV-01).

The rules file is read verbatim. Its SHA-256 must equal the pin in config/spec.lock;
any mismatch stops the process. YAML floats are parsed as Decimal from their source
text so thresholds such as 61.8 or 0.3333333333 are exact.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RULES_PATH = REPO_ROOT / "spec" / "SP2L_RULES.yaml"
DEFAULT_LOCK_PATH = REPO_ROOT / "config" / "spec.lock"
DEFAULT_MANIFEST_PATH = REPO_ROOT / "spec" / "MANIFEST_SHA256.json"


class SpecIntegrityError(RuntimeError):
    """The rules file does not match its pin or violates a frozen V5 invariant."""


class _DecimalSafeLoader(yaml.SafeLoader):
    pass


def _construct_decimal(loader: yaml.SafeLoader, node: yaml.Node) -> Decimal:
    return Decimal(str(loader.construct_scalar(node)))  # type: ignore[arg-type]


_DecimalSafeLoader.add_constructor("tag:yaml.org,2002:float", _construct_decimal)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def read_lock(lock_path: Path) -> str:
    for line in lock_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("sha256="):
            return line.removeprefix("sha256=").strip()
    raise SpecIntegrityError(f"no sha256= line in {lock_path}")


@dataclass(frozen=True)
class Rules:
    sha256: str
    raw: Mapping[str, Any]

    def get(self, dotted: str) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                raise KeyError(f"rule path not found: {dotted}")
            node = node[part]
        return node

    @property
    def version(self) -> str:
        return str(self.get("spec.version"))

    @property
    def leverage(self) -> int:
        """V5.8: the strategy leverage (an invariant, exactly 10)."""
        return int(self.get("market.leverage"))


# Frozen V5 invariants that must hold in any accepted rules file.
_INVARIANTS: tuple[tuple[str, Any], ...] = (
    ("spec.runtime_learning", False),
    ("spec.automatic_rule_mutation", False),
    ("market.entry_timeframe", "1m"),
    ("market.context_timeframe", "5m"),
    ("market.leverage", 10),
    ("market.margin_mode", "CROSS"),
    ("pgap.equality_allowed", False),
    ("pgap.finalized_only", True),
    ("directional_structure.candle_color_gate", False),
    ("take_profit.count", 1),
    ("take_profit.tp2", False),
    ("take_profit.partial_tp", False),
    ("e1.any_fill_freezes_entry_price", True),
    ("e1.no_retroactive_fill", True),
    ("e2.must_be_non_reducing", True),
    ("shadow.initial_wallet_usdt", 100),
    ("shadow.counterfactual_influences_runtime", False),
    ("ui.backend_authoritative", True),
    # V5.1 decisions
    ("spec.version", "6.0"),
    ("pgap.qualification.enforced", False),
    ("risk.net_at_tp.rule", "abs(TP - E1) - E1*entry_fee_rate - TP*exit_fee_rate > 0"),
    ("risk.net_at_tp.legs", "E1_ONLY"),
    ("context.gate.pass_iff", "net_tp AND (level_break OR channel_edge OR htf_aligned)"),
    ("context.gate.net_tp", "abs(TP - E1) - E1*entry_fee_rate - TP*exit_fee_rate > 0"),
    ("context.gate.no_other_gates", True),
    ("exhaustion.gating", False),
    ("pgap.qualification.body_ratio_min", "0.60"),
    ("pgap.qualification.gap_body_ratio_min", "0.15"),
    ("pgap.qualification.gap_ticks_min", 2),
    ("pgap.qualification.thresholds_frozen", True),
    ("market_data.live_forming_candle.authority", "NONE"),
    ("indicators.adx_reference", "TA_LIB"),
    ("indicators.adx_seed", "TALIB_N_MINUS_1"),
    ("indicators.runtime_implementation_switch", False),
    ("indicators.context_warmup_m5_bars", 30),
    ("e1.submit_guard.equality_sufficient", False),
    ("e1.submit_guard.marketable_or_taker_entry", False),
    ("e2.rounding", "TOWARD_SL"),
    ("risk.hardcoded_fees", False),
    ("risk.quantity.resize_after_e1_fill", False),
    ("protection.mandatory_after_any_execution", True),
    ("protection.sl_tp_working_type", "CONTRACT_PRICE"),
    ("shadow.fill_model.fabricated_partial_fills", False),
    ("market_data.unrecoverable_gap.synthesize_candles", False),
    ("spike.unarmed_expiry.status", "EXPIRED_UNARMED"),
    ("risk.quantity.live_requires_runtime_validated_margin_model", True),
)


def verify_manifest(manifest_path: Path = DEFAULT_MANIFEST_PATH) -> None:
    """Every spec file must match spec/MANIFEST_SHA256.json and none may be unlisted."""
    import json

    spec_dir = manifest_path.parent
    manifest = json.loads(manifest_path.read_text())
    present = {p.name for p in spec_dir.iterdir() if p.is_file() and not p.name.startswith(".")} - {
        manifest_path.name
    }
    if present != set(manifest):
        raise SpecIntegrityError(
            f"manifest file set differs: unlisted={sorted(present - set(manifest))} "
            f"missing={sorted(set(manifest) - present)}"
        )
    for name, entry in manifest.items():
        digest = hashlib.sha256((spec_dir / name).read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            raise SpecIntegrityError(f"{name} does not match MANIFEST_SHA256.json")


def load_rules(rules_path: Path = DEFAULT_RULES_PATH, lock_path: Path = DEFAULT_LOCK_PATH) -> Rules:
    data = rules_path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    pinned = read_lock(lock_path)
    if digest != pinned:
        raise SpecIntegrityError(
            f"{rules_path.name} sha256 {digest} does not match pinned {pinned}"
        )
    parsed = yaml.load(data, Loader=_DecimalSafeLoader)  # noqa: S506 - SafeLoader subclass
    if not isinstance(parsed, Mapping):
        raise SpecIntegrityError("rules file is not a mapping")
    rules = Rules(sha256=digest, raw=_freeze(parsed))
    for path, expected in _INVARIANTS:
        actual = rules.get(path)
        if actual != expected:
            raise SpecIntegrityError(f"invariant {path}={expected!r} violated: {actual!r}")
    return rules
