"""V5.9 live forming M1: display only, built from canonical trades as they arrive.

It must follow the trades exactly (O = first by exchange time, H/L extremes, C = latest,
V/N sums), be replaced by the final canonical candle at finalization (never duplicated), and
have zero influence on anything the strategy consumes."""

from __future__ import annotations

import ast
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

from sp2l.marketdata.collector import Collector, CollectorConfig
from tests.conftest import T0
from tests.unit.test_collector import Sink, frame

SRC = Path(__file__).resolve().parents[2] / "src" / "sp2l"


def at(s: float):
    return T0 + timedelta(seconds=s)


def make():
    sink = Sink()
    sink.live = []
    col = Collector(CollectorConfig("BTCUSDT"), sink, clock=lambda: at(0))
    col.session_started(at(0), "A")
    col.pong(at(0), at(0.1), "A")
    return col, sink


def trade(col, seq, t, px, amount="0.001"):
    col.ingest(frame(seq, at(t), price=px, amount=amount), at(t + 0.05), "A")


def last_live(sink):
    return [e for e in sink.live if e["type"] == "trade"][-1]


def test_forming_candle_follows_each_trade():
    col, sink = make()
    trade(col, 1, 61.0, "100")  # 1. first trade creates the forming candle
    e = last_live(sink)
    assert (e["t"], e["o"], e["h"], e["l"], e["c"], e["n"]) == (
        at(60).isoformat(),
        "100",
        "100",
        "100",
        "100",
        1,
    )
    trade(col, 2, 62.0, "105")  # 2. higher trade: H and C
    e = last_live(sink)
    assert (e["o"], e["h"], e["l"], e["c"]) == ("100", "105", "100", "105")
    trade(col, 3, 63.0, "95")  # 3. lower trade: L and C
    e = last_live(sink)
    assert (e["o"], e["h"], e["l"], e["c"]) == ("100", "105", "95", "95")
    trade(col, 4, 64.0, "101", amount="0.004")  # 4. ordinary trade: only C, V, N
    e = last_live(sink)
    assert (e["o"], e["h"], e["l"], e["c"], e["n"]) == ("100", "105", "95", "101", 4)
    assert D(e["v"]) == D("0.007") and e["price"] == "101" and e["src"] == "WS"
    assert e["recv_ts"] == at(64.05).isoformat()  # for trade -> WebUI latency


def test_out_of_order_arrival_keeps_open_and_close_by_exchange_time():
    col, sink = make()
    trade(col, 1, 61.0, "100")
    trade(col, 3, 61.5, "102")
    trade(col, 2, 60.5, "99")  # earlier exchange time arriving later: becomes the open
    e = last_live(sink)
    assert (e["o"], e["c"], e["l"]) == ("99", "102", "99")


def test_minute_close_replaces_the_forming_candle_with_the_final_one_no_duplicate():
    col, sink = make()
    for i, (t, px) in enumerate([(61, "100"), (70, "103"), (90, "98"), (110, "101")]):
        trade(col, i + 1, t, px)
        col.pong(at(t + 0.2), at(t + 0.3), "A")
    col.pong(at(125), at(125.1), "A")
    col.advance(at(125.2))
    finals = [e for e in sink.live if e["type"] == "final" and e["t"] == at(60).isoformat()]
    assert len(finals) == 1  # 7. exactly one final for the minute
    f = finals[0]
    m1 = next(m for m in sink.m1 if m.open_time == at(60))
    assert (f["o"], f["h"], f["l"], f["c"], f["n"]) == (
        str(m1.candle.open),
        str(m1.candle.high),
        str(m1.candle.low),
        str(m1.candle.close),
        4,
    )
    assert at(60) not in col.forming  # the display view of that minute is gone
    forming = last_live(sink)
    assert (forming["o"], forming["h"], forming["l"], forming["c"]) == (
        f["o"],
        f["h"],
        f["l"],
        f["c"],
    )  # the live view converged to the canonical values


def test_forming_candle_never_reaches_the_strategy():
    """5/6: the M1 results the strategy consumes are identical with or without anyone
    reading the live view, and no strategy/engine/indicator module references it."""
    a, sa = make()
    b, sb = make()
    for col in (a, b):
        for i in range(40):
            trade(col, i + 1, 61 + i * 3, str(100 + (i % 5)))
            col.pong(at(62 + i * 3), at(62.1 + i * 3), "A")
        col.pong(at(200), at(200.1), "A")
    b.forming.clear()  # tamper with the display view: must change nothing downstream
    b.forming[at(120)] = {"o": D(1), "h": D(1), "l": D(1), "c": D(1), "v": D(1), "n": 1}
    a.advance(at(201))
    b.advance(at(201))
    assert [(m.open_time, m.candle) for m in sa.m1] == [(m.open_time, m.candle) for m in sb.m1]
    for pkg in ("strategy", "engine", "indicators", "counterfactual", "execution"):
        for f in (SRC / pkg).rglob("*.py"):
            names = {
                n.attr if isinstance(n, ast.Attribute) else getattr(n, "id", None)
                for n in ast.walk(ast.parse(f.read_text()))
            }
            assert "forming" not in names and "on_live" not in names, f
    assert "forming" not in (SRC / "marketdata" / "m1_builder.py").read_text()


def test_rest_only_trade_updates_the_forming_candle():
    from sp2l.marketdata.tabdeal_public import RestTrade

    sink = Sink()
    sink.live = []
    col = Collector(
        CollectorConfig("BTCUSDT", rest_enabled=True, repair_enabled=True),
        sink,
        clock=lambda: at(0),
    )
    col.session_started(at(0), "A")
    col.pong(at(0), at(0.1), "A")
    trade(col, 1, 61.0, "100")
    col.ingest_rest([RestTrade(at(62.0), D("108"), D("0.001"), "BUY", {})], at(65), at(65))
    e = last_live(sink)
    assert e["src"] == "REST" and e["h"] == "108" and e["n"] == 2


def test_restarted_collector_continues_the_forming_candle_from_stored_trades():
    """After a collector restart the live view continues the minute (it never restarts at
    n=1), so a browser holding the stored snapshot keeps updating."""
    from sp2l.marketdata.m1_builder import Trade

    stored = [
        Trade(f"s{i}", at(60 + i), at(60 + i), D(px), D("0.001"))
        for i, px in enumerate(["100", "104", "98"])
    ]
    sink = Sink()
    sink.live = []
    col = Collector(
        CollectorConfig("BTCUSDT"),
        sink,
        clock=lambda: at(0),
        resume_from=at(60),
        resume_trades=stored,
    )
    assert sink.live == []  # seeding publishes nothing
    col.session_started(at(64), "A")
    col.pong(at(64), at(64.1), "A")
    trade(col, 9, 65.0, "101")
    e = last_live(sink)
    assert (e["o"], e["h"], e["l"], e["c"], e["n"]) == ("100", "104", "98", "101", 4)
