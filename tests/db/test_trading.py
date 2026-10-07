"""Trading from the chart against a simulated Tabdeal (tests/trading/fake_exchange.py): the
entry order, the stop / target once filled (again after more fills), the live position, every way
a trade ends (target, stop, closed here, closed on Tabdeal, canceled), the safety refusals, a
lost order reply, and the API's guards (switch, token, origin)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from sp2l.trading.client import ExchangeError
from sp2l.trading.manager import TradeError, TradeManager, TradingConfig
from tests.trading.fake_exchange import FakeExchange

pytestmark = pytest.mark.db
INSTR = {"BTCUSDT": (Decimal("0.1"), Decimal("0.00001")), "XRPUSDT": (Decimal("0.00001"), Decimal("0.1"))}
LONG = {"symbol": "BTCUSDT", "side": "LONG", "entry": 60000, "sl": 59000, "tp": 63000,
        "leverage": 10, "margin_usdt": 30, "drawing_id": "d1"}
M = "BTC_USDT"


@pytest.fixture()
def ex() -> FakeExchange:
    return FakeExchange(wallet="200")


@pytest.fixture()
def tm(engine, ex) -> TradeManager:
    with engine.begin() as c:
        c.execute(text("DELETE FROM manual_trades"))
    return TradeManager(engine, ex, TradingConfig(enabled=True), INSTR)


def test_long_fills_gets_its_stop_and_target_and_ends_at_the_target(tm, ex):
    t = tm.open(LONG)
    assert t["status"] == "PENDING" and t["qty"] == pytest.approx(0.005)  # 30 x 10 / 60000
    assert ex.leverage[M] == 10 and ex.names().index("set_leverage") < ex.names().index("limit_order")
    _, side, qty, price, cid = next(a for n, a in ex.calls if n == "limit_order")
    assert (side, qty, price) == ("BUY", "0.00500", "60000.0") and cid == t["client_id"]
    tm.reconcile()
    assert tm.trades("BTCUSDT", "open")[0]["status"] == "PENDING"
    ex.fill(t["order_id"])
    ex.mark[M] = Decimal("61000")
    tm.reconcile()
    a = tm.trades("BTCUSDT", "open")[0]
    pos = ex.position[M]
    assert a["status"] == "ACTIVE" and a["protected"] and a["position_id"] == pos["id"]
    assert ex.calls[-1 - [n for n, _ in reversed(ex.calls)].index("position_sl_tp")][1][2:] == (
        "59000.0", "63000.0", "MARK_PRICE")  # sent at the market's precision
    assert a["live"]["upnl"] == pytest.approx(5.0) and a["live"]["roe_pct"] == pytest.approx(16.6667, rel=1e-3)
    n = ex.names().count("position_sl_tp")
    tm.reconcile()
    assert ex.names().count("position_sl_tp") == n  # protected once per filled quantity
    ex.end(M, "63000")
    tm.reconcile()
    assert tm.trades("BTCUSDT", "open") == []
    h = tm.trades("BTCUSDT", "history")[0]
    assert (h["status"], h["close_reason"], h["exit_price"]) == ("CLOSED", "TP", 63000.0)
    assert h["realized_pnl"] == pytest.approx(15.0)


def test_partial_fills_are_protected_as_they_grow_and_a_cancel_keeps_the_filled_part(tm, ex):
    t = tm.open({**LONG, "side": "SHORT", "sl": 61000, "tp": 57000})
    ex.fill(t["order_id"], "0.002")
    tm.reconcile()
    a = tm.trades(None, "open")[0]
    assert a["status"] == "ACTIVE" and a["filled_qty"] == pytest.approx(0.002)
    assert ex.position[M]["sltp_amt"] == Decimal("0.002")
    ex.fill(t["order_id"], "0.001")
    tm.reconcile()
    assert ex.position[M]["sltp_amt"] == Decimal("0.003")  # the stop / target were set again
    with pytest.raises(TradeError, match="only a pending"):
        tm.cancel(t["id"])
    c = tm.close(t["id"])  # the unfilled rest is canceled first, then the position closes
    assert ex.orders[t["order_id"]]["status"] == "CANCELED"
    assert c["status"] == "CLOSED" and c["close_reason"] == "MANUAL"


def test_cancel_a_pending_trade_and_a_stop_out(tm, ex):
    t = tm.open(LONG)
    c = tm.cancel(t["id"])
    assert c["status"] == "CANCELED" and ex.orders[t["order_id"]]["status"] == "CANCELED"
    t = tm.open(LONG)  # the market is free again
    ex.fill(t["order_id"])
    tm.reconcile()
    ex.end(M, "59000")
    tm.reconcile()
    h = tm.trades("BTCUSDT", "history")[0]
    assert (h["id"], h["close_reason"], h["realized_pnl"]) == (t["id"], "SL", pytest.approx(-5.0))


def test_closed_on_tabdeal_and_a_resting_remainder_is_canceled(tm, ex):
    t = tm.open(LONG)
    ex.fill(t["order_id"], "0.001")
    tm.reconcile()
    ex.end(M, "60500")  # the user closed it in Tabdeal's app
    tm.reconcile()
    h = tm.trades(None, "history")[0]
    assert h["close_reason"] == "CLOSED_ON_TABDEAL"
    assert ex.orders[t["order_id"]]["status"] == "CANCELED"  # would open a new position


def test_order_cancelled_on_tabdeal(tm, ex):
    t = tm.open(LONG)
    ex.orders[t["order_id"]]["status"] = "CANCELED"
    tm.reconcile()
    assert tm.trades(None, "history")[0]["status"] == "CANCELED"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"sl": 61000}, "stop < entry < target"),
        ({"side": "SHORT"}, "target < entry < stop"),
        ({"leverage": 21}, "leverage must be 1..20"),
        ({"margin_usdt": 51}, "at most 50"),
        ({"margin_usdt": 0}, "above 0"),
        ({"margin_usdt": 0.001, "leverage": 1}, "less than one step"),
        ({"symbol": "ETHUSDT"}, "symbol must be one of"),
        ({"entry": "abc"}, "not a number"),
        ({"leverage": 20, "margin_usdt": 50, "sl": 45000}, "liquidated before the stop"),
    ],
)
def test_refusals(tm, ex, change, message):
    with pytest.raises(TradeError, match=message):
        tm.open({**LONG, **change})
    assert "limit_order" not in ex.names()


def test_refused_while_the_market_is_busy(tm, ex):
    tm.open(LONG)
    with pytest.raises(TradeError, match="already open here"):
        tm.open(LONG)
    with tm.db.begin() as c:
        c.execute(text("DELETE FROM manual_trades"))
    with pytest.raises(TradeError, match="open orders on Tabdeal"):
        tm.open(LONG)
    ex.orders.clear()
    ex.position[M] = {"id": 1, "market": M, "amt": Decimal("0.01"), "entry": Decimal(1), "lev": 1}
    with pytest.raises(TradeError, match="position on Tabdeal"):
        tm.open(LONG)


def test_insufficient_margin_and_an_exchange_rejection(tm, ex):
    ex.wallet = Decimal("20")
    with pytest.raises(TradeError, match="available"):
        tm.open(LONG)
    ex.wallet = Decimal("200")
    ex.fail["limit_order"] = ExchangeError("Margin is insufficient.", -2019, 400)
    r = tm.open(LONG)
    assert r["status"] == "REJECTED" and "insufficient" in r["last_error"]


def test_a_lost_order_reply_is_found_by_its_client_id(tm, ex):
    ex.lost_reply = True
    t = tm.open(LONG)
    assert t["status"] == "PENDING" and t["order_id"] is not None  # found at once
    ex.fill(t["order_id"])
    tm.reconcile()
    assert tm.trades(None, "open")[0]["protected"]


def test_an_order_that_never_arrived_is_given_up_after_a_while(tm, ex):
    ex.fail["limit_order"] = ExchangeError("exchange unreachable: TimeoutError")
    t = tm.open(LONG)
    assert t["status"] == "PENDING" and t["order_id"] is None  # not seen yet: keep looking
    tm.reconcile()
    assert tm.trades(None, "open")[0]["status"] == "PENDING"
    with tm.db.begin() as c:
        c.execute(text("UPDATE manual_trades SET created_at = now() - interval '1 minute'"))
    tm.reconcile()
    assert tm.trades(None, "history")[0]["status"] == "REJECTED"


def test_a_refused_stop_is_retried(tm, ex):
    t = tm.open(LONG)
    ex.fill(t["order_id"])
    ex.fail["position_sl_tp"] = ExchangeError("try again", 1, 400)
    tm.reconcile()
    a = tm.trades(None, "open")[0]
    assert not a["protected"] and "try again" in a["last_error"]
    tm.reconcile()
    assert tm.trades(None, "open")[0]["protected"]


# ---- the API (behind the dashboard login) ------------------------------------------------------
ORIGIN = {"Origin": "http://dash.local"}


def _client(engine, tmp_path: Path, tm: TradeManager | None, enabled: bool = True,
            login: bool = True):
    from sp2l.api.app import create_app
    from sp2l.api.auth import set_login
    from sp2l.config import RuntimeConfig
    from sp2l.trading.api import TradingDesk
    from tests.db.conftest import URL

    users = tmp_path / "auth" / "dashboard.auth"
    set_login(users, "admin", "correct horse")
    cfg = RuntimeConfig({
        "database_url": URL, "symbols": ["BTCUSDT", "XRPUSDT"],
        "trading": {"enabled": enabled},
        "auth": {"enabled": login, "users_file": str(users)},
    })
    desk = TradingDesk(cfg, engine, manager=tm)
    return TestClient(create_app(cfg, trading=desk), base_url="http://dash.local")


def _login(c, password: str = "correct horse"):
    return c.post("/login", data={"user": "admin", "password": password}, headers=ORIGIN,
                  follow_redirects=False)


def test_api_needs_the_login_and_a_trade_round_trip(engine, tm, ex, tmp_path):
    c = _client(engine, tmp_path, tm)
    assert c.get("/api/trade/trades").status_code == 401  # no session
    assert c.get("/api/overview").status_code == 401
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/login").status_code == 200 and c.get("/api/health").status_code == 200
    assert _login(c, "wrong").status_code == 401
    r = _login(c)
    assert r.status_code == 303 and "ets_session" in r.cookies
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc
    assert c.post("/api/trade/open", json=LONG).status_code == 403  # no Origin: not the dashboard
    assert c.post("/api/trade/open", json=LONG, headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/smc/signals", headers=ORIGIN).status_code == 405
    r = c.post("/api/trade/open", json=LONG, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["status"] == "PENDING"
    bad = c.post("/api/trade/open", json=LONG, headers=ORIGIN)
    assert bad.status_code == 400 and "already open" in bad.json()["detail"]
    acc: Any = c.get("/api/trade/account?symbol=BTCUSDT").json()
    assert acc["busy"] and acc["wallet_usdt"] == 200
    tid = r.json()["id"]
    assert c.post(f"/api/trade/{tid}/close", headers=ORIGIN).status_code == 400  # pending
    assert c.post(f"/api/trade/{tid}/cancel", headers=ORIGIN).json()["status"] == "CANCELED"
    hist = c.get("/api/trade/trades?scope=history").json()["items"]
    assert [x["id"] for x in hist] == [tid]
    r = c.post("/logout", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/api/trade/trades").status_code == 401  # signed out


def test_wrong_passwords_are_throttled_and_a_forged_cookie_is_refused(engine, tmp_path):
    c = _client(engine, tmp_path, None, enabled=False)
    for _ in range(5):
        assert _login(c, "nope").status_code == 401
    r = _login(c)  # the right password, but too many wrong ones from this address
    assert r.status_code == 401 and "Too many" in r.text
    c2 = _client(engine, tmp_path / "b", None, enabled=False)
    good = _login(c2).cookies["ets_session"]
    user, exp, sig = good.split(".")
    c3 = _client(engine, tmp_path / "c", None, enabled=False)
    c3.cookies.set("ets_session", f"{user}.{int(exp) + 99999}.{sig}")  # a longer life: refused
    assert c3.get("/api/overview").status_code == 401


def test_trading_off_and_trading_without_login(engine, tmp_path):
    c = _client(engine, tmp_path, None, enabled=False)
    _login(c)
    r = c.get("/api/trade/trades")
    assert r.status_code == 403 and "trading.enabled" in r.json()["detail"]
    c = _client(engine, tmp_path / "b", None, enabled=True, login=False)
    cfg = c.get("/api/trade/config").json()
    assert not cfg["ready"] and "auth.enabled" in cfg["problem"]


def test_an_unreadable_key_file_turns_trading_off_but_the_dashboard_runs(engine, tmp_path):
    from sp2l.config import RuntimeConfig
    from sp2l.trading.api import TradingDesk

    d = tmp_path / "sp2l"
    d.mkdir(mode=0o700)
    env = d / "tabdeal.env"
    env.write_text("SP2L_TABDEAL_API_KEY=k\nSP2L_TABDEAL_API_SECRET=s\n")
    env.chmod(0o600)
    real = Path.read_text

    def denied(self: Path, *a: Any, **k: Any) -> str:
        if self == env:
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    mp = pytest.MonkeyPatch()
    mp.setattr(Path, "read_text", denied)
    try:
        cfg = RuntimeConfig({"database_url": "x", "symbols": ["BTCUSDT"],
                             "trading": {"enabled": True, "credentials_file": str(env)}})
        desk = TradingDesk(cfg, engine)
    finally:
        mp.undo()
    assert desk.manager is None and "Permission denied" in (desk.problem or "")
