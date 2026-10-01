"""V5.8 acceptance on Postgres: history-repaired candles are stored with lineage and an
UNKNOWN trade count, count toward the 150-bar warmup (ready at once from the DB), are shown
by the API/WebUI with their lineage, and a restart never duplicates candles or trades."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from sp2l.api.app import create_app
from sp2l.config import RuntimeConfig
from sp2l.core.types import Candle
from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.marketdata.m1_builder import M1Result, M1Status, Quality, Trade
from sp2l.marketdata.m5_aggregator import M5Aggregator
from sp2l.persistence.market_store import MarketStore
from sp2l.runtime.shadow_service import ShadowRunner
from sp2l.strategy.risk.engine import CostModel, ExchangeFilters
from tests.db.conftest import URL

pytestmark = pytest.mark.db

NOW = datetime.now(UTC).replace(second=0, microsecond=0)
START = NOW - timedelta(minutes=5 * 170)
START -= timedelta(minutes=START.minute % 5)
HIST = (START + timedelta(minutes=5 * 100), START + timedelta(minutes=5 * 110))  # 50 min


def _m1(t: datetime) -> M1Result:
    px = D(100) + D((t.minute * 7) % 11) - D(t.minute % 7)
    if HIST[0] <= t < HIST[1]:  # a CANDLE_HISTORY_REPAIR minute: OHLCV, trade count UNKNOWN
        c = Candle(t, px, px + 2, px - 2, px + 1, D("1.5"), None)
        return M1Result(
            t,
            M1Status.OK,
            c,
            Quality.TABDEAL_HISTORY_REPAIRED,
            {"repair_type": "CANDLE_HISTORY_REPAIR"},
        )
    return M1Result(
        t, M1Status.OK, Candle(t, px, px + 2, px - 2, px + 1, D(1), 4), Quality.LIVE_RECONCILED
    )


@pytest.fixture(scope="module")
def stored(engine):
    store = MarketStore(engine, "BTCUSDT")
    agg = M5Aggregator()
    for i in range(170 * 5):
        m = _m1(START + timedelta(minutes=i))
        store.upsert_m1(m)
        m5 = agg.add(m)
        if m5 is not None:
            store.upsert_m5(m5)
    return store


def test_history_candles_are_stored_with_lineage_and_unknown_trade_count(stored, engine):
    with engine.connect() as c:
        r1 = c.execute(
            text("SELECT quality, trade_count, repair_type FROM candles_1m WHERE open_time = :t"),
            {"t": HIST[0]},
        ).one()
        r5 = c.execute(
            text(
                "SELECT quality, trade_count, history_m1_count FROM candles_5m WHERE open_time = :t"
            ),
            {"t": HIST[0]},
        ).one()
    assert tuple(r1) == ("TABDEAL_HISTORY_REPAIRED", None, "CANDLE_HISTORY_REPAIR")
    assert tuple(r5) == ("TABDEAL_HISTORY_REPAIRED", None, 5)


def test_db_with_150_trusted_m5_is_context_ready_immediately(stored, engine):
    """AT-5: a new process primes from stored contiguous M5 - history bars included."""
    with engine.begin() as x:
        x.execute(
            text(
                "INSERT INTO market_events (symbol, kind, ts, payload)"
                " VALUES ('BTCUSDT', 'M1', now(), CAST(:p AS jsonb))"
            ),
            {
                "p": json.dumps(
                    {
                        "t": (START + timedelta(minutes=5 * 170)).isoformat(),
                        "status": "OK",
                        "candle": None,
                    }
                )
            },
        )
    eng = ShadowSymbolEngine(
        "BTCUSDT",
        tick=D("0.1"),
        costs=CostModel(D("0.0008"), D("0.00095"), D("0.000198")),
        filters=ExchangeFilters(D("0.1"), D("0.001"), None, None, False),
        warmup_bars=150,
    )
    ShadowRunner._prime_new(engine, eng, "BTCUSDT")
    assert eng.m5.warm and eng.m5.price_ready and len(eng.m5.segment.bars) >= 150
    assert eng.m5.segment.anchor_open_time <= HIST[0]  # the history interval did not re-anchor
    assert eng.m5.liquidity_ready  # 60 known bars since the last history bucket


def test_repaired_candles_are_visible_in_api_and_webui_with_lineage(stored, engine):
    """AT-14."""
    app = create_app(RuntimeConfig({"database_url": URL, "symbol": "BTCUSDT"}))
    c = TestClient(app)
    items = c.get("/api/market/candles?tf=1m&limit=500").json()["items"]
    hist = [i for i in items if i.get("quality") == "TABDEAL_HISTORY_REPAIRED"]
    assert hist and all(i["trade_count"] is None for i in hist)
    assert all(i["repair_type"] == "CANDLE_HISTORY_REPAIR" for i in hist)
    m5 = c.get("/api/market/candles?tf=5m&limit=500").json()["items"]
    assert any(i.get("quality") == "TABDEAL_HISTORY_REPAIRED" for i in m5)
    ig = c.get("/api/integrity").json()
    assert {"trusted_m5", "last_break", "last_repair", "unknown_fields", "ready_reason"} <= set(
        ig["continuity"]
    )
    o = c.get("/api/overview").json()
    lev = o["system"]["leverage"]
    assert lev["strategy"] == 10 and lev["automatic_change"] is False
    js = (Path(__file__).resolve().parents[2] / "web" / "app.js").read_text()
    assert "TABDEAL_HISTORY_REPAIRED" in js and "RECENT_TRADES_REPAIRED" in js


def test_restart_rewrites_create_no_duplicate_candles_or_trades(stored, engine):
    """AT-15: a restarted collector re-emitting the same minutes/trades changes nothing."""
    again = MarketStore(engine, "BTCUSDT")  # a new process: no in-memory id cache
    for i in range(5):
        again.upsert_m1(_m1(HIST[0] + timedelta(minutes=i)))
    t = Trade("x1", NOW, NOW, D("100"), D("1"), source="WS")
    again.insert_trade(t, False)
    again.insert_trade(t, False)
    with engine.connect() as c:
        dup = c.execute(
            text(
                "SELECT COUNT(*) FROM (SELECT open_time FROM candles_1m WHERE symbol = 'BTCUSDT'"
                " GROUP BY 1 HAVING COUNT(*) > 1) d"
            )
        ).scalar_one()
        n = c.execute(text("SELECT COUNT(*) FROM raw_trades WHERE trade_id = 'x1'")).scalar_one()
    assert dup == 0 and n == 1


def test_leverage_mismatch_from_recorded_evidence_blocks_live_in_the_api(stored, engine):
    """AT-11 (display): 61x recorded by the read-only check -> Live blocked, exact text."""
    with engine.begin() as x:
        x.execute(
            text(
                "INSERT INTO runtime_validation_runs (item, started_at, finished_at, passed,"
                " api_version, evidence, notes, approved_by) VALUES ('CROSS_10X', now(), now(),"
                " false, 'test', CAST(:e AS jsonb), 'leverage=61', 'test')"
            ),
            {
                "e": json.dumps(
                    {
                        # the read-only check's shape: the request names the market it read
                        "leverage": {"request": {"params": {"symbol": "BTC_USDT"}}},
                        "exchange_leverage": 61,
                        "strategy_leverage": 10,
                    }
                )
            },
        )
    c = TestClient(create_app(RuntimeConfig({"database_url": URL, "symbol": "BTCUSDT"})))
    live = c.get("/api/overview").json()["system"]["live"]
    assert live["leverage"]["live_blocker"] == "LEVERAGE_MISMATCH"
    assert live["leverage"]["text"] == (
        "Strategy leverage: 10x / Exchange leverage: 61x / Live: blocked — exchange leverage"
        " must be 10x"
    )
    assert live["code"] == "LIVE_AUTOMATION_DISABLED"


def test_backfill_fills_a_stored_hole_from_validated_history_and_joins_the_m5_series(engine):
    """V5.8: a hole already in the DB (a pre-V5.8 UNRECOVERED gap) is filled only through
    validated chart history, audited, and the M5 series becomes one segment again."""
    from sp2l.runtime.reconcile import backfill_history

    store = MarketStore(engine, "ETHUSDT")
    base = NOW - timedelta(days=3)
    base -= timedelta(minutes=base.minute % 5)
    hole = {base + timedelta(minutes=42), base + timedelta(minutes=43)}
    series = {}
    for i in range(90):
        t = base + timedelta(minutes=i)
        px = D(2000) + D(i % 9)
        series[t] = Candle(t, px, px + 1, px - 1, px + D("0.5"), D(2), 5)
    agg = M5Aggregator()
    for t, cd in series.items():
        m = (
            M1Result(t, M1Status.DATA_GAP, None)
            if t in hole
            else M1Result(t, M1Status.OK, cd, Quality.LIVE_RECONCILED)
        )
        store.upsert_m1(m)
        r = agg.add(m)
        if r is not None:
            store.upsert_m5(r)

    def chart(a, b, tamper=False):
        out, prev = [], None
        for t, cd in series.items():
            o, h, lo = (
                (cd.open, cd.high, cd.low)
                if prev is None
                else (prev, max(cd.high, prev), min(cd.low, prev))
            )
            prev = cd.close
            if a <= t <= b:
                out.append(
                    {
                        "time": int(t.timestamp()),
                        "open": str(o),
                        "high": str(h + (5 if tamper else 0)),
                        "low": str(lo),
                        "close": str(cd.close),
                        "volume": str(cd.volume),
                    }
                )
        return out

    bad = backfill_history(engine, "ETHUSDT", base, lambda a, b: chart(a, b, True), apply=True)
    assert bad["failed"] and bad["failed"][0]["reason"] == "HISTORY_VALIDATION_FAILED"
    ok = backfill_history(engine, "ETHUSDT", base, chart, apply=True)
    assert len(ok["repaired"]) == 1 and ok["repaired"][0]["minutes"] == 2
    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT quality, trade_count, repair_type FROM candles_1m WHERE symbol = 'ETHUSDT'"
                " AND open_time = ANY(:t)"
            ),
            {"t": sorted(hole)},
        ).all()
        m5 = c.execute(
            text(
                "SELECT quality, history_m1_count FROM candles_5m WHERE symbol = 'ETHUSDT'"
                " AND open_time = :t"
            ),
            {"t": base + timedelta(minutes=40)},
        ).one()
        rep = c.execute(
            text("SELECT status, method FROM gap_repairs WHERE symbol = 'ETHUSDT'")
        ).all()
    assert {tuple(r) for r in rows} == {("TABDEAL_HISTORY_REPAIRED", None, "CANDLE_HISTORY_REPAIR")}
    assert tuple(m5) == ("TABDEAL_HISTORY_REPAIRED", 2)
    assert [tuple(r) for r in rep] == [("REPAIRED", "TABDEAL_HISTORY")]
    again = backfill_history(engine, "ETHUSDT", base, chart, apply=True)
    assert again["holes"] == 0  # idempotent
