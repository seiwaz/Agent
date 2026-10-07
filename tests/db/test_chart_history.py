"""Chart history from Tabdeal's chart feed: the newest page with its forming bar, older pages
back to the listing, closed bars cached (a cached page is not fetched again), Tehran-aligned
bars kept as Tabdeal serves them, and the overlays computed over every loaded bar."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from sp2l.chart.history import SECONDS, TabdealHistory
from sp2l.chart.overlays import ChartParams
from sp2l.chart.service import ChartService
from sp2l.smc.model import SmcParams
from tests.chart.fake_tabdeal import FakeTabdeal

pytestmark = pytest.mark.db


@pytest.fixture()
def hist(engine):
    with engine.begin() as c:
        c.execute(text("DELETE FROM chart_bars"))
    fake = FakeTabdeal(listed_days=30)
    return TabdealHistory(engine, "BTCUSDT", fetch=fake), fake


def test_tail_has_the_forming_bar_last_and_stores_only_closed_bars(engine, hist):
    h, fake = hist
    bars = h.tail("1h", 100)
    assert len(bars) == 100 and fake.calls[-1][0] == "BTC_USDT"
    assert all(b["t"] % 3600 == 1800 for b in bars)  # Tehran-aligned, as Tabdeal's chart
    with engine.connect() as c:
        stored = {int(r[0].timestamp()) for r in c.execute(text("SELECT open_time FROM chart_bars WHERE tf = '1h'"))}
    assert bars[-1]["t"] not in stored  # the forming bar is not cached
    assert {b["t"] for b in bars[:-1]} <= stored
    calls = len(fake.calls)
    h.tail("1h", 100)
    assert len(fake.calls) == calls  # within the tail TTL nothing is fetched again


def test_older_pages_reach_the_listing_and_are_contiguous(hist):
    h, fake = hist
    bars = h.tail("4h", 50)
    seen = list(bars)
    more = True
    while more:
        page, more = h.older("4h", seen[0]["t"], 50)
        assert all(b["t"] < seen[0]["t"] for b in page)
        seen = page + seen
    ts = [b["t"] for b in seen]
    assert ts == sorted(set(ts)) and all(b - a == SECONDS["4h"] for a, b in zip(ts, ts[1:], strict=False))
    assert ts[0] >= fake.listed and ts[0] - fake.listed < SECONDS["4h"]  # back to the listing
    assert h.older("4h", ts[0], 50) == ([], False)


def test_a_cached_page_is_read_from_the_database(hist):
    h, fake = hist
    bars = h.tail("15m", 200)
    page, _ = h.older("15m", bars[0]["t"], 100)
    calls = len(fake.calls)
    again, more = h.older("15m", bars[0]["t"], 100)
    assert [b["t"] for b in again] == [b["t"] for b in page] and more and len(fake.calls) == calls
    assert all(abs(a["c"] - b["c"]) < 1e-6 for a, b in zip(again, page, strict=True))


def test_overlays_cover_every_loaded_bar(engine):
    with engine.begin() as c:
        c.execute(text("DELETE FROM chart_bars"))
    fake = FakeTabdeal(listed_days=60)
    svc = ChartService(engine, "BTCUSDT", SmcParams(htf_grid="utc"), TabdealHistory(engine, "BTCUSDT", fetch=fake))
    tail = svc.candles("1h", 300)["items"]
    assert tail[-1]["forming"] and not tail[-2]["forming"]
    older = svc.candles("1h", 300, before=tail[0]["t"])
    first = older["items"][0]["t"]
    o = svc.overlays("1h", first, 500, ChartParams())
    assert o["ready"] and o["bars"] == len(older["items"]) + len(tail) - 1  # closed bars only
    assert svc.trends()["timeframes"][0]["tf"] == "5m"
