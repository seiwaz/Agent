"""System B paper service on the database: daily bars from stored 1-minute history, the journal
(first record per day kept), and the /api/trend endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from sp2l.trend import live as tl
from sp2l.trend.model import TrendParams

pytestmark = pytest.mark.db
T0 = datetime(2026, 3, 1, tzinfo=UTC)
P = TrendParams(entry_len=5, exit_len=3, atr_len=3, risk_pct=0.05)
ROWS = [(100, 101, 99, 100)] * 12 + [(100, 106, 99.5, 105)]  # breakout on the last close


def store_days(engine, rows):
    """Each day as four 1-minute bars: open at 00:00, high at 06:00, low at 12:00, close at 23:59."""
    with engine.begin() as c:
        c.execute(text("DELETE FROM exchange_m1 WHERE symbol = 'BTCUSDT'"))
        c.execute(text("DELETE FROM trend_journal"))
        for d, (o, h, lo, cl) in enumerate(rows):
            day = T0 + timedelta(days=d)
            for minute, (bo, bh, bl, bc) in (
                (0, (o, o, o, o)), (360, (o, h, o, h)), (720, (h, h, lo, lo)), (1439, (lo, max(lo, cl), lo, cl)),
            ):
                c.execute(
                    text("INSERT INTO exchange_m1 (symbol, open_time, open, high, low, close, volume)"
                         " VALUES ('BTCUSDT', :t, :o, :h, :l, :c, 1)"),
                    {"t": day + timedelta(minutes=minute), "o": bo, "h": max(bo, bh, bc), "l": min(bo, bl, bc), "c": bc},
                )


def test_state_from_stored_history_and_journal(engine):
    store_days(engine, ROWS)
    cfg = tl.LiveConfig("BTCUSDT", (T0 + timedelta(days=5)).date(), 100.0, 0.001, 0.0003)
    s = tl.current(engine, P, cfg)
    assert s["ready"] and s["as_of"] == (T0 + timedelta(days=12)).date().isoformat()
    assert s["action"] == "BUY" and s["entry_level"] == 106 and s["close"] == 105
    assert tl.record(engine, s) is True
    assert tl.record(engine, {**s, "action": "WAIT"}) is False  # the first record of a day stays
    j = tl.journal(engine, "BTCUSDT")
    assert len(j) == 1 and j[0]["action"] == "BUY" and j[0]["details"]["next"]["action"] == "BUY"


def test_api_trend_endpoint(engine, tmp_path: Path):
    from fastapi.testclient import TestClient

    from sp2l.api.app import create_app
    from sp2l.config import RuntimeConfig
    from tests.db.conftest import URL

    store_days(engine, ROWS)
    src = Path("config/runtime.yaml").read_text()
    src = src.replace("database_url: postgresql+psycopg:///sp2l_dev?host=/tmp", f"database_url: {URL}")
    src = src.replace('paper_start: "2026-10-08"', f'paper_start: "{(T0 + timedelta(days=5)).date()}"')
    src = src.replace("  entry_len: 20 ", "  entry_len: 5 ").replace("  exit_len: 10 ", "  exit_len: 3 ").replace("  atr_len: 20\n", "  atr_len: 3\n")
    f = tmp_path / "rt.yaml"
    f.write_text(src)
    client = TestClient(create_app(RuntimeConfig.load(f)))
    r = client.get("/api/trend").json()
    assert r["ready"] and r["action"] == "BUY" and r["params"]["risk_pct"] == 0.05
    assert r["journal"] == []
    assert client.get("/").status_code == 200
