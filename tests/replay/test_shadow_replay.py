"""End-to-end Shadow replay: causality, determinism, ledger identity, CF isolation."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D

import pytest

from sp2l.engine.model import ExitKind, SetupState
from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.runtime.replay import replay
from sp2l.strategy.risk.engine import CostModel, ExchangeFilters
from tests.conftest import T0
from tests.replay.tape import tape

MINUTES = 900
COSTS = CostModel(D("0.0004"), D("0.0006"), D("0.0005"))  # provisional (no evidence id)
FILTERS = ExchangeFilters(D("0.1"), D("0.001"), None, None, verified=False)


def run(seed: int = 3) -> ShadowSymbolEngine:
    eng = ShadowSymbolEngine("BTCUSDT", tick=D("0.1"), costs=COSTS, filters=FILTERS, warmup_bars=30)
    end = T0 + timedelta(minutes=MINUTES)
    replay(eng, tape(MINUTES, seed), start=T0, end=end + timedelta(minutes=1), healthy_until=end)
    return eng


@pytest.fixture(scope="module")
def eng() -> ShadowSymbolEngine:
    return run()


def fingerprint(e: ShadowSymbolEngine) -> str:
    return json.dumps(
        {
            "summary": e.summary(),
            "events": [m.events for m in e.finished],
            "cf": [str(c.outcome()) for c in e.counterfactuals],
        },
        sort_keys=True,
        default=str,
    )


def test_pipeline_produces_candidates_and_logs_warmup_pgaps(eng):
    assert eng.finished or eng.active
    reasons = {p.reason for p in eng.pgaps}
    assert "DATA_WARMUP" in reasons  # before the M5 state is warm (B25)
    early = [p for p in eng.pgaps if p.ts < T0 + timedelta(minutes=150)]
    assert all(not p.promoted for p in early)


def test_deterministic_replay():
    assert fingerprint(run()) == fingerprint(run())


def test_no_retroactive_fill_ever(eng):
    assert all(trade_seq > submit_seq for _, trade_seq, submit_seq in eng.broker.fill_audit)
    for cf in eng.counterfactuals:
        assert all(t > s for _, t, s in cf.broker.fill_audit)


def test_shadow_wallet_identity_b16(eng):
    b = eng.broker
    assert eng.wallet_balance() == D(100) + b.realized_pnl - b.fees


def test_finished_setups_are_terminal_with_single_tp(eng):
    for m in eng.finished:
        assert m.terminal
        for ex in m.exits:
            if ex.kind is ExitKind.TP:
                assert ex.price == m.levels.e1 + m.levels.r or ex.price == m.levels.e1 - m.levels.r


def test_counterfactuals_isolated_and_labelled(eng):
    assert all(not cid.startswith("cf-") for cid, _, _ in eng.broker.fill_audit)
    rejected = [
        m
        for m in eng.finished
        if m.state in (SetupState.REJECTED_CONTEXT, SetupState.REJECTED_EXHAUSTION)
    ]
    assert len(eng.counterfactuals) == len(rejected)
    for cf in eng.counterfactuals:
        assert cf.outcome().label == "COUNTERFACTUAL"
        assert cf.broker is not eng.broker
