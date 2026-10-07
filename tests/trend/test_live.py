# ruff: noqa: E501  (synthetic bar rows are clearer on one line)
"""System B paper state: same engine as the backtest, paper start, next-open decision."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from sp2l.core.types import Candle
from sp2l.trend import backtest as tb
from sp2l.trend import live as tl
from sp2l.trend.model import TrendParams

T0 = datetime(2026, 1, 1, tzinfo=UTC)
P = TrendParams(entry_len=5, exit_len=3, atr_len=3, risk_pct=0.05)


def daily(rows):
    return [Candle(T0 + timedelta(days=i), D(str(o)), D(str(h)), D(str(lo)), D(str(c)), D(1)) for i, (o, h, lo, c) in enumerate(rows)]


FLAT = [(100, 101, 99, 100)] * 12


def cfg(start_day: int) -> tl.LiveConfig:
    return tl.LiveConfig("BTCUSDT", (T0 + timedelta(days=start_day)).date(), 100.0, 0.001, 0.0003)


def test_breakout_on_the_last_close_is_a_buy_for_the_next_open():
    bars = daily(FLAT + [(100, 106, 99.5, 105)])
    s = tl.state(bars, P, cfg(5))
    assert s["ready"] and s["action"] == "BUY" and s["position"] is None
    assert s["next"]["at"] == (T0 + timedelta(days=13)).isoformat()
    atr = tb.wilder_atr([float(b.high) for b in bars], [float(b.low) for b in bars], [float(b.close) for b in bars], 3)[-1]
    assert s["next"]["est_stop"] == pytest.approx(105 - 2 * atr)
    assert s["next"]["risk_usdt"] == pytest.approx(5.0)  # 5 % of 100
    assert s["entry_level"] == 106 and s["exit_level"] == 99


def test_after_the_fill_the_position_is_held_and_matches_the_backtest():
    rows = FLAT + [(100, 106, 99.5, 105), (105.5, 107, 105, 106.5), (106.5, 108, 106, 107.5)]
    bars = daily(rows)
    s = tl.state(bars, P, cfg(5))
    assert s["action"] == "HOLD" and s["position"]["entry_day"] == (T0 + timedelta(days=13)).date().isoformat()
    r = tb.run(bars, TrendParams(entry_len=5, exit_len=3, atr_len=3, risk_pct=0.05, initial_equity=100.0), tb.Fees(0.001, 0.0003))
    assert s["equity"] == pytest.approx(r.equity[-1]) and s["position"]["stop"] == pytest.approx(r.stop)


def test_no_entry_before_the_paper_start():
    bars = daily(FLAT + [(100, 106, 99.5, 105), (105.5, 107, 105, 106.5)] + [(106.5, 106.6, 106, 106.2)] * 3)
    late = tl.state(bars, P, cfg(16))  # the breakout and its fill are before the start
    assert late["position"] is None and late["equity"] == 100.0 and late["action"] == "WAIT"
    early = tl.state(bars, P, cfg(5))
    assert early["position"] is not None


def test_exit_signal_is_a_sell_and_closed_trades_are_listed():
    up = [(100 + i, 101.2 + i, 99.8 + i, 101 + i) for i in range(8)]
    rows = FLAT + up + [(108, 108.2, 104, 104.5)]  # closes below the 3-day low
    s = tl.state(daily(rows), P, cfg(5))
    assert s["action"] == "SELL" and s["next"]["qty"] > 0
    s2 = tl.state(daily(rows + [(104, 104.5, 103, 103.5)]), P, cfg(5))
    assert s2["position"] is None and len(s2["trades"]) == 1 and s2["trades"][0]["reason"] == "CHANNEL"


def test_not_ready_with_too_few_bars_and_config_validation():
    assert tl.state(daily(FLAT[:4]), P, cfg(0))["ready"] is False
    with pytest.raises(ValueError):
        tl.LiveConfig.from_mapping({"symbol": "BTCUSDT"}, 0.001, 0.0)  # paper_start missing
    with pytest.raises(ValueError):
        tl.LiveConfig.from_mapping({"paper_start": "2026-10-08", "risk": 1}, 0.001, 0.0)
    c = tl.LiveConfig.from_mapping({"paper_start": "2026-10-08"}, 0.001, 0.0)
    assert c.paper_start == date(2026, 10, 8) and c.account_usdt == 100.0
