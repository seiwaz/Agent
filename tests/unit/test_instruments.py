"""Precision of every dashboard market: the config's instruments first, else Tabdeal's futures
exchangeInfo; nothing is known (so nothing is traded) before exchangeInfo has answered."""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from sp2l.marketdata.instruments import Instruments, parse

INFO = {"symbols": [
    {"symbol": "BTC_USDT", "pricePrecision": 1, "quantityPrecision": 5, "status": "TRADING"},
    {"symbol": "SOL_USDT", "pricePrecision": 2, "quantityPrecision": 2, "status": "TRADING"},
    {"symbol": "ADA_USDT", "pricePrecision": 5, "quantityPrecision": 0, "status": "TRADING"},
    {"symbol": "BROKEN", "pricePrecision": None},
]}
MARKETS = ["BTCUSDT", "SOLUSDT", "ADAUSDT", "NEARUSDT"]


def test_parse_reads_ticks_and_steps_and_skips_broken_rows():
    p = parse(INFO)
    assert set(p) == {"BTCUSDT", "SOLUSDT", "ADAUSDT"}
    assert (p["SOLUSDT"]["tick"], p["SOLUSDT"]["step"]) == (Decimal("0.01"), Decimal("0.01"))
    assert (p["ADAUSDT"]["tick"], p["ADAUSDT"]["step"]) == (Decimal("0.00001"), Decimal("1"))
    assert p["BTCUSDT"]["market"] == "BTC_USDT" and parse("nonsense") == {}


def test_config_first_then_exchange_info_and_nothing_guessed():
    cfg = {"BTCUSDT": (Decimal("0.1"), Decimal("0.001"))}
    ins = Instruments(MARKETS, cfg, fetch=lambda: INFO)
    assert list(ins) == ["BTCUSDT"] and "SOLUSDT" not in ins  # before exchangeInfo answers
    assert ins.describe("SOLUSDT")["known"] is False and ins.describe("SOLUSDT")["listed"] is None
    assert ins.refresh()
    assert ins["BTCUSDT"] == (Decimal("0.1"), Decimal("0.001"))  # the config wins
    assert ins["SOLUSDT"] == (Decimal("0.01"), Decimal("0.01"))
    assert list(ins) == ["BTCUSDT", "SOLUSDT", "ADAUSDT"] and len(ins) == 3
    assert "NEARUSDT" not in ins and ins.describe("NEARUSDT")["listed"] is False
    assert ins.describe("BTCUSDT")["source"] == "config"
    assert ins.describe("ADAUSDT")["source"] == "exchangeInfo"
    with pytest.raises(KeyError):
        ins["ETHUSDT"]  # not a dashboard market, even if Tabdeal lists it


def test_a_failed_refresh_keeps_what_was_known_and_says_why():
    answers = [INFO]

    def fetch():
        if not answers:
            raise OSError("timed out")
        return answers.pop()

    ins = Instruments(MARKETS, {}, fetch=fetch)
    assert ins.refresh() and "SOLUSDT" in ins
    assert not ins.refresh()
    assert "SOLUSDT" in ins and "timed out" in (ins.problem or "")


def test_run_refreshes_until_stopped():
    calls = []
    stop = threading.Event()

    def fetch():
        calls.append(1)
        stop.set()
        return INFO

    Instruments(MARKETS, {}, fetch=fetch).run(stop)
    assert calls == [1]
