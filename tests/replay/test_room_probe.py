"""Analysis-only RoomToTP probe: never influences runtime, records complete causal evidence."""

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


def test_probes_never_change_any_runtime_result():
    with_probes, rec = run(True)
    without, _ = run(False)
    assert rec.records, "the tape must produce RoomToTP rejections"
    assert runtime_hash(with_probes) == runtime_hash(without)


def test_probe_records_are_complete_and_causal():
    eng, rec = run(True)
    results = {r["result"] for r in rec.records}
    assert results  # e.g. RUN_ENDED / NO_CLOSE_THROUGH_OBSTACLE / REJECTED_* / CLOSED
    for r in rec.records:
        assert r["probe_version"] == "room-b1"
        assert r["blocking_swing"] and r["distance_r"] >= 0
        assert "same_spike_alive" in r or r["result"] in ("AMBIGUOUS_DATA_GAP", "PROBE_ERROR")
        if r.get("closed_through") and r["result"] not in ("RUN_ENDED", "PULLBACK_STARTED"):
            re = r["reevaluation"]
            # exactly the consumed swing is gone; any OTHER swing in the path still blocks
            assert re["obstacle"] != r["blocking_swing"]
        if r["filled"]:
            ex = r["execution"]
            assert D(ex["e1_filled_qty"]) > 0 and ex["r_usdt"]
            pb = r["pullback_minute"]
            assert pb is not None
    assert not eng.probes or all(not p.done for p in eng.probes)
