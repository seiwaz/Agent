"""A setup left non-terminal by a superseded Shadow session is not shown as active.

When a new Shadow session starts (e.g. after a spec upgrade) the previous session ends, and a
setup it had in progress stays non-terminal in the append-only store. The API shows it as
SESSION_ENDED (display only): never in the Active list or count, and without a current stage.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import text

from sp2l.api import queries
from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.persistence.market_store import MarketStore
from sp2l.persistence.shadow_recorder import PostgresShadowRecorder
from sp2l.runtime.replay import replay
from tests.conftest import T0
from tests.db.runner_utils import COSTS, FILTERS
from tests.replay.tape import tape

pytestmark = pytest.mark.db


def recorder(engine, session):  # type: ignore[no-untyped-def]
    return PostgresShadowRecorder(
        engine,
        MarketStore(engine, "BTCUSDT"),
        symbol="BTCUSDT",
        spec_version="5.11",
        spec_sha256="session-ended-test",
        session_id=session,
        started_at=T0,
    )


@pytest.fixture(scope="module")
def orphan(engine):  # type: ignore[no-untyped-def]
    old = uuid.uuid4()
    eng = ShadowSymbolEngine(
        "BTCUSDT",
        tick=D("0.1"),
        costs=COSTS,
        filters=FILTERS,
        warmup_bars=30,
        recorder=recorder(engine, old),
        session_id=str(old),
    )
    end = T0 + timedelta(minutes=300)
    replay(eng, tape(300, 3), start=T0, end=end + timedelta(minutes=1), healthy_until=end)
    eng.close()
    with engine.begin() as c:  # leave one of the old session's setups in progress
        cid, key, seq, ts = c.execute(
            text(
                "SELECT cc.id, cc.setup_key, MAX(t.seq), MAX(t.ts) FROM candidate_current cc"
                " JOIN setup_checkpoints sc ON sc.candidate_id = cc.id"
                " JOIN candidate_transitions t ON t.candidate_id = cc.id"
                " WHERE sc.session_id = :s GROUP BY cc.id, cc.setup_key, cc.created_at"
                " ORDER BY cc.created_at DESC LIMIT 1"
            ),
            {"s": old},
        ).one()
        c.execute(
            text(
                "INSERT INTO candidate_transitions (candidate_id, seq, state_from, state_to,"
                " primary_reason, reasons, ts) VALUES (:c, :q, NULL, 'E1_PREPARING', NULL,"
                " '{}', :ts)"
            ),
            {"c": cid, "q": seq + 1, "ts": ts},
        )
    return old, key


def test_in_progress_setup_of_the_open_session_is_active(engine, orphan):  # type: ignore[no-untyped-def]
    _, key = orphan
    active = queries.setup_list(engine, "BTCUSDT", "ACTIVE", 10)
    assert [r["setup_key"] for r in active] == [key]
    assert queries.setup_counts(engine, "BTCUSDT")["ACTIVE"] == 1


def test_superseded_session_setup_is_session_ended_not_active(engine, orphan):  # type: ignore[no-untyped-def]
    _, key = orphan
    recorder(engine, uuid.uuid4())  # a new session supersedes (ends) the old one
    assert queries.setup_list(engine, "BTCUSDT", "ACTIVE", 10) == []
    counts = queries.setup_counts(engine, "BTCUSDT")
    assert counts["ACTIVE"] == 0 and counts["SESSION_ENDED"] == 1
    (row,) = queries.setup_list(engine, "BTCUSDT", "SESSION_ENDED", 10)
    assert row["setup_key"] == key and row["terminal"]
    assert row["result"]["label"] == "Session ended"
    assert row["stage_label"] == "Stopped when its Shadow session was replaced"
    detail = queries.setup_detail(engine, "BTCUSDT", key)
    assert detail is not None
    assert all(f["status"] != "current" for f in detail["flow"])
    with engine.connect() as c:  # nothing stored was rewritten
        stored = c.execute(
            text("SELECT status FROM candidate_current WHERE setup_key = :k"), {"k": key}
        ).scalar_one()
    assert stored == "E1_PREPARING"


def test_other_symbols_are_never_shown(engine, orphan):  # type: ignore[no-untyped-def]
    """The API serves only the configured symbol: another market's setups do not leak in."""
    _, key = orphan
    assert queries.setup_counts(engine, "XAUTUSDT")["ALL"] == 0
    assert queries.setup_list(engine, "XAUTUSDT", None, 10) == []
    assert queries.setup_detail(engine, "XAUTUSDT", key) is None
    assert queries.counterfactuals(engine, "XAUTUSDT", 10) == []
