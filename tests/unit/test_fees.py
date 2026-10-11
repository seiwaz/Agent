"""Tabdeal's current commission (sp2l.trading.fees): the level by 30-day volume from its public
table, the config's fees while it does not answer, never waiting for Tabdeal on a request."""

from __future__ import annotations

import threading

import pytest

from sp2l.trading.fees import Fees, pick_level

TABLE = {"special_margin_commission_levels": [
    {"level": 1, "name": "Level 1", "maker_commission_percent": "0.0008",
     "taker_commission_percent": "0.00095", "from_volume": "0", "to_volume": "1000000"},
    {"level": 2, "name": "Level 2", "maker_commission_percent": "0.0007",
     "taker_commission_percent": "0.0009", "from_volume": "1000000", "to_volume": "5000000"},
]}


def test_the_level_is_picked_by_volume():
    assert pick_level(TABLE, 0) == {"maker": 0.0008, "taker": 0.00095, "level": "Level 1"}
    assert pick_level(TABLE, 1_000_000)["maker"] == 0.0007
    with pytest.raises(ValueError):
        pick_level(TABLE, 9_000_000)


def test_tabdeal_first_the_config_while_it_fails_and_a_request_never_waits():
    clock = [1000.0]
    f = Fees(0.001, 0.002, fetch=lambda: TABLE, clock=lambda: clock[0])
    assert f.refresh() == {"maker": 0.0008, "taker": 0.00095, "level": "Level 1",
                           "source": "tabdeal", "fetched_at": 1000.0}

    def down():
        raise OSError("unreachable")

    g = Fees(0.001, 0.002, fetch=down, clock=lambda: clock[0])
    v = g.refresh()
    assert (v["maker"], v["taker"], v["source"]) == (0.001, 0.002, "config")
    assert "unreachable" in v["problem"]
    gate, done = threading.Event(), threading.Event()

    def slow():
        gate.wait(5)
        done.set()
        return TABLE

    h = Fees(0.001, 0.002, fetch=slow, clock=lambda: clock[0])
    assert h.get()["source"] == "config"  # Tabdeal not answered yet: the config, at once
    gate.set()
    assert done.wait(5)
    for _ in range(100):
        if h.get()["source"] == "tabdeal":
            break
        threading.Event().wait(0.01)
    assert h.get()["source"] == "tabdeal"
