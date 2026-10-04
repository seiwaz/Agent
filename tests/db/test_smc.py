"""SMC on the database: history cache, merged series, runner lifecycle and the API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import text

from sp2l.core.types import Side
from sp2l.smc import store
from sp2l.smc.history import ensure_history, load_bars, series_end
from sp2l.smc.lifecycle import Tracked, risk_unit
from sp2l.smc.model import Costs, Setup, SmcParams, Zone
from sp2l.smc.runner import SmcRunner

pytestmark = pytest.mark.db
SYM = "XAUTUSDT"
NOW = datetime(2026, 3, 2, 12, 0, 20, tzinfo=UTC)
MIN = timedelta(minutes=1)


def chart(a: datetime, b: datetime) -> list[dict]:
    """Tabdeal-like chart bars for [a, b), including the still-forming minute."""
    out, t = [], a.replace(second=0, microsecond=0)
    while t < b:
        i = int(t.timestamp() // 60)
        px = 4000 + (i % 37) - (i % 11) * 0.5
        out.append(
            {
                "time": int(t.timestamp()),
                "open": px,
                "high": px + 1,
                "low": px - 1,
                "close": px + 0.5,
                "volume": 0.2,
            }
        )
        t += MIN
    return out


@pytest.fixture()
def clean(engine):
    with engine.begin() as c:
        c.execute(text("TRUNCATE exchange_m1, smc_runner_state, smc_signal_events, smc_signals"))
    yield engine


def test_history_fills_head_and_tail_and_never_caches_the_forming_minute(clean):
    calls = []

    def fetch(a, b):
        calls.append((a, b))
        return chart(a, min(b, NOW))

    out = ensure_history(clean, SYM, fetch, days=1, now=NOW)
    first, last = datetime.fromisoformat(out["first"]), datetime.fromisoformat(out["last"])
    assert first == (NOW - timedelta(days=1)).replace(second=0)
    assert last == NOW.replace(second=0) - MIN  # 12:00 is still forming at 12:00:20
    assert len(calls) == 2  # head, then tail with overlap
    ensure_history(clean, SYM, fetch, days=1, now=NOW + timedelta(minutes=3))
    assert len(calls) == 3 and calls[-1][0] == last - timedelta(minutes=15)


def test_live_canonical_minutes_take_precedence_and_timeframes_aggregate(clean):
    ensure_history(clean, SYM, lambda a, b: chart(a, min(b, NOW)), days=1, now=NOW)
    t = NOW.replace(second=0) - 3 * MIN
    with clean.begin() as c:
        c.execute(
            text(
                "INSERT INTO candles_1m (symbol, open_time, close_time, open, high, low, close, volume,"
                " trade_count, finalized_at, source_hash) VALUES (:s, :t, :e, 1, 9999, 1, 5000, 1, 1, now(), 'x')"
                " ON CONFLICT DO NOTHING"
            ),
            {"s": SYM, "t": t, "e": t + MIN},
        )
    upto = series_end(clean, SYM)
    m1 = {b.open_time: b for b in load_bars(clean, SYM, "1m", 10, upto)}
    assert m1[t].high == D(9999)  # canonical wins over the chart bar
    h1 = load_bars(clean, SYM, "1h", 5, upto)
    assert all(b.open_time.minute == 30 for b in h1)  # Tabdeal's 1h grid
    assert h1[-1].open_time + timedelta(hours=1) <= upto
    forming = load_bars(clean, SYM, "1h", 5, upto, include_forming=True)
    assert len(forming) == len(h1) + 1 and forming[-1].high == D(9999)


def test_runner_steps_tracks_an_open_signal_to_its_stop_and_heartbeats(clean):
    ensure_history(clean, SYM, lambda a, b: chart(a, min(b, NOW)), days=2, now=NOW)
    p = SmcParams(history_days=2, lookback_4h=10, lookback_1h=40)
    costs = Costs()
    created = NOW.replace(second=0) - timedelta(minutes=30)
    zone = Zone("15m:OB:LONG:1", "OB", Side.LONG, D(4010), D(4000), 0, created, 0)
    s = Setup(
        key="test|1",
        direction=Side.LONG,
        accepted=True,
        reasons=(),
        created_at=created,
        trigger_kind="CHOCH",
        trigger_event_id="1m:CHOCH:LONG:1",
        bias=1,
        poi=zone,
        poi_tf="15m",
        entry_zone=zone,
        entry=D(4100),
        sl=D(4099),
        tp=D(4200),
        rr=D(100),
        net_rr=D(100),
        score=3,
        factors={"sweep": True},
        qty=D(1),
        notional=D(4100),
        leverage=D(41),
    )
    t = Tracked(
        "test|1",
        Side.LONG,
        D(4100),
        D(4099),
        D(4200),
        created,
        risk_unit(Side.LONG, D(4100), D(4099), costs),
        market=True,
    )
    with clean.begin() as c:
        sid = store.insert_signal(c, SYM, s, t, p.digest())
    r = SmcRunner(clean, SYM, p, costs, fetch=None)
    assert r.step(NOW)
    with clean.connect() as c:
        st, res = c.execute(
            text("SELECT state, result_r FROM smc_signals WHERE id = :i"), {"i": sid}
        ).one()
        kinds = [
            k
            for (k,) in c.execute(
                text("SELECT kind FROM smc_signal_events WHERE signal_id = :i ORDER BY id"),
                {"i": sid},
            )
        ]
        hb = c.execute(
            text("SELECT status FROM smc_runner_state WHERE symbol = :s"), {"s": SYM}
        ).scalar_one()
    assert (
        st == "SL" and res == D(-1) and kinds == ["CREATED", "SL"]
    )  # chart prices sit far below 4099
    assert {x["tf"] for x in hb["timeframes"]} == {"1m", "15m", "1h", "4h"}
    assert not r.step(NOW)  # no new minute: nothing to do


def test_api_serves_every_smc_view(clean, tmp_path):
    from fastapi.testclient import TestClient

    from sp2l.api.app import create_app
    from sp2l.config import RuntimeConfig
    from tests.db.conftest import URL

    ensure_history(clean, SYM, lambda a, b: chart(a, min(b, NOW)), days=2, now=NOW)
    cfg = RuntimeConfig(
        {
            "database_url": URL,
            "symbol": SYM,
            "costs": {
                "maker_fee": "0.0008",
                "taker_fee": "0.00095",
                "slippage_allowance": "0.0003",
            },
            "smc": {"history_days": 2, "lookback_4h": 10, "lookback_1h": 40},
        }
    )
    c = TestClient(create_app(cfg))
    for path in (
        "/api/overview",
        "/api/smc/params",
        "/api/smc/radar",
        "/api/smc/signals?state=closed",
        "/api/smc/performance",
        "/api/smc/backtest?days=1",
        "/api/market/candles?tf=4h&limit=5",
        "/api/smc/analysis?tf=1m&bars=100",
        "/api/smc/analysis?tf=15m",
    ):
        r = c.get(path)
        assert r.status_code == 200, (path, r.text[:300])
    a = c.get("/api/smc/analysis?tf=5m&bars=200").json()
    assert a["ready"] and {"zones", "htf_zones", "events", "liquidity", "range"} <= set(a)
    assert c.post("/api/smc/signals").status_code == 405  # read-only
    assert c.get("/api/smc/analysis?tf=2m").status_code == 422
