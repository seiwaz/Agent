"""V5.12 history bootstrap: a cold-started DB with only a few live minutes becomes
PRICE_CONTEXT_READY right after the bootstrap - no 150-bar live warmup - and it fails closed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import text

from sp2l.core.types import Candle
from sp2l.marketdata.m1_builder import M1Result, M1Status, Quality
from sp2l.marketdata.m5_aggregator import M5Aggregator
from sp2l.persistence.market_store import MarketStore
from sp2l.runtime.reconcile import bootstrap_history

pytestmark = pytest.mark.db
MIN = timedelta(minutes=1)
N = 1200  # 20 h of true minutes, ending now (readiness is judged against wall-clock time)
_NOW5 = datetime.now(UTC).replace(second=0, microsecond=0)
_NOW5 -= timedelta(minutes=_NOW5.minute % 5)
BASE = _NOW5 - N * MIN
LIVE_FROM = 1178  # the collector started inside minute 1177 (partial -> not stored)
NOW = BASE + N * MIN + timedelta(seconds=90)  # minute N-1 is final, minute N is forming
HOLES = {100, 101, 102, 400, 700, 701}  # a 3-minute hole (DATA_GAP) + 1 and 2 minutes


def true_series() -> dict[datetime, Candle]:
    out = {}
    for i in range(N + 1):
        t = BASE + i * MIN
        px = D(4190) + D(i % 13) / 4
        out[t] = Candle(t, px, px + D("0.5"), px - D("0.5"), px + D("0.25"), D(3), 7)
    return out


SERIES = true_series()


def _set_clock() -> None:
    """Re-anchor the synthetic series to the current wall-clock 5-minute boundary."""
    global BASE, NOW, SERIES
    now5 = datetime.now(UTC).replace(second=0, microsecond=0)
    now5 -= timedelta(minutes=now5.minute % 5)
    BASE = now5 - N * MIN
    NOW = BASE + N * MIN + timedelta(seconds=90)
    SERIES = true_series()


def chart(a: datetime, b: datetime, tamper: bool = False) -> list[dict[str, object]]:
    """Tabdeal's chart under the continuity model; it omits the HOLES minutes."""
    out, prev = [], None
    for i, (t, cd) in enumerate(SERIES.items()):
        o, h, lo = (cd.open, cd.high, cd.low) if prev is None else (
            prev, max(cd.high, prev), min(cd.low, prev))  # fmt: skip
        prev = cd.close
        if i in HOLES or not a <= t < b:
            continue
        out.append({"time": int(t.timestamp()), "open": str(o),
                    "high": str(h + (3 if tamper else 0)), "low": str(lo),
                    "close": str(cd.close), "volume": str(cd.volume)})  # fmt: skip
    return out


_n = iter(range(100))


@pytest.fixture()
def live(engine):  # type: ignore[no-untyped-def]
    """A fresh symbol per test (canonical candles can never be deleted)."""
    global SYM
    SYM = f"XAU{next(_n)}USDT"
    _set_clock()  # anchored when the test runs: readiness is judged against wall-clock time
    store = MarketStore(engine, SYM)
    agg = M5Aggregator()
    for i in range(LIVE_FROM - 1, N):
        t = BASE + i * MIN
        m = (
            M1Result(t, M1Status.DATA_GAP, None)
            if i == LIVE_FROM - 1
            else M1Result(t, M1Status.OK, SERIES[t], Quality.LIVE_RECONCILED)
        )
        store.upsert_m1(m)
        r = agg.add(m)
        if r is not None:
            store.upsert_m5(r)
    return store


def count(engine, table: str) -> int:  # type: ignore[no-untyped-def]
    with engine.connect() as c:
        return int(
            c.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE symbol = :s"), {"s": SYM}
            ).scalar_one()
        )


def run(engine, fetch, apply=True):  # type: ignore[no-untyped-def]
    return bootstrap_history(engine, SYM, fetch, lookback=N * MIN, apply=apply, now=NOW)


def test_cold_start_is_price_context_ready_right_after_the_bootstrap(engine, live):
    out = run(engine, chart)
    assert out["applied"] and out["validation"]["ok"], out
    assert out["synthetic_no_trade_minutes"] == 3  # minutes 400, 700, 701
    assert out["gaps_left"] == [((BASE + 100 * MIN).isoformat(), (BASE + 102 * MIN).isoformat())]
    with engine.connect() as c:
        stitched = c.execute(
            text(
                "SELECT quality, trade_count FROM candles_1m WHERE symbol = :s AND open_time = :t"
            ),
            {"s": SYM, "t": BASE + (LIVE_FROM - 1) * MIN},
        ).one()
        syn = c.execute(
            text(
                "SELECT quality, synthetic_no_trade, volume, trade_count FROM candles_1m"
                " WHERE symbol = :s AND open_time = :t"
            ),
            {"s": SYM, "t": BASE + 400 * MIN},
        ).one()
        live_row = c.execute(
            text(
                "SELECT quality, trade_count FROM candles_1m WHERE symbol = :s AND open_time = :t"
            ),
            {"s": SYM, "t": BASE + LIVE_FROM * MIN},
        ).one()
        rep = c.execute(
            text("SELECT reason, status, method FROM gap_repairs WHERE symbol = :s"), {"s": SYM}
        ).all()
    assert tuple(stitched) == ("TABDEAL_HISTORY_REPAIRED", None)  # the partial start minute
    assert tuple(syn) == ("TABDEAL_HISTORY_REPAIRED", True, D(0), 0)
    assert tuple(live_row) == ("LIVE_RECONCILED", 7)  # live minutes are never touched
    assert [tuple(r) for r in rep] == [("V5.12_HISTORY_BOOTSTRAP", "REPAIRED", "TABDEAL_HISTORY")]
    again = run(engine, chart)
    assert again["minutes_from_chart"] == 0 and again["synthetic_no_trade_minutes"] == 0


def test_validation_failure_writes_nothing(engine, live):
    before = count(engine, "candles_1m")
    out = run(engine, lambda a, b: chart(a, b, tamper=True))
    assert not out["applied"] and out["failure"] == "HISTORY_VALIDATION_FAILED"
    assert count(engine, "candles_1m") == before and count(engine, "gap_repairs") == 0


def test_fetch_failure_and_no_live_minutes_fail_closed(engine, live):
    def down(a, b):  # type: ignore[no-untyped-def]
        raise TimeoutError("offline")

    before = count(engine, "candles_1m")
    out = run(engine, down)
    assert out["failure"].startswith("FETCH_FAILED") and count(engine, "candles_1m") == before
    empty = bootstrap_history(engine, "NEVERSEENUSDT", chart, lookback=N * MIN, apply=True, now=NOW)
    assert empty["failure"] == "NO_LIVE_MINUTES_TO_VALIDATE_AGAINST"
    with engine.connect() as c:
        n = c.execute(text("SELECT COUNT(*) FROM candles_1m WHERE symbol = 'NEVERSEENUSDT'"))
        assert n.scalar_one() == 0


def test_dry_run_reports_without_writing(engine, live):
    before = count(engine, "candles_1m")
    out = run(engine, chart, apply=False)
    assert out["validation"]["ok"] and out["minutes_from_chart"] > 1000 and not out["applied"]
    assert count(engine, "candles_1m") == before
