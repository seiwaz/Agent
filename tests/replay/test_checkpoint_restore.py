"""Checkpoint exactness and exact (no-gap) hand-over equivalence."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D

from sp2l.engine import checkpoint as ck
from sp2l.engine.gates import BaseSp2lGates
from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.marketdata.m1_builder import M1Builder
from sp2l.marketdata.m5_aggregator import M5Result
from sp2l.strategy.risk.engine import CostModel, ExchangeFilters
from tests.conftest import T0
from tests.replay.tape import tape

MINUTES = 900
COSTS = CostModel(D("0.0004"), D("0.0006"), D("0.0005"))
FILTERS = ExchangeFilters(D("0.1"), D("0.001"), None, None, verified=False)


def new_engine() -> ShadowSymbolEngine:
    return ShadowSymbolEngine(
        "BTCUSDT", tick=D("0.1"), costs=COSTS, filters=FILTERS, warmup_bars=30
    )


class M5Log:
    def __init__(self):
        self.m5: list[M5Result] = []

    def on_m1(self, m1):
        pass

    def on_m5(self, m5):
        self.m5.append(m5)

    def on_indicators(self, snapshot):
        pass

    def on_pgap(self, log):
        return None

    def on_candidate_created(self, m, pgap_id):
        pass

    def on_archived(self, m, broker):
        pass

    def on_counterfactual(self, run):
        pass

    def on_setup_event(self, m, kind, event):
        pass

    def checkpoint(self, engine):
        pass


def drive(switch_when=None):
    """Replay; optionally hand over to a restored engine the first time `switch_when` holds."""
    trades = tape(MINUTES, 3)
    end = T0 + timedelta(minutes=MINUTES)
    log = M5Log()
    eng = new_engine()
    eng.recorder = log
    first = eng
    builder = M1Builder(T0)
    builder.mark_healthy_until(end)
    switched = False
    for t in sorted(trades, key=lambda x: (x.recv_ts, x.exch_ts, x.trade_id)):
        for m1 in builder.advance(t.recv_ts):
            eng.on_m1(m1)
        eng.on_trade(t.price, t.exch_ts)
        builder.add_trade(t)
        if switch_when and not switched and switch_when(eng):
            state = json.loads(json.dumps(eng.checkpoint()))
            broker = json.loads(json.dumps(ck.dump_broker(eng.broker)))
            restored = new_engine()
            anchor, last = state["m5_anchor"], state["m5_last_open"]
            segment = [m for m in log.m5 if anchor and anchor <= m.open_time.isoformat() <= last]
            restored.restore(state, broker, segment)

            restored.recorder = log
            restored.finished = list(eng.finished)
            eng = restored
            switched = True
    for m1 in builder.advance(end + timedelta(minutes=1)):
        eng.on_m1(m1)
    return first, eng, switched


def outcome(e: ShadowSymbolEngine):
    return (
        [(m.cfg.setup_id, m.state.value, m.primary_reason, str(m.levels.e1)) for m in e.finished],
        str(e.wallet_balance()),
        sorted(e.broker.fill_audit),
    )


def test_machine_and_broker_checkpoints_round_trip_exactly():
    _, eng, _ = drive()
    for m in [*eng.finished, *(cf.machine for cf in eng.counterfactuals)]:
        port = eng.broker
        j = ck.dump_machine(m)
        assert (
            ck.dump_machine(ck.load_machine(json.loads(json.dumps(j)), port, BaseSp2lGates())) == j
        )
    b = ck.dump_broker(eng.broker)
    assert ck.dump_broker(ck.load_broker(json.loads(json.dumps(b)), COSTS)) == b


def test_exact_handover_mid_setup_matches_uninterrupted_run():
    _, baseline, _ = drive()
    first, resumed, switched = drive(lambda e: e.active is not None and e.active.e1_id is not None)
    assert switched, "a setup with an armed E1 must exist to exercise the hand-over"
    assert outcome(resumed) == outcome(baseline)
    before = [
        c for i, c in enumerate(first.counterfactuals) if c.done and i in first._cf_recorded
    ]  # finished before the hand-over
    combined = sorted(str(c.outcome()) for c in [*before, *resumed.counterfactuals])
    assert combined == sorted(str(c.outcome()) for c in baseline.counterfactuals)
