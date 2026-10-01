"""Shared helpers for Shadow-runner tests."""

from __future__ import annotations

from decimal import Decimal as D

from sqlalchemy import Engine, text

from sp2l.config import RuntimeConfig
from sp2l.persistence.market_store import MarketStore
from sp2l.runtime.shadow_service import ShadowRunner
from sp2l.spec.loader import load_rules
from sp2l.strategy.risk.engine import CostModel, ExchangeFilters

# V5.11: fees small enough that the synthetic tape's setups (R >= 3.6 bps) clear the net-at-TP rule
COSTS = CostModel(D("0.0001"), D("0.0001"), D("0.0005"))
FILTERS = ExchangeFilters(D("0.1"), D("0.001"), None, None, verified=False)


def cfg(url: str, symbol: str) -> RuntimeConfig:
    return RuntimeConfig({"database_url": url, "symbol": symbol, "shadow": {"warmup_m5_bars": 30}})


def open_runner(db: Engine, url: str, symbol: str, *, fresh: bool) -> ShadowRunner:
    return ShadowRunner.open(
        db,
        MarketStore(db, symbol),
        cfg(url, symbol),
        load_rules(),
        costs=COSTS,
        filters=FILTERS,
        start_cursor=0 if fresh else None,
    )


def drain(r: ShadowRunner) -> None:
    while r.step(5000):
        pass


def step_until(r: ShadowRunner, predicate) -> bool:  # type: ignore[no-untyped-def]
    while r.step(1):
        if predicate(r.engine):
            return True
    return False


def crash_mid_input(r: ShadowRunner) -> None:
    """Apply the next journal event in memory, then die before its transaction commits."""
    with r.db.connect() as c:
        row = c.execute(
            text(
                "SELECT seq, kind, ts, payload FROM market_events WHERE symbol = :s"
                " AND seq > :q ORDER BY seq LIMIT 1"
            ),
            {"s": r.symbol, "q": r.cursor},
        ).first()
    if row is None:
        return
    r.recorder.begin_input()
    r.apply(row[1], row[2], row[3])
    r.recorder.abort_input()


def outcome(engine: Engine, session: str) -> dict[str, object]:
    """Session outcome from the DB, independent of setup-key naming."""
    with engine.connect() as c:
        setups = c.execute(
            text("""
            SELECT cc.created_at, cc.side, cc.status, cc.primary_reason,
                   sc.checkpoint->'levels'->>'e1'
            FROM candidate_current cc JOIN setup_checkpoints sc ON sc.candidate_id = cc.id
            WHERE sc.session_id = :s ORDER BY cc.created_at, cc.side"""),
            {"s": session},
        ).all()
        ledger = c.execute(
            text("SELECT kind, amount FROM shadow_wallet_ledger WHERE session_id = :s ORDER BY id"),
            {"s": session},
        ).all()
        cf = c.execute(
            text("""
            SELECT cc.created_at, o.rejection_stage, o.outcome, o.result_r
            FROM cf.counterfactual_outcomes o JOIN candidate_current cc ON cc.id = o.candidate_id
            JOIN setup_checkpoints sc ON sc.candidate_id = cc.id
            WHERE sc.session_id = :s ORDER BY cc.created_at"""),
            {"s": session},
        ).all()
    return {
        "setups": [tuple(x) for x in setups],
        "ledger": [tuple(x) for x in ledger],
        "cf": [tuple(x) for x in cf],
    }
