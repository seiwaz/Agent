"""V5.9 live chart transport and P-Gap quality persistence (Postgres)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from sp2l.api.app import create_app
from sp2l.api.live import LiveHub, psycopg_dsn, sse
from sp2l.config import RuntimeConfig
from sp2l.core.types import Candle
from sp2l.marketdata.m1_builder import M1Result, M1Status, Quality, Trade
from sp2l.persistence.market_store import MarketStore
from tests.db.conftest import URL

pytestmark = pytest.mark.db
SYM = "LIVEUSDT"


def test_notify_reaches_sse_clients_quickly(engine):
    """Collector NOTIFY -> API LISTEN -> SSE: the forming candle arrives in milliseconds."""
    store = MarketStore(engine, SYM)

    async def run() -> tuple[list[str], float]:
        hub = LiveHub(psycopg_dsn(URL), SYM)

        async def never() -> bool:
            return False

        gen = sse(hub, lambda: {"type": "snapshot", "forming": []}, never)
        assert (await gen.__anext__()).startswith("retry:")
        snap = await gen.__anext__()
        for _ in range(100):  # the LISTEN connection is up
            if hub.connected:
                break
            await asyncio.sleep(0.05)
        t0 = time.perf_counter()
        await asyncio.to_thread(
            store.notify_live, {"type": "trade", "t": "2026-01-01T00:00:00+00:00", "c": "1"}
        )
        got = await asyncio.wait_for(gen.__anext__(), 5)
        dt = time.perf_counter() - t0
        await gen.aclose()
        return [snap, got], dt

    (snap, got), dt = asyncio.run(run())
    assert snap.startswith("event: snapshot")
    ev = json.loads(got.removeprefix("data: ").strip())
    assert ev["type"] == "trade" and ev["symbol"] == SYM and ev["c"] == "1"
    assert dt < 0.5


def test_other_symbols_are_not_forwarded():
    hub = LiveHub("postgresql:///none", SYM)
    q: asyncio.Queue[str] = asyncio.Queue()
    hub.subs.add(q)
    hub.publish(json.dumps({"symbol": SYM, "x": 1}))
    assert q.qsize() == 1


def test_rest_revision_republishes_the_just_closed_candle(engine):
    """8. REST reconciliation corrects an already-final minute: the chart gets the revision."""
    store = MarketStore(engine, SYM)
    t = datetime(2026, 2, 1, 10, 0, tzinfo=UTC)
    store.upsert_m1(
        M1Result(
            t,
            M1Status.OK,
            Candle(t, D(100), D(101), D(99), D(100), D(1), 1),
            Quality.LIVE_WS_ONLY,
        )
    )
    store.insert_trade(Trade("w1", t + timedelta(seconds=5), t, D(100), D(1)), False)
    store.insert_trade(
        Trade("r1", t + timedelta(seconds=30), t, D(104), D("0.5"), source="REST"), True
    )
    sent: list[dict[str, object]] = []
    store.notify_live = lambda e: sent.append(e)  # type: ignore[method-assign]
    from sp2l.runtime.sinks import MarketStoreSink

    MarketStoreSink(store).on_late_rest_trade(
        Trade("r1", t + timedelta(seconds=30), t, D(104), D("0.5"), source="REST")
    )
    assert len(sent) == 1 and sent[0]["type"] == "revised" and sent[0]["t"] == t.isoformat()
    assert sent[0]["h"] == "104.000000000000000000" and sent[0]["revision"] == 1


def test_snapshot_restores_the_forming_candle_after_reconnect(engine):
    """9. A (re)connecting browser gets the current forming candle from stored trades."""
    store = MarketStore(engine, SYM)
    last = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
    store.upsert_m1(
        M1Result(
            last,
            M1Status.OK,
            Candle(last, D(100), D(100), D(100), D(100), D(1), 1),
            Quality.LIVE_RECONCILED,
        )
    )
    m = last + timedelta(minutes=1)
    for i, px in enumerate(["101", "103", "99", "102"]):
        store.insert_trade(Trade(f"s{i}", m + timedelta(seconds=i + 1), m, D(px), D("0.25")), False)
    c = TestClient(create_app(RuntimeConfig({"database_url": URL, "symbol": SYM})))
    snap = c.get("/api/live/snapshot").json()
    (f,) = snap["forming"]
    assert f["t"] == m.isoformat()
    assert (f["o"], f["h"], f["l"], f["c"], f["v"], f["n"]) == ("101", "103", "99", "102", "1", 4)
    assert snap["price"] == "102"


def test_pgap_quality_is_persisted_and_old_rows_are_not_reinterpreted(engine):
    from sp2l.engine.symbol_engine import PGapLog
    from sp2l.persistence.shadow_recorder import PostgresShadowRecorder

    store = MarketStore(engine, "PGAPUSDT")
    t0 = datetime(2026, 3, 1, tzinfo=UTC)
    for i in range(3):
        t = t0 + timedelta(minutes=i)
        store.upsert_m1(
            M1Result(
                t, M1Status.OK, Candle(t, D(1), D(2), D(1), D(2), D(1), 1), Quality.LIVE_RECONCILED
            )
        )
    rec = PostgresShadowRecorder(
        engine,
        store,
        symbol="PGAPUSDT",
        spec_version="5.9",
        spec_sha256="t",
        session_id=uuid.uuid4(),
        started_at=t0,
    )
    q = {
        "impulse_body": "0.4",
        "impulse_range": "1",
        "impulse_upper_shadow": "0.3",
        "impulse_lower_shadow": "0.3",
        "impulse_body_ratio": "0.4",
        "impulse_direction": "BULLISH",
        "gap_size": "0.5",
        "gap_body_ratio": "1.25",
        "gap_size_ticks": "5",
        "strong_impulse_pass": False,
        "strong_gap_pass": True,
        "final_pgap_pass": False,
        "failure_reasons": ["PGAP_IMPULSE_BODY_TOO_WEAK"],
        "thresholds": {"body_ratio_min": "0.60", "gap_body_ratio_min": "0.15", "gap_ticks_min": 2},
    }
    rec.on_pgap(
        PGapLog(
            t0 + timedelta(minutes=3),
            "LONG",
            t0,
            t0 + timedelta(minutes=1),
            t0 + timedelta(minutes=2),
            False,
            "PGAP_IMPULSE_BODY_TOO_WEAK",
            q,
        )
    )
    with engine.begin() as c:  # a row recorded before V5.9 (no quality)
        ids = (
            c.execute(
                text("SELECT id FROM candles_1m WHERE symbol = 'PGAPUSDT' ORDER BY open_time")
            )
            .scalars()
            .all()
        )
        c.execute(
            text(
                "INSERT INTO pgaps (symbol, side, left_candle_id, middle_candle_id,"
                " right_candle_id, confirmed_at, promoted) VALUES ('PGAPUSDT', 'LONG', :a, :b,"
                " :c, :t, true)"
            ),
            {"a": ids[0], "b": ids[1], "c": ids[2], "t": t0},
        )
        row = c.execute(
            text(
                "SELECT impulse_body_ratio, final_pgap_pass, failure_reasons, spec_version"
                " FROM pgaps WHERE symbol = 'PGAPUSDT' AND quality IS NOT NULL"
            )
        ).one()
    assert tuple(row) == (D("0.4"), False, ["PGAP_IMPULSE_BODY_TOO_WEAK"], "5.9")
    app = create_app(RuntimeConfig({"database_url": URL, "symbol": "PGAPUSDT"}))
    items = TestClient(app).get("/api/pgaps").json()["items"]
    new = next(i for i in items if i["view"]["measured"])
    assert new["view"]["result"] == "Rejected — impulse candle is dominated by shadows"
    assert (
        new["view"]["rows"][1]["value"] == "40%" and new["view"]["rows"][1]["required"] == "≥ 60%"
    )
    assert new["outcome"]["code"] == "PGAP_IMPULSE_BODY_TOO_WEAK"
    old = next(i for i in items if not i["view"]["measured"])
    assert old["view"]["result"] == "Not measured"  # never re-evaluated under V5.9


def test_room_probe_records_are_append_only(engine):
    from sqlalchemy.exc import DBAPIError

    from sp2l.persistence.shadow_recorder import PostgresShadowRecorder

    store = MarketStore(engine, "PROBEUSDT")
    rec = PostgresShadowRecorder(
        engine,
        store,
        symbol="PROBEUSDT",
        spec_version="5.9",
        spec_sha256="t",
        session_id=uuid.uuid4(),
        started_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    rec.on_probe(
        {
            "probe_version": "room-b1",
            "candidate_key": "PROBEUSDT-x-1",
            "created_at": "2026-03-01T00:05:00+00:00",
            "result": "NO_CLOSE_THROUGH_OBSTACLE",
            "blocking_swing": "100.5",
        }
    )
    with engine.connect() as c:
        row = c.execute(
            text("SELECT kind, result, record->>'blocking_swing' FROM analysis_probes")
        ).one()
    assert tuple(row) == ("ROOM_TO_TP_BREAKOUT", "NO_CLOSE_THROUGH_OBSTACLE", "100.5")
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text("DELETE FROM analysis_probes"))
