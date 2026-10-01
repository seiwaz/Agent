"""V5.6 exact trade-level gap repair from Tabdeal recent-trades (pure decision logic)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

from sp2l.marketdata.repair import KnownTrade, repair_gap
from sp2l.marketdata.tabdeal_public import RestTrade, parse_recent_trades
from tests.conftest import T0

DMAX = timedelta(milliseconds=500)


def at(s: float):
    return T0 + timedelta(seconds=s)


def rest(created_s: float, px: str, qty: str = "0.001", side: str = "BUY") -> RestTrade:
    return RestTrade(at(created_s), D(px), D(qty), side, {"created": str(created_s)})


def known(ts_s: float, px: str, qty: str = "0.001") -> KnownTrade:
    return KnownTrade(at(ts_s), D(px), D(qty))


def test_parses_the_observed_recent_trades_payload():
    payload = {
        "trades": [
            {
                "created": "2026-09-27T05:40:12.443063Z",
                "updated": "2026-09-27T05:40:12.443084Z",
                "market_id": 1,
                "amount": "0.00586",
                "price": "84527",
                "side": 2,
                "side_name": "Sell",
            },
            {
                "created": "2026-09-27T05:40:10.502666Z",
                "updated": "2026-09-27T05:40:10.502688Z",
                "market_id": 1,
                "amount": "0.00449",
                "price": "84527",
                "side": 2,
                "side_name": "Sell",
            },
        ]
    }
    rows = parse_recent_trades(payload)
    assert [r.qty for r in rows] == [D("0.00449"), D("0.00586")]  # oldest first
    assert rows[0].side == "SELL" and rows[0].created.tzinfo is not None


def test_exact_repair_recovers_only_the_missing_trades():
    # live before the gap [20, 40), live again after; REST = Tabdeal record time (+30 ms)
    live = [known(10.0, "100"), known(15.0, "101"), known(40.2, "102"), known(45.0, "103")]
    window = [
        rest(10.03, "100"),
        rest(15.03, "101"),
        rest(25.03, "99"),
        rest(33.03, "104"),
        rest(40.23, "102"),
        rest(45.03, "103"),
    ]
    out = repair_gap(at(20), at(40), window, live, delta_max=DMAX, fetched_at=at(47))
    assert out.repaired, out.reason
    assert [(str(t.price)) for t in out.trades] == ["99", "104"]  # live ones not duplicated
    assert all(at(20) <= t.exch_ts < at(40) and t.source == "REPAIR_REST" for t in out.trades)
    assert out.detail["matched_live"] == 1 and out.detail["recovered"] == 2


def test_rest_window_that_does_not_reach_back_before_the_gap_fails_closed():
    window = [rest(21.0, "99"), rest(30.0, "100")]
    out = repair_gap(at(20), at(40), window, [], delta_max=DMAX, fetched_at=at(47))
    assert not out.repaired and out.reason == "REST_WINDOW_TOO_SHORT"


def test_a_missing_trade_that_could_belong_to_two_minutes_fails_closed():
    # recorded 0.2 s after a minute boundary: its stream time may be in either minute
    window = [rest(5.0, "100"), rest(60.2, "99")]
    out = repair_gap(
        at(30), at(90), window, [known(5.0 - 0.03, "100")], delta_max=DMAX, fetched_at=at(95)
    )
    assert not out.repaired and out.reason == "AMBIGUOUS_MINUTE"


def test_an_ambiguous_close_fails_closed():
    # two recovered trades 0.1 s apart at the end of the minute, different prices
    window = [rest(5.0, "100"), rest(58.8, "99"), rest(58.9, "98")]
    out = repair_gap(
        at(30), at(59.5), window, [known(4.97, "100")], delta_max=DMAX, fetched_at=at(61)
    )
    assert not out.repaired and out.reason == "AMBIGUOUS_CLOSE"


def test_time_model_violation_on_the_window_fails_closed():
    # a live trade whose record time is 0.8 s later than its stream time: model broken
    window = [rest(5.0, "100"), rest(25.8, "101")]
    out = repair_gap(
        at(20),
        at(40),
        window,
        [known(4.97, "100"), known(25.0, "101")],
        delta_max=DMAX,
        fetched_at=at(47),
    )
    assert not out.repaired and out.reason == "TIME_MODEL_VIOLATION"


def test_must_not_fetch_before_the_gap_end_has_settled():
    out = repair_gap(at(20), at(40), [rest(5, "1")], [], delta_max=DMAX, fetched_at=at(40.2))
    assert not out.repaired and out.reason == "FETCHED_TOO_EARLY"
