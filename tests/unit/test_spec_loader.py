"""GOV-01: the rules file is hash-pinned, frozen, exact, and matches the coded expressions."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from sp2l.indicators import regime, trend
from sp2l.spec.loader import DEFAULT_RULES_PATH, SpecIntegrityError, load_rules, verify_manifest
from sp2l.strategy import pgap, sequence


@pytest.fixture(scope="module")
def rules():
    return load_rules()


def test_loads_pinned_rules(rules):
    assert rules.version == "6.0"
    assert rules.get("market.initial_symbol") == "BTCUSDT"


def test_hash_mismatch_refuses_to_load(tmp_path: Path):
    lock = tmp_path / "spec.lock"
    lock.write_text("sha256=" + "0" * 64 + "\n")
    with pytest.raises(SpecIntegrityError, match="does not match"):
        load_rules(DEFAULT_RULES_PATH, lock)


def test_invariant_violation_refuses_to_load(tmp_path: Path):
    import hashlib

    text = DEFAULT_RULES_PATH.read_text().replace("tp2: false", "tp2: true")
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(text)
    lock = tmp_path / "spec.lock"
    lock.write_text("sha256=" + hashlib.sha256(text.encode()).hexdigest() + "\n")
    with pytest.raises(SpecIntegrityError, match="take_profit.tp2"):
        load_rules(rules_file, lock)


def test_rules_are_frozen(rules):
    with pytest.raises(TypeError):
        rules.raw["market"]["leverage"] = 20  # type: ignore[index]


def test_manifest_matches_every_spec_file():
    verify_manifest()


def test_floats_parsed_as_exact_decimal(rules):
    assert rules.get("context.range_middle.min_inclusive") == "1/3"  # B08 exact rational
    assert rules.get("exhaustion.outer_edge.long_range_position_gte") == Decimal("0.90")


def test_coded_expressions_match_yaml(rules):
    """Any spec edit to these expressions must fail the build until code is re-audited."""
    assert rules.get("pgap.bullish") == pgap.BULLISH_EXPR
    assert rules.get("pgap.bearish") == pgap.BEARISH_EXPR
    from fractions import Fraction

    q = rules.get("pgap.qualification")
    assert Fraction(q["body_ratio_min"]) == pgap.BODY_RATIO_MIN
    assert Fraction(q["gap_body_ratio_min"]) == pgap.GAP_BODY_RATIO_MIN
    assert q["gap_ticks_min"] == pgap.GAP_TICKS_MIN
    assert set(q["reason_codes"]) == {
        pgap.WRONG_DIRECTION,
        pgap.BODY_TOO_WEAK,
        pgap.GAP_SMALL_VS_BODY,
        pgap.GAP_SMALL_TICKS,
    }
    assert rules.get("directional_structure.long") == sequence.LONG_EXPR
    assert rules.get("directional_structure.short") == sequence.SHORT_EXPR
    assert rules.get("context.trend.bull") == trend.BULL_EXPR
    assert rules.get("context.trend.bear") == trend.BEAR_EXPR
    assert rules.get("context.regime.range") == regime.RANGE_EXPR
    assert rules.get("context.regime.trend") == regime.TREND_EXPR
    assert rules.get("context.pivots.left") == 2
    assert rules.get("context.pivots.right") == 2
