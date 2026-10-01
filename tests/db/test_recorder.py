"""Persistence round trip: a Shadow replay recorded to Postgres matches the engine."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from sp2l.engine.model import SetupState
from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.persistence.market_store import MarketStore
from sp2l.persistence.shadow_recorder import PostgresShadowRecorder
from sp2l.runtime.replay import replay
from sp2l.strategy.risk.engine import CostModel, ExchangeFilters
from tests.conftest import T0
from tests.replay.tape import tape

pytestmark = pytest.mark.db
MINUTES = 600


@pytest.fixture(scope="module")
def recorded(engine):
    store = MarketStore(engine, "BTCUSDT")
    session = uuid.uuid4()
    rec = PostgresShadowRecorder(
        engine,
        store,
        symbol="BTCUSDT",
        spec_version="5.4",
        spec_sha256="test-sha",
        session_id=session,
        started_at=T0,
    )
    eng = ShadowSymbolEngine(
        "BTCUSDT",
        tick=D("0.1"),
        costs=CostModel(D("0.0004"), D("0.0006"), D("0.0005")),
        filters=ExchangeFilters(D("0.1"), D("0.001"), None, None, verified=False),
        warmup_bars=30,
        recorder=rec,
        session_id=str(session),
    )
    end = T0 + timedelta(minutes=MINUTES)
    replay(eng, tape(MINUTES, 3), start=T0, end=end + timedelta(minutes=1), healthy_until=end)
    eng.close()
    return eng, session


def q(engine, sql, **p):
    with engine.connect() as c:
        return c.execute(text(sql), p).all()


def test_every_pgap_and_candidate_persisted(engine, recorded):
    eng, _ = recorded
    assert q(engine, "SELECT count(*) FROM pgaps")[0][0] == len(eng.pgaps)
    promoted = sum(1 for p in eng.pgaps if p.promoted)
    assert q(engine, "SELECT count(*) FROM candidates")[0][0] == promoted
    assert promoted == len(eng.finished) + (1 if eng.active else 0)


def test_final_status_and_reasons_match_engine(engine, recorded):
    eng, _ = recorded
    rows = {
        r[0]: (r[1], r[2])
        for r in q(engine, "SELECT setup_key, status, primary_reason FROM candidate_current")
    }
    for m in eng.finished:
        assert rows[m.cfg.setup_id] == (m.state.value, m.primary_reason)


def test_snapshots_one_per_evaluation_with_exact_values(engine, recorded):
    eng, _ = recorded
    n_eval = sum(len(m.evaluations) for m in eng.finished)
    assert q(engine, "SELECT count(*) FROM context_snapshots")[0][0] == n_eval
    assert q(engine, "SELECT count(*) FROM exhaustion_snapshots")[0][0] == n_eval
    row = q(
        engine,
        "SELECT exact->>'range_position_e1', range_position_e1 FROM context_snapshots"
        " WHERE exact->>'range_position_e1' IS NOT NULL LIMIT 1",
    )
    assert row and "/" in row[0][0] or row[0][0].lstrip("-").isdigit()


def test_revisions_orders_and_ledger(engine, recorded):
    eng, session = recorded
    armed = sum(len(m.revisions) for m in eng.finished)
    assert q(engine, "SELECT count(*) FROM e1_revisions")[0][0] == armed
    subs = sum(len(m.submissions) for m in eng.finished)
    assert q(engine, "SELECT count(*) FROM orders")[0][0] == subs
    closed = [m for m in eng.finished if m.state is SetupState.CLOSED]
    assert q(engine, "SELECT count(*) FROM strategy_events WHERE event_type = 'EXIT'")[0][0] == sum(
        len(m.exits) for m in closed
    )
    last = q(
        engine,
        "SELECT balance_after FROM shadow_wallet_ledger WHERE session_id = :s"
        " ORDER BY id DESC LIMIT 1",
        s=session,
    )[0][0]
    assert abs(last - eng.wallet_balance()) < D("1e-12")


def test_counterfactuals_persisted_and_labelled(engine, recorded):
    eng, _ = recorded
    rows = q(engine, "SELECT label, outcome FROM cf.counterfactual_outcomes")
    # recorded once terminal; runs still open at shutdown stay in the checkpoint (B39)
    assert len(rows) == sum(1 for c in eng.counterfactuals if c.done)
    assert all(r[0] == "COUNTERFACTUAL" for r in rows)


def test_candles_carry_quality_fields_and_records_are_append_only(engine, recorded):
    assert q(engine, "SELECT count(*) FROM candles_5m WHERE real_trade_count IS NULL")[0][0] == 0
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as c:
        c.execute(text("DELETE FROM context_snapshots"))
