"""Analysis-only RoomToTP probe: never influences runtime.

V6.0: RoomToTP is informational and never rejects, so the probe's trigger (a Context rejected
for RoomToTP) can no longer occur; the probe stays compiled but inert (DECISIONS V6.0)."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal as D
from typing import Any

from sp2l.engine.recording import NullRecorder
from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.marketdata.m1_builder import MINUTE
from sp2l.runtime.replay import replay
from sp2l.strategy.risk.engine import CostModel, ExchangeFilters
from tests.conftest import T0
from tests.replay.tape import tape

COSTS = CostModel(D("0.0008"), D("0.00095"), D("0.000198"))
FILTERS = ExchangeFilters(D("0.1"), D("0.001"), None, None, verified=False)
MINUTES = 1500
END = T0 + timedelta(minutes=MINUTES)


class _Probes(NullRecorder):
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def on_probe(self, record: dict[str, Any]) -> None:
        self.records.append(record)


def run(probes: bool) -> tuple[ShadowSymbolEngine, _Probes]:
    rec = _Probes()
    eng = ShadowSymbolEngine(
        "BTCUSDT",
        tick=D("0.1"),
        costs=COSTS,
        filters=FILTERS,
        warmup_bars=30,
        recorder=rec,
        probes=probes,
    )
    replay(eng, tape(MINUTES, 11), start=T0, end=END + MINUTE, healthy_until=END)
    return eng, rec


def runtime_hash(eng: ShadowSymbolEngine) -> str:
    rows = [
        [(m.cfg.setup_id, str(m.state), m.events) for m in eng.finished],
        [(p.ts, p.side, p.promoted, p.reason) for p in eng.pgaps],
        [(cf.candidate_id, cf.outcome()) for cf in eng.counterfactuals],
        str(eng.wallet_balance()),
    ]
    return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()


def test_probes_never_change_any_runtime_result_and_are_inert_under_v6():
    with_probes, rec = run(True)
    without, _ = run(False)
    assert rec.records == []  # V6.0: no Context is ever rejected for RoomToTP
    assert runtime_hash(with_probes) == runtime_hash(without)
    assert not any("ROOM_TO_TP_INSUFFICIENT" in m.reasons for m in with_probes.finished)
