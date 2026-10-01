"""V5.8 acceptance: three-tier recovery (dual WS -> recent-trades -> validated Tabdeal chart
history), warmup independence from process identity, and exact 10x strategy leverage.

The collector is driven second by second on a deterministic tape. Tabdeal's chart history is
modelled exactly as measured (TradingView-continuous bars bucketed by record time = stream
time + 30 ms, no trade count). An outage = both WebSockets AND recent-trades unreachable.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest

from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.indicators.m5_state import M5State
from sp2l.marketdata.collector import Collector, CollectorConfig
from sp2l.marketdata.history import parse_bars
from sp2l.marketdata.m1_builder import M1Result, M1Status, Quality
from sp2l.marketdata.m5_aggregator import M5Aggregator
from sp2l.marketdata.tabdeal_public import RestTrade
from sp2l.strategy.risk.engine import (
    STRATEGY_LEVERAGE,
    CostModel,
    ExchangeFilters,
    Mode,
    RiskInputs,
    size_setup,
    unverifiable_liquidation,
)
from tests.conftest import T0
from tests.unit.test_collector import frame
from tests.unit.test_collector_repair import RepairSink

MIN = timedelta(minutes=1)
REC = timedelta(milliseconds=30)  # Tabdeal record time - stream time


def _tape(minutes: int) -> list[tuple[int, float, D, D]]:
    rng = random.Random(58)
    px = D("84000.0")
    out = []
    for i in range(minutes * 30):  # a trade every 2 s, never within 30 ms of a boundary
        px += D(rng.randint(-40, 40)) / 10
        out.append((i + 1, 1.5 + 2 * i, px, D("0.001") + D(i % 5) / 1000))
    return out


TAPE = _tape(200)


def at(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


def chart(lo: datetime, hi: datetime, now: datetime, tamper: bool = False) -> list[dict[str, Any]]:
    """Tabdeal chart bars [lo, hi] as the endpoint returns them at `now` (forming bar incl.)."""
    bars: dict[datetime, list[D]] = {}
    for _, t, px, q in TAPE:
        rt = at(t) + REC
        if rt > now:
            break
        m = rt.replace(second=0, microsecond=0)
        b = bars.setdefault(m, [px, px, px, px, D(0)])
        b[1], b[2], b[3], b[4] = max(b[1], px), min(b[2], px), px, b[4] + q
    out, prev = [], None
    for m in sorted(bars):
        o, h, lo_, c, v = bars[m]
        if prev is not None:  # TradingView continuity: open = previous close, H/L include it
            o, h, lo_ = prev, max(h, prev), min(lo_, prev)
        prev = c
        if lo <= m <= hi:
            h2 = h + 50 if tamper else h
            out.append(
                {
                    "time": int(m.timestamp()),
                    "open": str(o),
                    "high": str(h2),
                    "low": str(lo_),
                    "close": str(c),
                    "volume": str(v),
                }
            )
    return out


class Sim:
    """Collector on A+B+REST+history with a controllable clock and outages."""

    def __init__(self, start_s: float = 0.0, history: str = "ok", **resume: Any) -> None:
        self.now = at(start_s)
        self.sink = RepairSink()
        self.sink.late = []
        self.sink.on_late_rest_trade = lambda t: self.sink.late.append(t)
        cfg = CollectorConfig(
            "BTCUSDT",
            connections=("A", "B"),
            repair_enabled=True,
            rest_enabled=True,
            history_enabled=True,
        )
        self.col = Collector(cfg, self.sink, clock=lambda: self.now, **resume)
        self.up = False
        self.i = next((k for k, x in enumerate(TAPE) if at(x[1]) >= self.now), len(TAPE))
        self.history = history
        self.history_calls = 0

    def connect(self) -> None:
        for c in ("A", "B"):
            self.col.session_started(self.now, c)
            self.col.pong(self.now, self.now + timedelta(milliseconds=100), c)
        self.up = True

    def disconnect(self) -> None:
        for c in ("A", "B"):
            self.col.session_ended(self.now, "CLOSED_NO_FRAME", c)
        self.up = False

    def run(self, until_s: float, network: bool = True, rest: bool = True) -> None:
        while self.now < at(until_s):
            self.now += timedelta(seconds=1)
            while self.i < len(TAPE) and at(TAPE[self.i][1]) + timedelta(seconds=0.1) <= self.now:
                seq, t, px, q = TAPE[self.i]
                self.i += 1
                if self.up and network:
                    for c in ("A", "B"):
                        self.col.ingest(
                            frame(seq, at(t), price=str(px), amount=str(q)),
                            at(t) + timedelta(seconds=0.1),
                            c,
                        )
            if self.up and network:
                for c in ("A", "B"):
                    self.col.pong(self.now - timedelta(milliseconds=50), self.now, c)
            if int((self.now - T0).total_seconds()) % 5 == 0:
                if network and rest:
                    win = [x for x in TAPE if at(x[1]) + REC <= self.now - timedelta(seconds=0.5)]
                    self.col.ingest_rest(
                        [
                            RestTrade(at(t) + REC, px, q, "BUY", {"s": s})
                            for s, t, px, q in win[-50:]
                        ],
                        self.now,
                        self.now,
                    )
                else:
                    self.col.ingest_rest(None, self.now)
            self.col.tick(self.now, (self.now - T0).total_seconds())
            for job in self.col.due_history_jobs(self.now):
                job.in_flight = True
                self.history_calls += 1
                a, b = self.col.history_window(job)
                raw = (
                    None
                    if self.history == "down" or not network
                    else chart(a, b, self.now, tamper=self.history == "tampered")
                )
                self.col.resolve_history(job, raw, self.now)

    def m1(self) -> dict[datetime, M1Result]:
        """Minutes after the very first one (a fresh collector's first minute has no
        coverage before the first session: the COLLECTOR_START gap, never repairable)."""
        return {m.open_time: m for m in self.sink.m1 if m.open_time > T0}


def outage_run(minutes: float, history: str = "ok", after_min: int = 20) -> Sim:
    s = Sim(history=history)
    s.connect()
    s.run(40 * 60)
    s.disconnect()
    s.run(40 * 60 + minutes * 60, network=False)
    s.connect()
    s.run(40 * 60 + minutes * 60 + after_min * 60)
    return s


@pytest.fixture(scope="module")
def reference() -> dict[datetime, M1Result]:
    s = Sim()
    s.connect()
    s.run(130 * 60)
    return s.m1()


def m5_state(m1s: list[M1Result], warmup: int = 150) -> M5State:
    st, agg = M5State(warmup), M5Aggregator()
    for m in sorted(m1s, key=lambda x: x.open_time):
        r = agg.add(m)
        if r is not None:
            st.add(r)
    return st


def indicator_hash(st: M5State) -> list[tuple[Any, ...]]:
    assert st.segment is not None
    return [
        (
            b.open_time,
            b.candle.high,
            b.candle.low,
            b.candle.close,
            b.atr14,
            b.adx,
            b.ema20,
            b.chop14,
        )
        for b in st.segment.bars
    ]


# ---- AT-1 / AT-2 (+ 60 min for the report): outages repaired, no 150 reset ------------------


@pytest.mark.parametrize("minutes", [5, 30, 60])
def test_server_outage_is_repaired_from_recent_trades_and_history_without_reset(minutes, reference):
    s = outage_run(minutes)
    got = s.m1()
    gap_first = at(40 * 60 - 60)  # coverage is proven 1 s behind the last pong
    gap_last = at(40 * 60 + minutes * 60)
    span = [m for m in got.values() if gap_first <= m.open_time <= gap_last]
    assert all(m.status is M1Status.OK for m in got.values())  # no DATA_GAP anywhere
    q = {m.quality for m in span}
    assert Quality.TABDEAL_HISTORY_REPAIRED in q
    assert q <= {Quality.TABDEAL_HISTORY_REPAIRED, Quality.RECENT_TRADES_REPAIRED}
    hist = [m for m in span if m.quality is Quality.TABDEAL_HISTORY_REPAIRED]
    assert all(m.candle.trade_count is None for m in hist)  # never invented
    assert all(m.lineage["repair_type"] == "CANDLE_HISTORY_REPAIR" for m in hist)
    st = m5_state(list(got.values()))
    assert st.breaks == []  # one continuous segment: warmup counts straight through
    assert st.segment is not None and st.segment.anchor_open_time == T0 + timedelta(minutes=5)
    ok = [r for r in s.sink.repairs if r[2] == "REPAIRED"]
    assert ok and not [r for r in s.sink.repairs if r[2] == "UNRECOVERED"]
    # every minute outside the outage equals the uninterrupted reference exactly
    for t, m in got.items():
        if not (gap_first <= t <= gap_last) and t in reference:
            assert m.candle == reference[t].candle, t


def test_history_repair_indicators_match_the_historical_reference_rebuild(reference):
    """AT-7: the live path (collector -> held minutes -> M5 -> indicators) equals an offline
    rebuild from the same canonical minutes + the same validated chart bars."""
    s = outage_run(30)
    got = s.m1()
    bars = parse_bars(chart(min(got), max(got) + MIN, at(10**6)))
    assert bars is not None
    rebuilt = [
        M1Result(t, M1Status.OK, bars[t], Quality.TABDEAL_HISTORY_REPAIRED)
        if m.quality is Quality.TABDEAL_HISTORY_REPAIRED
        else reference[t]
        if m.quality is not Quality.RECENT_TRADES_REPAIRED
        else m
        for t, m in got.items()
    ]
    assert indicator_hash(m5_state(list(got.values()))) == indicator_hash(m5_state(rebuilt))


# ---- AT-3 / AT-15: collector restart = bootstrap from stored history -------------------------


def test_collector_restart_bootstraps_from_history_without_reset_or_duplicates(reference):
    a = Sim()
    a.connect()
    a.run(40 * 60)
    done = a.sink.m1
    nxt = done[-1].open_time + MIN
    bucket = [m for m in done if m.open_time >= nxt - timedelta(minutes=nxt.minute % 5)]
    trades = [t for t, _ in a.sink.trades if t.exch_ts >= nxt - timedelta(minutes=5)]
    b = Sim(
        start_s=50 * 60,  # 10 minutes of downtime
        resume_from=nxt,
        resume_m1=bucket,
        resume_trades=trades,
        resume_recent=done[-30:],
    )
    b.connect()
    b.run(80 * 60)
    all_m1 = [m for m in done + b.sink.m1 if m.open_time > T0]
    times = [m.open_time for m in all_m1]
    assert len(times) == len(set(times))  # no duplicate candle across the restart
    ids = [t.trade_id for t, _ in a.sink.trades + b.sink.trades]
    assert len(ids) == len(set(ids))  # no duplicate trade across the restart
    assert all(m.status is M1Status.OK for m in all_m1)
    assert any(m.quality is Quality.TABDEAL_HISTORY_REPAIRED for m in b.sink.m1)
    st = m5_state(all_m1)
    assert (
        st.breaks == []
        and st.segment is not None
        and st.segment.anchor_open_time == T0 + timedelta(minutes=5)
    )
    for m in b.sink.m1:
        if m.quality is Quality.LIVE_RECONCILED:
            assert m.candle == reference[m.open_time].candle


def test_warmup_does_not_depend_on_process_or_session_identity(reference):
    """AT-9 invariant: same canonical history -> same M5 segment, whatever the restarts."""
    s = Sim()  # (process restarts: test above); here: repeated WS/session generations
    s.connect()
    for k in range(6):  # six WS generations, each drop within REST reach
        s.run(20 * 60 + k * 120)
        s.disconnect()
        s.run(20 * 60 + k * 120 + 15)
        s.connect()
    s.run(40 * 60)
    st = m5_state(list(s.m1().values()))
    ref = m5_state([m for t, m in reference.items() if t < max(s.m1()) + MIN])
    assert st.breaks == [] and indicator_hash(st)[: len(indicator_hash(ref))] == indicator_hash(ref)


# ---- AT-4: both WebSockets reconnect inside the recent-trades window -------------------------


def test_both_ws_reconnect_is_an_exact_recent_trades_repair(reference):
    s = Sim()
    s.connect()
    s.run(30 * 60)
    s.disconnect()
    s.run(30 * 60 + 20)  # REST keeps polling (only the sockets dropped)
    s.connect()
    s.run(40 * 60)
    got = s.m1()
    assert all(m.status is M1Status.OK for m in got.values())
    assert any(m.quality is Quality.RECENT_TRADES_REPAIRED for m in got.values())
    assert not any(m.quality is Quality.TABDEAL_HISTORY_REPAIRED for m in got.values())
    for t, m in got.items():
        assert m.candle == reference[t].candle  # exact: identical to the uninterrupted run
    assert s.history_calls == 0


# ---- AT-9: history unavailable or invalid -> fail closed, 150 warmup -------------------------


@pytest.mark.parametrize(
    ("mode", "failure"),
    [("down", "HISTORY_UNAVAILABLE"), ("tampered", "HISTORY_VALIDATION_FAILED")],
)
def test_history_unavailable_or_invalid_fails_closed(mode, failure):
    s = outage_run(10, history=mode)
    unrec = [r for r in s.sink.repairs if r[2] == "UNRECOVERED"]
    assert unrec and unrec[0][3] == failure
    got = s.m1()
    gaps = [m for m in got.values() if m.status is M1Status.DATA_GAP]
    assert gaps  # nothing synthesized
    assert not any(m.quality is Quality.TABDEAL_HISTORY_REPAIRED for m in got.values())
    st = m5_state(list(got.values()))
    assert st.breaks and st.segment is not None and st.segment.anchor_open_time > at(40 * 60)
    assert not st.warm  # the 150-bar warmup restarts


def test_short_gap_before_a_full_outage_is_still_repaired_from_history():
    """Regression (30 Sep 05:31 Tehran): an 8 s gap, the stream back for a few seconds while
    REST still fails, then the server's whole connection is down longer than the history
    deadline. The deadline only runs while Tabdeal is reachable, so the short gap is repaired
    from history once the connection returns - no DATA_GAP and no 150-bar warmup reset."""
    s = Sim()
    s.connect()
    s.run(40 * 60)
    s.disconnect()
    s.run(40 * 60 + 8, network=False)  # the short gap
    s.connect()
    s.run(40 * 60 + 20, rest=False)  # stream back, recent-trades still failing
    s.disconnect()
    s.run(40 * 60 + 20 + 10 * 60, network=False)  # full outage > history deadline (5 min)
    s.connect()
    s.run(40 * 60 + 20 + 10 * 60 + 20 * 60)
    assert [r for r in s.sink.repairs if r[2] == "UNRECOVERED"] == []
    assert s.history_calls > 1  # it kept retrying through the outage
    got = s.m1()
    assert all(m.status is M1Status.OK for m in got.values())
    st = m5_state(list(got.values()))
    assert st.breaks == [] and st.segment is not None
    assert st.segment.anchor_open_time == T0 + timedelta(minutes=5)


def test_history_unavailable_while_the_feed_is_up_still_fails_at_the_deadline():
    """The pause is only for a full outage: with the stream up and history failing, the
    deadline runs as before and the gap fails closed (HISTORY_UNAVAILABLE)."""
    s = outage_run(10, history="down")
    unrec = [r for r in s.sink.repairs if r[2] == "UNRECOVERED"]
    assert unrec and unrec[0][3] == "HISTORY_UNAVAILABLE"


# ---- liquidity: PRICE vs LIQUIDITY readiness ------------------------------------------------


def test_history_bars_keep_price_context_and_leave_only_liquidity_unknown():
    """PRICE_CONTEXT_READY counts history bars; LIQUIDITY_CONTEXT_READY needs the context
    bar and the 20 before it with a KNOWN trade count: 21 M5 bars (105 min) after the last
    history-repaired bucket. Nothing is invented meanwhile."""
    s = outage_run(30, after_min=60)
    st = m5_state(list(s.m1().values()), warmup=10)
    assert st.price_ready
    seg = st.segment
    assert seg is not None
    unknown = [i for i, b in enumerate(seg.bars) if b.candle.trade_count is None]
    assert unknown  # the history-repaired buckets carry no trade count
    after = len(seg.bars) - 1 - unknown[-1]
    assert not st.liquidity_ready and st.liquidity_bars_missing() == 21 - after
    # the Context gate itself: the trade count is never invented; V5.10 lets volume decide
    # when it is not low, otherwise the gate stays UNKNOWN
    from fractions import Fraction

    from sp2l.strategy.context.engine import (
        LIQ_BASIS_VOLUME_ONLY,
        ContextSnapshot,
        Reason,
        _liquidity,
    )

    snap = ContextSnapshot()
    _liquidity(snap, seg.bars, len(seg.bars) - 1)
    assert snap.tradecount_ratio is None and snap.volume_ratio is not None
    assert not snap.liquidity_context_ready and snap.liquidity_basis == LIQ_BASIS_VOLUME_ONLY
    expected = "PASS" if snap.volume_ratio >= Fraction(1, 2) else Reason.LIQUIDITY_UNKNOWN
    assert snap.liquidity_status == expected


# ---- AT-10 .. AT-13: exact 10x strategy leverage; exchange leverage only blocks Live ---------


COSTS = CostModel(D("0.0008"), D("0.00095"), D("0.000198"))
FILTERS = ExchangeFilters(D("0.1"), D("0.00001"), None, None, verified=False)


def test_shadow_leverage_is_exactly_10x():
    assert STRATEGY_LEVERAGE == 10
    eng = ShadowSymbolEngine("BTCUSDT", tick=D("0.1"), costs=COSTS, filters=FILTERS)
    assert eng.leverage == 10 and eng.gates.leverage == 10
    with pytest.raises(ValueError, match="exactly 10x"):
        ShadowSymbolEngine("BTCUSDT", tick=D("0.1"), costs=COSTS, filters=FILTERS, leverage=61)
    from sp2l.spec.loader import load_rules

    assert load_rules().leverage == 10


def test_q_margin_limit_uses_10x():
    from sp2l.core.types import Candle, Side
    from sp2l.strategy.levels import compute_levels

    origin = Candle(T0, D("83950"), D("83990"), D("83900"), D("83980"), D("1"), 5)
    last = Candle(T0 + MIN, D("84010"), D("84100"), D("84000"), D("84090"), D("1"), 5)
    lv = compute_levels(Side.LONG, origin, last, D("0.1"))
    r = size_setup(
        RiskInputs(
            Mode.SHADOW,
            lv,
            D("100"),
            D("100"),
            STRATEGY_LEVERAGE,
            COSTS,
            FILTERS,
            unverifiable_liquidation,
        )
    )
    import decimal

    from sp2l.core.numeric import DECISION_CONTEXT

    with decimal.localcontext(DECISION_CONTEXT):
        expected = D("100") * 10 / (lv.e1 + lv.e2)  # available_margin * 10 / (E1 + E2)
    assert r.q_margin_limit == expected


class _Probe:
    def __init__(self, body: Any) -> None:
        self.status, self.body, self.error = 200, body, None

    def evidence(self) -> dict[str, Any]:
        return {"response": self.body}


class _Client:
    """Fake READ-ONLY client: records every call; there is no write method at all."""

    def __init__(self, lev: int) -> None:
        self.lev, self.calls = lev, []

    def get(self, path: str, params: Any = None, signed: bool = False) -> _Probe:
        self.calls.append(("GET", path))
        return _Probe({"leverage": self.lev, "symbol": "BTC_USDT"})


def test_exchange_61x_cannot_change_shadow_and_blocks_live():
    """AT-11: the account reads 61x -> Live LEVERAGE_MISMATCH; Shadow results unchanged."""
    from sp2l.validation.readonly import check_cross_10x

    fake = _Client(61)
    res = check_cross_10x(fake, "BTC_USDT", _Probe([]))
    assert not res.passed and res.evidence["leverage_blocker"] == "LEVERAGE_MISMATCH"
    assert res.evidence["exchange_leverage"] == 61 and res.evidence["strategy_leverage"] == 10
    assert "LEVERAGE_MISMATCH" in res.notes
    # the Shadow engine has no exchange-leverage input: two identical runs, one after the
    # 61x read, produce identical results
    from tests.replay.test_gap_repair import drive
    from tests.replay.test_gap_repair import m5_state as eng_state

    a = drive(None, "live")
    check_cross_10x(_Client(61), "BTC_USDT", _Probe([]))
    b = drive(None, "live")
    assert [(m.created_at, str(m.state), m.events) for m in a.finished] == [
        (m.created_at, str(m.state), m.events) for m in b.finished
    ]
    assert eng_state(a) == eng_state(b) and a.leverage == b.leverage == 10


def test_exchange_exactly_10x_passes_the_leverage_blocker():
    """AT-12."""
    from sp2l.validation.readonly import check_cross_10x

    res = check_cross_10x(_Client(10), "BTC_USDT", _Probe([]))
    assert res.evidence["leverage_blocker"] is None and res.evidence["exchange_leverage"] == 10
    assert "leverage=10 (OK)" in res.notes


def test_no_account_write_action_exists():
    """AT-13: the leverage check only ever GETs; no write path exists in the module."""
    import inspect

    from sp2l.validation import readonly
    from sp2l.validation.readonly import READ_ONLY_PATHS, check_cross_10x

    fake = _Client(61)
    check_cross_10x(fake, "BTC_USDT", _Probe([]))
    assert fake.calls and all(m == "GET" for m, _ in fake.calls)
    src = inspect.getsource(readonly)
    for verb in ('method="POST"', 'method="PUT"', 'method="DELETE"', "urlopen(data"):
        assert verb not in src
    assert all(p.startswith("/r/fapi/") for p in READ_ONLY_PATHS)


def test_history_client_is_read_only_and_tabdeal_only():
    from sp2l.marketdata import tabdeal_public

    assert tabdeal_public.BASE == "https://api-web.tabdeal.org"
    assert set(tabdeal_public._ALLOWED) == {
        "/special-margin/recent-trades/",
        "/special-margin/plots/history/",
    }
    with pytest.raises(ValueError):
        tabdeal_public._get("/special-margin/order/", {}, 1)
