"""B39: a DATA_GAP overlapping live exposure finalizes the setup as AMBIGUOUS_DATA_GAP.
No close or PnL is fabricated, it is excluded from confirmed performance, and the symbol
returns to normal warmup/scanning."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from sp2l.api.app import create_app
from sp2l.engine.model import SetupState
from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.runtime.replay import replay
from tests.conftest import T0
from tests.db.conftest import URL
from tests.db.journal import write_journal
from tests.db.runner_utils import COSTS, FILTERS, cfg, drain, open_runner, step_until
from tests.replay.tape import tape

pytestmark = pytest.mark.db
MINUTES = 900
END = T0 + timedelta(minutes=MINUTES)


def position_window():
    """A CLOSED setup from an uninterrupted in-memory run: (fill_ts, exit_ts, e1)."""
    eng = ShadowSymbolEngine("BTCUSDT", tick=D("0.1"), costs=COSTS, filters=FILTERS, warmup_bars=30)
    replay(eng, tape(MINUTES, 3), start=T0, end=END + timedelta(minutes=1), healthy_until=END)
    for m in eng.finished:
        if m.state is SetupState.CLOSED and m.fills and m.exits:
            fill, ex = m.fills[0].ts, m.exits[-1].ts
            if ex - fill > timedelta(seconds=20) and fill < END - timedelta(minutes=300):
                return fill, ex, m.levels.e1
    raise AssertionError("no suitable closed trade in the tape")


def q(engine, sql, **p):
    with engine.connect() as c:
        return c.execute(text(sql), p).all()


@pytest.fixture(scope="module")
def scenario(engine):
    fill, ex, e1 = position_window()
    window = (
        fill + timedelta(seconds=5),
        min(fill + timedelta(minutes=3), ex - timedelta(seconds=2)),
    )
    write_journal(
        engine,
        "BTCUSDT",
        tape(MINUTES, 3),
        start=T0,
        end=END + timedelta(minutes=1),
        window=window,
        mode="disconnect",
    )
    write_journal(
        engine,
        "ETHUSDT",
        tape(MINUTES, 3),
        start=T0,
        end=END + timedelta(minutes=1),
        window=window,
        mode="downtime",
    )
    return engine, window, e1


def ambiguous_setup(engine, session):
    return q(
        engine,
        """
        SELECT cc.id, cc.primary_reason, sc.checkpoint->'levels'->>'e1'
        FROM candidate_current cc JOIN setup_checkpoints sc ON sc.candidate_id = cc.id
        WHERE sc.session_id = :s AND cc.status = 'AMBIGUOUS_DATA_GAP'""",
        s=session,
    )


def test_disconnect_gap_while_position_active(scenario):
    db, window, e1 = scenario
    r = open_runner(db, URL, "BTCUSDT", fresh=True)
    drain(r)
    session = r.engine.session_id
    amb = ambiguous_setup(db, session)
    assert len(amb) == 1 and amb[0][1] == "DATA_GAP_WHILE_ACTIVE" and amb[0][2] == str(e1)
    cid = amb[0][0]
    # nothing fabricated: no EXIT, no realized PnL for the ambiguous setup
    assert (
        q(
            db,
            "SELECT count(*) FROM strategy_events WHERE candidate_id = :c AND event_type = 'EXIT'",
            c=cid,
        )[0][0]
        == 0
    )
    assert (
        q(
            db,
            "SELECT count(*) FROM shadow_wallet_ledger WHERE candidate_id = :c AND"
            " kind = 'REALIZED_PNL'",
            c=cid,
        )[0][0]
        == 0
    )
    ev = q(
        db,
        "SELECT payload FROM strategy_events WHERE candidate_id = :c AND"
        " event_type = 'DATA_GAP_WHILE_ACTIVE'",
        c=cid,
    )[0][0]
    assert ev["reason"] == "DISCONNECTED" and D(ev["discarded_position"]) != 0
    # the ambiguous setup's position was discarded; any position left at the end of the tape
    # belongs to a later, still-active setup (V6.0 lets more setups through)
    active = r.engine.active
    left = active.exposure()[1] if active is not None else D(0)
    assert active is None or active.candidate_id != str(cid)
    assert r.engine.broker.position_qty() == left
    # excluded from confirmed performance
    api = TestClient(create_app(cfg(URL, "BTCUSDT")))
    perf = api.get("/api/performance").json()["confirmed"]
    assert perf["ambiguous_excluded"] == 1
    closed_ids = {
        x[0]
        for x in q(
            db,
            "SELECT cc.id FROM candidate_current cc JOIN"
            " setup_checkpoints sc ON sc.candidate_id = cc.id WHERE"
            " sc.session_id = :s AND cc.status = 'CLOSED'",
            s=session,
        )
    }
    assert cid not in closed_ids and perf["closed"] == len(closed_ids)
    # back to normal warmup, then scanning resumes after the gap
    after = [p for p in r.engine.pgaps if p.ts > window[1]]
    assert any(p.reason == "DATA_WARMUP" for p in after)
    assert any(p.promoted for p in after if p.ts > window[1] + timedelta(minutes=150))


def test_collector_downtime_and_shadow_crash_while_position_active(scenario):
    db, window, e1 = scenario
    r = open_runner(db, URL, "ETHUSDT", fresh=True)
    session = r.engine.session_id
    assert step_until(  # the position that is open when the collector goes down
        r,
        lambda e: (
            e.active is not None
            and e.broker.position_qty() != 0
            and e.active.levels is not None
            and e.active.levels.e1 == e1
        ),
    )
    r = open_runner(db, URL, "ETHUSDT", fresh=False)  # Shadow crash + restart: no auto-close
    assert r.engine.broker.position_qty() != 0
    drain(r)
    amb = ambiguous_setup(db, session)
    assert len(amb) == 1 and amb[0][2] == str(e1)
    ev = q(
        db,
        "SELECT payload FROM strategy_events WHERE candidate_id = :c AND"
        " event_type = 'DATA_GAP_WHILE_ACTIVE'",
        c=amb[0][0],
    )[0][0]
    assert ev["reason"] == "COLLECTOR_START"
    assert (
        q(
            db,
            "SELECT count(*) FROM shadow_wallet_ledger WHERE candidate_id = :c AND"
            " kind = 'REALIZED_PNL'",
            c=amb[0][0],
        )[0][0]
        == 0
    )
