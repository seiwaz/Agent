"""SMC on the database: history cache, merged series, runner lifecycle and the API."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import text

from sp2l.core.types import Side
from sp2l.smc import store
from sp2l.smc.backtest import run as backtest
from sp2l.smc.history import ensure_history, load_bars, series_end
from sp2l.smc.lifecycle import Tracked, risk_unit
from sp2l.smc.model import Costs, Setup, SmcParams, StructureEvent, Sweep, Target, Zone, ZoneSetup
from sp2l.smc.runner import SmcRunner
from sp2l.smc.wallet import Wallet
from tests.smc.fixtures import SETUP, T0, expand
from tests.smc.fixtures import P as FIXTURE_P

pytestmark = pytest.mark.db
SYM = "XAUTUSDT"
OTHER = "ETHUSDT"
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


def make_setup(key: str, created: datetime, entry: D, sl: D, tp: D) -> Setup:
    """A stored-signal stand-in: the zone setup is only carried along for the record."""
    ob = Zone(f"1h:OB:LONG:{key}", "OB", Side.LONG, D(4010), D(4000), 0, created, 0)
    ev = StructureEvent(f"1h:BOS:LONG:{key}", "BOS", Side.LONG, D(4020), 0, created, 1, created)
    zs = ZoneSetup(
        ob.id,
        "1h",
        Side.LONG,
        ob,
        (D(4010), D(4012)),
        Sweep(0, Side.LONG, D(4001), D(3999), created),
        ev,
        created,
    )
    tgt = Target(tp, "test", D(2))
    return Setup(
        key=key,
        direction=Side.LONG,
        accepted=True,
        reasons=(),
        created_at=created,
        zone=zs,
        bias=1,
        entry=entry,
        sl=sl,
        tp=tgt,
        qty=D(1),
        notional=entry,
        leverage=D(41),
    )


@pytest.fixture()
def clean(engine):
    with engine.begin() as c:
        c.execute(
            text(
                "TRUNCATE exchange_m1, smc_runner_state, smc_signal_events, smc_wallet_ledger,"
                " smc_signals, smc_wallets"
            )
        )
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
    # the tail overlaps 15 minutes, and every request starts LEAD (5 min) earlier
    assert len(calls) == 3 and calls[-1][0] == last - timedelta(minutes=15) - timedelta(minutes=5)


def test_the_first_bar_of_a_request_is_never_stored(clean):
    """Tabdeal reports another open for the first bar of a request: requests start earlier
    and those lead bars are dropped, so a chunk boundary stores the true bar."""

    def fetch(a, b):
        bars = chart(a, min(b, NOW))
        bars[0] = {**bars[0], "open": 1.0, "low": 1.0}  # the artifact
        return bars

    ensure_history(clean, SYM, fetch, days=3, now=NOW)  # two-day chunks: a boundary inside
    with clean.connect() as c:
        lows = c.execute(
            text("SELECT MIN(low) FROM exchange_m1 WHERE symbol = :s"), {"s": SYM}
        ).scalar_one()
    assert lows > 100  # no artifact bar was stored


def test_a_hole_inside_the_history_is_fetched_again_once(clean):
    from sp2l.smc import history

    history._tried.clear()
    t0 = NOW.replace(second=0)
    hole = (t0 - timedelta(hours=10), t0 - timedelta(hours=8))
    flaky = {"on": True}
    calls = []

    def fetch(a, b):
        calls.append((a, b))
        bars = chart(a, min(b, NOW))
        if flaky["on"]:  # the first load loses two hours
            bars = [x for x in bars if not hole[0].timestamp() <= x["time"] < hole[1].timestamp()]
        return bars

    ensure_history(clean, SYM, fetch, days=1, now=NOW)  # head, tail, then the repair try
    assert history.gaps(clean, SYM, NOW - timedelta(days=1), NOW) == [hole]  # still flaky
    flaky["on"] = False
    history._tried.clear()  # a new process
    ensure_history(clean, SYM, fetch, days=1, now=NOW)
    assert history.gaps(clean, SYM, NOW - timedelta(days=1), NOW) == []
    n = len(calls)
    ensure_history(clean, SYM, fetch, days=1, now=NOW)
    assert len(calls) == n + 1  # only the tail top-up: no hole left to ask for


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
    s = make_setup("test|1", created, D(4100), D(4099), D(4200))
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
    w = Wallet(clean, p, [SYM])
    with clean.begin() as c:
        size = w.size(c, t.entry, t.risk, D("0.001"))
        assert not isinstance(size, str)
        # 1 % of 100 USDT at 1 USDT/unit risk = 1 unit = 4100 notional: cut to 10x the free 100
        assert (size.qty, size.note) == (D("0.243"), "MARGIN_LIMITED")
        sid = store.insert_signal(c, SYM, s, t, p.digest(), size, w.id)
    r = SmcRunner(clean, SYM, p, costs, fetch=None, wallet=w)
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
    # the chart prices sit far below 4099: stopped at once
    assert st == "SL" and res == D(-1) and kinds == ["CREATED", "SL", "BOOKED"]
    with clean.connect() as c:
        led = c.execute(
            text("SELECT kind, amount, balance_after, part FROM smc_wallet_ledger ORDER BY id")
        ).all()
        pnl = c.execute(text("SELECT pnl_usdt FROM smc_signals WHERE id = :i"), {"i": sid}).scalar()
    assert [(k, a, pt) for k, a, _, pt in led] == [
        ("DEPOSIT", D(100), None),
        ("REALIZED_PNL", D("-0.243"), "SL"),
    ]
    assert led[-1][2] == D("99.757") and pnl == D("-0.243")
    assert {x["tf"] for x in hb["timeframes"]} == {"1m", "15m", "1h", "4h"}  # bias, zone, exec
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
            "symbols": [SYM],
            "instruments": {SYM: {"tick": "0.01", "step": "0.001"}},
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
        f"/api/smc/analysis?tf=15m&symbol={SYM}",
        "/api/smc/wallet",
        f"/api/live/snapshot?symbol={SYM}",
    ):
        r = c.get(path)
        assert r.status_code == 200, (path, r.text[:300])
    a = c.get("/api/smc/analysis?tf=5m&bars=200").json()
    assert a["ready"] and {"zones", "htf_zones", "events", "liquidity", "range"} <= set(a)
    assert all("gaps" in z for z in a["zones"] + a["htf_zones"] if z["kind"] == "OB")
    assert c.post("/api/smc/signals").status_code == 405  # read-only
    assert c.get("/api/smc/analysis?tf=2m").status_code == 422


def test_shared_wallet_sizes_from_the_balance_and_caps_positions_across_markets(clean):
    p = SmcParams(account_usdt=D(100), risk_pct=D("0.01"), max_leverage=D(10))
    w = Wallet(clean, p, ["BTCUSDT", "XRPUSDT"])
    assert Wallet(clean, p, ["XRPUSDT", "BTCUSDT"]).id == w.id  # same wallet on restart
    with clean.begin() as c:
        s = w.size(c, D("2.5"), D("0.05"), D("0.1"))  # XRP: 1 USDT risk / 0.05 = 20 units
        assert not isinstance(s, str)
        assert (s.qty, s.notional, s.margin, s.note) == (D(20), D(50), D(5), None)
        assert w.size(c, D(80000), D(1000000), D("0.00001")) == "SIZE_BELOW_STEP"
    w2 = Wallet(clean, SmcParams(account_usdt=D(200)), ["BTCUSDT", "XRPUSDT"])
    assert w2.id != w.id  # a different initial balance starts a new wallet; the old one ends
    with clean.connect() as c:
        q = text("SELECT ended_at FROM smc_wallets WHERE id = :i")
        assert c.execute(q, {"i": w.id}).scalar() is not None
        assert w2.balance(c) == D(200)


def test_an_open_market_position_survives_a_restart_and_stays_open(clean):
    """Regression: an OPEN market entry is reloaded with its fill time (no crash on restart).
    Its own market, so no canonical candle of another test can touch its stop."""
    ensure_history(clean, OTHER, lambda a, b: chart(a, min(b, NOW)), days=2, now=NOW)
    p = SmcParams(history_days=2, lookback_4h=10, lookback_1h=40)
    created = NOW.replace(second=0) - timedelta(minutes=30)
    s = make_setup("test|2", created, D(4010), D(3900), D(4500))
    t = Tracked(
        "test|2",
        Side.LONG,
        D(4010),
        D(3900),
        D(4500),
        created,
        risk_unit(Side.LONG, D(4010), D(3900), Costs(), market=True),
        market=True,
    )
    w = Wallet(clean, p, [OTHER])
    with clean.begin() as c:
        sid = store.insert_signal(c, OTHER, s, t, p.digest(), None, w.id)
    ((loaded_id, loaded, _),) = store.active(clean, OTHER)
    assert loaded_id == sid and loaded.filled_at == created and loaded.market
    r = SmcRunner(clean, OTHER, p, Costs(), fetch=None, wallet=w)
    assert r.step(NOW)  # chart prices stay between 3900 and 4500: still open, no error
    with clean.connect() as c:
        st, last = c.execute(
            text("SELECT state, last_m1 FROM smc_signals WHERE id = :i"), {"i": sid}
        ).one()
    assert st == "OPEN" and last is not None


@pytest.mark.parametrize("exit_on_choch", [False, True])
def test_the_live_runner_replays_exactly_what_the_backtest_finds(clean, exit_on_choch):
    """Minute by minute over the same data, the runner opens and closes exactly the trades of
    the backtest: same setup, entry, stop, target, exit and R."""
    sym = "SOLUSDT"
    if exit_on_choch:  # the long is closed by the bearish 1h CHoCH of bar 23
        rows = SETUP + [(98, 100, 97.6, 99.5), (99.5, 100.5, 98.8, 99), (99, 99.2, 95.5, 96)]
    else:
        rows = SETUP + [
            (98, 102, 97.8, 101.8),
            (101.8, 105, 101.5, 104.8),
            (104.8, 108.5, 104.5, 108),
            (108, 110, 107.5, 109.5),
            (109.5, 109.8, 104, 104.5),
            (104.5, 105, 101, 101.5),
        ]
    m1 = expand(rows)
    p = replace(FIXTURE_P, history_days=2, exit_on_choch=exit_on_choch)
    want = backtest(m1, p, Costs())["trades"]
    assert want  # the fixture trades
    if exit_on_choch:
        assert want[0][1].state.value == "INVALIDATED"

    def fetch(a: datetime, b: datetime) -> list[dict]:
        return [
            {
                "time": int(c.open_time.timestamp()),
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
                "volume": 1.0,
            }
            for c in m1
            if a <= c.open_time < b
        ]

    r = SmcRunner(clean, sym, p, Costs(), fetch=None, wallet=None)
    r.started = T0
    start = T0 + timedelta(hours=15)
    ensure_history(clean, sym, fetch, days=2, now=start)
    t = start
    end = m1[-1].open_time + MIN
    while t <= end:
        ensure_history(clean, sym, fetch, days=2, now=t + timedelta(seconds=20))
        r.step(t + timedelta(seconds=20))
        t += 2 * MIN
    with clean.connect() as c:
        got = c.execute(
            text(
                "SELECT key, entry, sl, tp, state, result_r, parts, created_at,"
                " filled_at FROM smc_signals WHERE symbol = :s ORDER BY created_at"
            ),
            {"s": sym},
        ).all()
    assert len(got) == len(want)
    for row, (s, tr) in zip(got, want, strict=True):
        assert row.key == s.key and row.created_at == s.created_at
        assert (row.entry, row.sl, row.tp) == (tr.entry, tr.sl, tr.tp)
        assert row.state == tr.state.value and row.filled_at == tr.filled_at
        assert row.result_r == (None if tr.result_r is None else round(tr.result_r, 18))
        assert [(x["kind"], D(x["price"]), D(x["frac"])) for x in row.parts] == [
            (x.kind, x.price, x.frac) for x in tr.parts
        ]
