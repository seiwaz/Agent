"""Fee/slippage values are validated and never defaulted; the smc section is validated."""

from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from sp2l.config import ConfigError, RuntimeConfig

ROOT = Path(__file__).resolve().parents[2]


def cfg(costs, smc=None):
    return RuntimeConfig(
        {"database_url": "x", "symbol": "BTCUSDT", "costs": costs, "smc": smc or {}}
    )


def test_missing_values_fail_clearly_naming_each_field():
    with pytest.raises(ConfigError) as e:
        cfg({"maker_fee": None, "taker_fee": "0.0005"}).costs()
    msg = str(e.value)
    assert "costs.maker_fee is missing" in msg and "costs.slippage_allowance is missing" in msg
    assert "never hardcoded" in msg


@pytest.mark.parametrize("bad", ["-0.001", "abc", "0.05", "nan"])
def test_invalid_or_percent_unit_values_rejected(bad):
    with pytest.raises(ConfigError):
        cfg({"maker_fee": bad, "taker_fee": "0.0005", "slippage_allowance": "0.0005"}).costs()


def test_valid_values_map_to_costs():
    m = cfg({"maker_fee": "0.0002", "taker_fee": 0.0005, "slippage_allowance": "0.0003"}).costs()
    assert (m.maker_fee, m.taker_fee, m.slippage) == (D("0.0002"), D("0.0005"), D("0.0003"))


def test_smc_section_overrides_defaults_and_rejects_unknown_keys():
    p = cfg({}, {"swing_len": 3, "zone_tf": "15m", "tp_ref": "swing"}).smc_params()
    assert (p.swing_len, p.zone_tf, p.tp_ref) == (3, "15m", "swing")
    with pytest.raises(ConfigError, match="unknown"):
        cfg({}, {"poi_tfs": ["1h"]}).smc_params()  # SMC-1.0 keys are gone
    with pytest.raises(ConfigError, match="unknown"):
        cfg({}, {"swing_length": 3}).smc_params()


def test_symbols_instruments_and_per_market_params():
    c = RuntimeConfig(
        {
            "database_url": "x",
            "symbols": ["BTCUSDT", "XRPUSDT"],
            "instruments": {"XRPUSDT": {"tick": "0.00001", "step": "0.1"}},
            "smc": {"swing_len": 4},
        }
    )
    assert c.symbols == ["BTCUSDT", "XRPUSDT"] and c.symbol == "BTCUSDT"
    assert c.instrument("XRPUSDT") == (D("0.00001"), D("0.1"))
    p = c.symbol_params("XRPUSDT")
    assert (p.tick, p.swing_len) == (D("0.00001"), 4)
    one = c.with_symbol("XRPUSDT")
    assert one.symbols == ["XRPUSDT"] and one.symbol == "XRPUSDT"
    with pytest.raises(ConfigError):
        c.with_symbol("ETHUSDT")


def test_repo_configs_load_and_costs_match_recorded_evidence():
    ev = json.loads((ROOT / "docs" / "cost_evidence.json").read_text())
    for name in ("runtime.yaml", "server.yaml"):
        c = RuntimeConfig.load(ROOT / "config" / name)
        k = c.costs()
        assert k.maker_fee == D(ev["maker_fee"]) and k.taker_fee == D(ev["taker_fee"])
        assert k.slippage == D(ev["slippage_allowance"])
        c.smc_params()
        assert c.symbols == ["BTCUSDT", "XRPUSDT"]
        assert c.instrument("BTCUSDT") == (D("0.1"), D("0.00001"))


def test_boolean_parameters_stay_booleans():
    p = cfg({}, {"ob_require_fvg": True, "confirm_exec": "false"}).smc_params()
    assert p.ob_require_fvg is True and p.confirm_exec is False
