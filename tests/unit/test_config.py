"""B16: Shadow fee/slippage values are validated and never defaulted."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from sp2l.config import ConfigError, RuntimeConfig


def cfg(costs):
    return RuntimeConfig({"database_url": "x", "symbol": "BTCUSDT", "costs": costs})


def test_missing_values_fail_clearly_naming_each_field():
    with pytest.raises(ConfigError) as e:
        cfg({"maker_fee": None, "taker_fee": "0.0005"}).shadow_costs()
    msg = str(e.value)
    assert "costs.maker_fee is missing" in msg and "costs.slippage_allowance is missing" in msg
    assert "never hardcoded" in msg


@pytest.mark.parametrize("bad", ["-0.001", "abc", "0.05", "nan"])
def test_invalid_or_percent_unit_values_rejected(bad):
    with pytest.raises(ConfigError):
        cfg(
            {"maker_fee": bad, "taker_fee": "0.0005", "slippage_allowance": "0.0005"}
        ).shadow_costs()


def test_valid_values_map_to_cost_model():
    m = cfg(
        {"maker_fee": "0.0002", "taker_fee": 0.0005, "slippage_allowance": "0.0003"}
    ).shadow_costs()
    assert (m.entry_fee_rate, m.exit_fee_rate, m.sl_slippage_rate) == (
        D("0.0002"),
        D("0.0005"),
        D("0.0003"),
    )
    assert m.evidence_id is None


def test_repo_config_costs_are_backed_by_recorded_evidence():
    """B16: costs are never invented. Every configured value must equal the recorded evidence
    (docs/cost_evidence.json from Tabdeal's public commission table and measured slippage)."""
    import json
    from decimal import Decimal
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    ev = json.loads((root / "docs" / "cost_evidence.json").read_text())
    for name in ("runtime.yaml", "server.yaml"):
        c = RuntimeConfig.load(root / "config" / name).shadow_costs()
        assert c.evidence_id and c.evidence_id.startswith("docs/cost_evidence.json")
        assert c.entry_fee_rate == Decimal(ev["maker_fee"])
        assert c.exit_fee_rate == Decimal(ev["taker_fee"])
        assert c.sl_slippage_rate == Decimal(ev["slippage_allowance"])
    assert ev["fee_level"]["from_volume"] == "0"  # level 1 (account volume ~0)
