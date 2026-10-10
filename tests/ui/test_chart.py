# ruff: noqa: E501  (selectors and in-page scripts are clearer on one line)
"""The chart workspace in a real browser, served by the real API with a stand-in for Tabdeal's
chart feed (tests/chart/fake_tabdeal.py): scrolling back loads older pages, timeframes, the per-timeframe trend strip, every feature switched on
and off on its own, indicators in their own panes, settings applied and kept, a timeframe change
re-applying the active features, full screen, and no page errors (Playwright)."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.chart.fake_tabdeal import FakeTabdeal
from tests.db.conftest import URL, engine  # noqa: F401  (module-scoped fresh schema)

pytestmark = [pytest.mark.db, pytest.mark.browser]
SYM = "BTCUSDT"


@pytest.fixture(scope="module")
def data(engine) -> Iterator[FakeTabdeal]:  # noqa: F811
    """The chart reads Tabdeal's chart feed: a deterministic stand-in, listed 200 days ago."""
    import sp2l.chart.history as hist

    with engine.begin() as c:
        c.execute(text("DELETE FROM chart_bars"))
    fake = FakeTabdeal()
    mp = pytest.MonkeyPatch()
    mp.setattr(hist, "plots", fake)
    yield fake
    mp.undo()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# exchangeInfo for the markets beyond the config's instruments (SOL: 0.01 price, 0.01 quantity)
INFO = {"symbols": [{"symbol": "XRP_USDT", "pricePrecision": 5, "quantityPrecision": 1},
                    {"symbol": "SOL_USDT", "pricePrecision": 2, "quantityPrecision": 2}]}


@contextmanager
def serve(trading: dict | None = None, exchange=None, auth: dict | None = None,
          markets: list[str] | None = None) -> Iterator[str]:
    import uvicorn

    from sp2l.api.app import create_app
    from sp2l.config import RuntimeConfig
    from sp2l.marketdata.instruments import Instruments

    cfg = RuntimeConfig(
        {
            "database_url": URL,
            "symbols": [SYM],
            "markets": markets or [SYM],
            "instruments": {SYM: {"tick": "0.1", "step": "0.001"}},
            "costs": {"maker_fee": "0", "taker_fee": "0", "slippage_allowance": "0"},
            "smc": {"htf_grid": "utc"},
            "trading": trading or {},
            "auth": auth or {},
        }
    )
    ins = Instruments(cfg.markets, {SYM: cfg.instrument(SYM)}, fetch=lambda: INFO)
    ins.refresh()
    desk = None
    if exchange is not None:  # trading against a simulated Tabdeal
        from sqlalchemy import create_engine

        from sp2l.trading.api import TradingDesk
        from sp2l.trading.manager import TradeManager, TradingConfig

        tc = TradingConfig.from_mapping(trading or {})
        tm = TradeManager(create_engine(URL), exchange, tc, ins)
        desk = TradingDesk(cfg, tm.db, manager=tm)
    port = _free_port()
    srv = uvicorn.Server(
        uvicorn.Config(create_app(cfg, trading=desk, instruments=ins), host="127.0.0.1", port=port,
                       log_level="warning")
    )
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        th.join(timeout=10)


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        tries = [
            lambda: pw.chromium.launch(),
            *[
                (lambda exe=exe: pw.chromium.launch(executable_path=exe))
                for exe in sorted(str(x) for x in Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"))
            ],
            lambda: pw.webkit.launch(),
        ]
        b = None
        for launch in tries:
            try:
                b = launch()
                break
            except Exception:  # pragma: no cover
                continue
        if b is None:  # pragma: no cover
            pytest.skip("no Playwright browser installed (uv run playwright install chromium)")
        yield b
        b.close()


def _open(browser, url: str):
    pg = browser.new_page(viewport={"width": 1500, "height": 950})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f"{url}/#/chart")
    pg.wait_for_function("() => state.ws && state.ws.bars.length > 0 && state.ws.overlays", timeout=60000)
    return pg, errors


def _items(pg) -> dict:
    return pg.evaluate(
        "() => { const i = state.ws.layer.items; return { boxes: i.boxes.length, lines: i.lines.length, marks: i.marks.length }; }"
    )


def test_workspace_features_panes_settings_and_full_screen(data, browser):
    with serve() as url:
        pg, errors = _open(browser, url)
        pg.evaluate("() => localStorage.removeItem('smc-chart-v1')")
        pg.reload()
        pg.wait_for_function("() => state.ws && state.ws.bars.length > 0 && state.ws.overlays && state.ws.overlays.ready", timeout=60000)

        # timeframes and the trend strip (top bar), green / red per timeframe
        assert pg.eval_on_selector_all(".ws-tfs button", "bs => bs.map(b => b.textContent)") == ["5m", "15m", "1h", "4h", "1d"]
        pg.wait_for_function("() => document.querySelectorAll('#tf-trends .tf-trend').length === 5")
        assert all(c in ("tf-trend t-bull", "tf-trend t-bear", "tf-trend t-none")
                   for c in pg.eval_on_selector_all("#tf-trends .tf-trend", "xs => xs.map(x => x.className)"))

        # default: structure and levels drawn, no indicator pane
        assert pg.evaluate("() => state.ws.chart.panes().length") == 1
        base = _items(pg)
        assert base["marks"] > 0 and base["boxes"] + base["lines"] > 0

        # each structure feature switches off on its own
        pg.click(".ws-layers-btn")
        pg.uncheck("#ws-f-swings")
        assert _items(pg)["marks"] == 0
        pg.check("#ws-f-swings")
        assert _items(pg) == base

        # indicators: Donchian on the price pane, RSI and MACD each in their own pane
        for f in ("donchian", "rsi", "macd"):
            pg.check(f"#ws-f-{f}")
        assert pg.evaluate("() => state.ws.chart.panes().length") == 3
        pg.uncheck("#ws-f-rsi")
        assert pg.evaluate("() => state.ws.chart.panes().length") == 2

        # a setting applies at once and is kept
        pg.check("#ws-f-rsi")
        pg.click("#ws-f-rsi >> xpath=ancestor::div[contains(@class,'ws-item')]//button[contains(@class,'ws-gear')]")
        inp = pg.locator("#ws-form-rsi input[type=number]").first
        inp.fill("21")
        inp.dispatch_event("change")
        assert pg.evaluate("() => JSON.parse(localStorage.getItem('smc-chart-v1')).features.rsi.s.length") == 21
        assert pg.evaluate("() => state.ws.cfg.features.rsi.s.length") == 21

        # a timeframe change re-applies every active feature to that timeframe
        pg.click(".ws-tfs button:has-text('4h')")
        pg.wait_for_function("() => state.ws.overlays && state.ws.overlays.tf === '4h'", timeout=30000)
        assert pg.evaluate("() => state.ws.chart.panes().length") == 3
        assert pg.evaluate("() => state.ws.tf") == "4h"

        # mitigated order blocks only on request
        pg.click(".ws-tfs button:has-text('15m')")
        pg.wait_for_function("() => state.ws.overlays && state.ws.overlays.tf === '15m'", timeout=30000)
        n_valid = pg.evaluate("() => state.ws.overlays.zones.filter(z => z.kind === 'OB' && z.valid).length")
        n_all = pg.evaluate("() => state.ws.overlays.zones.filter(z => z.kind === 'OB').length")
        pg.uncheck("#ws-f-fvg")
        pg.uncheck("#ws-f-sr")
        assert _items(pg)["boxes"] == n_valid
        pg.evaluate("() => { state.ws.cfg.features.ob.s.history = true; state.ws.redraw(); }")
        assert _items(pg)["boxes"] == n_all

        # full screen keeps the toolbar and the options
        pg.click(".chart-ws .icon-btn")
        assert pg.evaluate("() => state.ws.isFull()")
        assert pg.is_visible(".ws-layers-btn") and pg.is_visible(".ws-tfs")
        pg.click(".chart-ws .icon-btn")
        assert not pg.evaluate("() => state.ws.isFull()")
        assert errors == []
        pg.close()


def test_drawing_tools_create_edit_delete_and_persist(data, browser):
    with serve() as url:
        pg, errors = _open(browser, url)
        pg.evaluate("() => localStorage.removeItem('smc-drawings-v1')")
        pg.reload()
        pg.wait_for_function("() => state.ws && state.ws.bars.length > 0", timeout=60000)
        box = pg.locator(".ws-chart").bounding_box()
        X, Y = box["x"], box["y"]

        def click(x, y, n=1):
            pg.mouse.move(X + x, Y + y)
            pg.mouse.click(X + x, Y + y, click_count=n)

        def tool(name):
            pg.click(f".ws-draw button[aria-label='{name}']")

        assert pg.eval_on_selector_all(".ws-draw button", "bs => bs.map(b => b.getAttribute('aria-label'))") == [
            "Trend line", "Horizontal line", "Long position", "Short position", "Price range", "Path",
            "Vertical line", "Date range", "Anchored VWAP", "Fib retracement",
            "Trade on Tabdeal: the selected (or last) Long / Short position", "Remove all drawings"]
        pg.evaluate("() => state.ws.trading.setCollapsed(true)")  # the whole height for the chart
        pg.wait_for_timeout(300)
        tool("Trend line")
        click(200, 500)
        click(500, 300)
        tool("Horizontal line")
        click(700, 250)
        tool("Long position")
        click(1000, 420)
        tool("Short position")
        click(1150, 300)
        tool("Price range")
        click(300, 200)
        click(420, 120)
        tool("Path")
        click(600, 600)
        click(700, 520)
        click(800, 580)
        click(800, 580, 2)
        types = pg.evaluate("() => state.ws.tools.items.map(i => i.type + (i.a ? i.a.length : ''))")
        assert types == ["trend2", "hline", "long", "short", "range2", "path3"]
        assert pg.evaluate("() => state.ws.tools.tool") is None  # a finished drawing ends the tool

        # the long position: entry below the target, stop below the entry; its target drags alone
        lp = pg.evaluate("() => state.ws.tools.items.find(i => i.type === 'long')")
        assert lp["sl"] < lp["entry"] < lp["tp"] and lp["t2"] > lp["t1"]
        sp = pg.evaluate("() => state.ws.tools.items.find(i => i.type === 'short')")
        assert sp["tp"] < sp["entry"] < sp["sl"]
        click(1010, 410)  # select it (inside its target zone)
        x1, ytp = pg.evaluate("() => { const t = state.ws.tools, it = t.items.find(i => i.type === 'long'); return [t.x(it.t1), t.y(it.tp)]; }")
        pg.mouse.move(X + x1, Y + ytp)
        pg.mouse.down()
        pg.mouse.move(X + x1, Y + ytp - 50, steps=5)
        pg.mouse.up()
        lp2 = pg.evaluate("() => state.ws.tools.items.find(i => i.type === 'long')")
        assert lp2["tp"] > lp["tp"] and lp2["entry"] == lp["entry"] and lp2["sl"] == lp["sl"]

        # select the horizontal line, delete it
        yh = pg.evaluate("() => state.ws.tools.y(state.ws.tools.items.find(i => i.type === 'hline').p)")
        click(900, yh)
        pg.keyboard.press("Delete")
        assert "hline" not in pg.evaluate("() => state.ws.tools.items.map(i => i.type)")

        # kept per market across a reload and on every timeframe
        pg.reload()
        pg.wait_for_function("() => state.ws && state.ws.bars.length > 0", timeout=60000)
        assert pg.evaluate("() => state.ws.tools.items.length") == 5
        pg.click(".ws-tfs button:has-text('15m')")
        pg.wait_for_function("() => state.ws.overlays && state.ws.overlays.tf === '15m'", timeout=30000)
        assert pg.evaluate("() => state.ws.tools.items.length") == 5

        # without a tool the chart still pans
        r0 = pg.evaluate("() => state.ws.chart.timeScale().getVisibleLogicalRange().from")
        pg.mouse.move(X + 900, Y + 120)
        pg.mouse.down()
        pg.mouse.move(X + 600, Y + 120, steps=8)
        pg.mouse.up()
        assert pg.evaluate("() => state.ws.chart.timeScale().getVisibleLogicalRange().from") != r0
        assert errors == []
        pg.close()


def test_scrolling_back_loads_older_bars_from_tabdeal(data, browser):
    with serve() as url:
        pg, errors = _open(browser, url)
        pg.click(".ws-tfs button:has-text('4h')")
        pg.wait_for_function("() => state.ws.tf === '4h' && state.ws.bars.length > 0 && state.ws.overlays && state.ws.overlays.tf === '4h'", timeout=30000)
        n0 = pg.evaluate("() => state.ws.bars.length")
        first0 = pg.evaluate("() => state.ws.bars[0].t")
        # scroll to the left edge, page after page, until the listing
        for _ in range(12):
            pg.evaluate("() => state.ws.chart.timeScale().setVisibleLogicalRange({ from: 0, to: 120 })")
            pg.wait_for_timeout(700)
            if not pg.evaluate("() => state.ws.more"):
                break
        ts = pg.evaluate("() => state.ws.bars.map(b => b.t)")
        assert len(ts) > n0 and ts[0] < first0
        assert ts == sorted(set(ts)) and all(b - a == 14400 for a, b in zip(ts, ts[1:], strict=False))
        assert not pg.evaluate("() => state.ws.more")  # stopped at the listing
        assert ts[0] - data.listed < 14400
        # the overlays cover the loaded history
        pg.wait_for_function(f"() => state.ws.overlays.bars >= {len(ts) - 1}", timeout=30000)
        assert errors == []
        pg.close()


def test_trade_from_a_long_drawing_through_the_panel(data, browser, engine, tmp_path):  # noqa: F811
    from decimal import Decimal

    from sp2l.api.auth import set_login
    from tests.trading.fake_exchange import FakeExchange

    with engine.begin() as c:
        c.execute(text("DELETE FROM manual_trades"))
    ex = FakeExchange(wallet="1000")
    users = tmp_path / "auth" / "dashboard.auth"
    set_login(users, "admin", "test password")
    trading = {"enabled": True, "poll_s": 1, "max_leverage": 20}
    with serve(trading, ex, {"enabled": True, "users_file": str(users)}) as url:
        # the dashboard opens on the login page; a wrong password is refused
        pg = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{url}/#/chart")
        assert "/login" in pg.url
        pg.fill("input[name=user]", "admin")
        pg.fill("input[name=password]", "nope")
        pg.click(".login-btn")
        assert "Wrong user or password" in pg.inner_text(".login")
        pg.fill("input[name=password]", "test password")
        pg.click(".login-btn")
        pg.wait_for_function("() => typeof state !== 'undefined' && state.ws && state.ws.bars.length > 0 && state.ws.trading && state.ws.trading.cfg && state.ws.trading.cfg.ready", timeout=60000)
        pg.evaluate("() => localStorage.removeItem('smc-drawings-v1')")

        # a Long drawing that price has already traded through: shaded by its outcome
        d = pg.evaluate("""() => {
            const b = state.ws.bars, k = b.length - 40, e = b[k].c;
            const it = { id: "L1", type: "long", t1: b[k].t, t2: b[b.length - 1].t + 3600 * 10, entry: e, sl: +(e * 0.9).toFixed(1), tp: +(e * 1.2).toFixed(1) };
            state.ws.tools.items.push(it); state.ws.tools.save(); state.ws.tools.redraw();
            return { it, out: state.ws.tools.outcome(it) };
        }""")
        assert d["out"] is not None and d["out"]["t0"] >= d["it"]["t1"]

        # Trade: the dialog asks for the leverage (and the margin), then places the order
        pg.click(".ws-trade-btn")
        pg.wait_for_selector(".ws-modal .ws-trade-dlg")  # an in-page window: shown in full screen too
        assert "Long" in pg.inner_text(".ws-trade-dlg h2")
        pg.fill(".ws-trade-dlg .ws-field input >> nth=0", "۵")  # Persian digits are accepted
        pg.fill(".ws-trade-dlg .ws-field input >> nth=1", "۲۰")
        pg.click(".ws-trade-dlg .ws-go")
        pg.wait_for_function("() => !document.querySelector('.ws-trade-dlg')", timeout=15000)
        pg.wait_for_selector(".ws-trade-table .b-pending", timeout=15000)
        (_, side, qty, price, _cid) = next(a for n, a in ex.calls if n == "limit_order")
        assert side == "BUY" and ex.leverage["BTC_USDT"] == 5
        assert Decimal(price) == Decimal(str(d["it"]["entry"])).quantize(Decimal("0.1"))

        # filled on Tabdeal: active, protected, live PnL, then closed from the panel
        oid = next(iter(ex.orders))
        ex.fill(oid)
        ex.mark["BTC_USDT"] = Decimal(price) * Decimal("1.01")
        pg.wait_for_selector(".ws-trade-table .b-active", timeout=15000)
        pg.wait_for_function("() => document.querySelector('.ws-trade-table').innerText.includes('✓')", timeout=15000)
        assert "+" in pg.inner_text(".ws-trade-table tbody tr >> nth=0")
        assert ex.position["BTC_USDT"]["sl"] == Decimal(str(d["it"]["sl"])).quantize(Decimal("0.1"))
        pg.click(".ws-trade-table button:has-text('Close')")
        pg.click(".ws-modal .ws-danger")  # confirm, in the page
        pg.wait_for_function("() => document.querySelector('.ws-trade-table td.empty')", timeout=15000)
        pg.click(".ws-trade-tabs button:has-text('History')")
        pg.wait_for_function("() => document.querySelector('.ws-trade-table').innerText.includes('Closed here')", timeout=15000)
        assert "BTC_USDT" not in ex.position
        pg.wait_for_function("() => !state.ws.tools.items.some(i => i.id === 'L1')", timeout=15000)  # its drawing went too
        pg.click(".logout-form button")  # sign out: back to the login page
        pg.wait_for_url("**/login")
        assert errors == []
        pg.close()


def test_a_position_opened_on_tabdeal_is_drawn_and_protected_from_the_chart(data, browser, engine, tmp_path):  # noqa: F811
    from decimal import Decimal

    from sp2l.api.auth import set_login
    from tests.trading.fake_exchange import FakeExchange

    with engine.begin() as c:
        c.execute(text("DELETE FROM manual_trades"))
    ex = FakeExchange(wallet="1000")
    users = tmp_path / "auth" / "dashboard.auth"
    set_login(users, "admin", "test password")
    with serve({"enabled": True, "poll_s": 1}, ex, {"enabled": True, "users_file": str(users)}) as url:
        pg = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{url}/login")
        pg.fill("input[name=user]", "admin")
        pg.fill("input[name=password]", "test password")
        pg.click(".login-btn")
        pg.wait_for_function("() => typeof state !== 'undefined' && state.ws && state.ws.bars.length > 0 && state.ws.trading && state.ws.trading.cfg", timeout=60000)
        last = pg.evaluate("() => state.ws.bars[state.ws.bars.length - 1].c")
        entry = round(last, 1)
        ex.manual("BTC_USDT", "0.003", str(entry), lev=7)  # opened in Tabdeal's app, unprotected
        ex.mark["BTC_USDT"] = Decimal(str(entry)) * Decimal("1.002")

        # it appears in the table (Tabdeal, unprotected) and on the chart as a linked Long drawing
        pg.wait_for_selector(".ws-trade-table .b-origin", timeout=15000)
        assert "no stop / target" in pg.inner_text(".ws-trade-table tbody tr >> nth=0")
        pg.wait_for_function("() => state.ws.tools.items.some(i => i.trade && i.status === 'ACTIVE' && i.type === 'long' && i.unset)", timeout=15000)
        d = pg.evaluate("() => state.ws.tools.items.find(i => i.trade)")
        assert abs(d["entry"] - entry) < 1e-6 and d["sl"] < entry < d["tp"]

        # the user moves the stop / target on the chart, then sends them with SL/TP
        sl, tp = round(entry * 0.98, 1), round(entry * 1.05, 1)
        pg.evaluate(f"() => {{ const d = state.ws.tools.items.find(i => i.trade); Object.assign(d, {{ sl: {sl}, tp: {tp}, dirty: true }}); state.ws.tools.redraw(); }}")
        pg.click(".ws-trade-table button:has-text('SL/TP')")
        pg.click(".ws-modal .ws-primary")
        pg.wait_for_function("() => document.querySelector('.ws-trade-table').innerText.includes('✓')", timeout=15000)
        assert ex.position["BTC_USDT"]["sl"] == Decimal(str(sl)) and ex.position["BTC_USDT"]["tp"] == Decimal(str(tp))
        assert not pg.evaluate("() => state.ws.tools.items.find(i => i.trade).dirty")

        # closed on Tabdeal at the target: history, and the drawing is no longer an open trade
        ex.end("BTC_USDT", str(tp))
        pg.wait_for_function("() => document.querySelector('.ws-trade-table td.empty')", timeout=15000)
        pg.wait_for_function("() => !state.ws.tools.items.some(i => i.trade)", timeout=15000)  # removed
        pg.click(".ws-trade-tabs button:has-text('History')")
        pg.wait_for_function("() => document.querySelector('.ws-trade-table').innerText.includes('Target')", timeout=15000)
        assert errors == []
        pg.close()


def test_vertical_line_date_range_anchored_vwap_fib_and_macd_shades(data, browser):
    with serve() as url:
        pg, errors = _open(browser, url)
        pg.evaluate("() => { localStorage.removeItem('smc-drawings-v1'); localStorage.removeItem('smc-chart-v1'); }")
        pg.reload()
        pg.wait_for_function("() => state.ws && state.ws.bars.length > 0", timeout=60000)
        pg.evaluate("() => state.ws.trading.setCollapsed(true)")
        pg.wait_for_timeout(300)
        box = pg.locator(".ws-chart").bounding_box()
        X, Y = box["x"], box["y"]

        def click(x, y):
            pg.mouse.move(X + x, Y + y)
            pg.mouse.click(X + x, Y + y)

        def tool(name):
            pg.click(f".ws-draw button[aria-label='{name}']")

        tool("Vertical line")
        click(400, 300)
        tool("Date range")
        click(500, 250)
        click(700, 350)
        tool("Anchored VWAP")
        click(600, 300)
        tool("Fib retracement")
        click(800, 200)
        click(1000, 450)
        items = pg.evaluate("() => state.ws.tools.items")
        assert [i["type"] for i in items] == ["vline", "daterange", "avwap", "fib"]
        dr, fib = items[1], items[3]
        assert dr["a"][1]["t"] > dr["a"][0]["t"] and fib["a"][0]["p"] > fib["a"][1]["p"]

        # the anchored VWAP runs from its anchor bar to the last bar, at the volume-weighted price
        g = pg.evaluate("""() => { const T = state.ws.tools, it = T.items.find(i => i.type === 'avwap');
            const geo = T.geom(it), b = state.ws.bars, i0 = b.findIndex(x => x.t + T.step() > it.t);
            let pv = 0, v = 0; for (let i = i0; i < b.length; i++) { const w = b[i].v > 0 ? b[i].v : 1; pv += (b[i].h + b[i].l + b[i].c) / 3 * w; v += w; }
            return { segs: geo.segs.length, n: b.length - i0, vwap: geo.vwap, want: pv / v }; }""")
        assert g["segs"] >= g["n"] - 2 and abs(g["vwap"] - g["want"]) < 1e-6

        # Fib retracement: 0 at the second point, 1 at the first, the usual levels between
        lv = pg.evaluate("() => { const T = state.ws.tools; return T.geom(T.items.find(i => i.type === 'fib')).levels.map(l => [l[0], l[1]]); }")
        a, b = fib["a"][0]["p"], fib["a"][1]["p"]
        assert [f for f, _ in lv] == [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]
        assert abs(lv[0][1] - b) < 1e-6 and abs(lv[-1][1] - a) < 1e-6 and abs(lv[4][1] - (b + (a - b) * 0.618)) < 1e-6

        # MACD: strong / faded green and red histogram bars
        pg.click(".ws-layers-btn")
        pg.check("#ws-f-macd")
        pg.wait_for_function("() => state.ws.chart.panes().length === 2")
        colors = pg.evaluate("() => { const s = state.ws.chart.panes()[1].getSeries()[0]; return [...new Set(s.data().filter(d => d.color).map(d => d.color))]; }")
        assert len(colors) == 4, colors
        assert errors == []
        pg.close()


def test_drawing_settings_vline_across_panes_and_dialogs_in_full_screen(data, browser):
    with serve() as url:
        pg, errors = _open(browser, url)
        pg.evaluate("() => { localStorage.removeItem('smc-drawings-v1'); localStorage.removeItem('smc-chart-v1'); }")
        pg.reload()
        pg.wait_for_function("() => state.ws && state.ws.bars.length > 0", timeout=60000)
        pg.evaluate("() => state.ws.trading.setCollapsed(true)")
        pg.click(".ws-layers-btn")
        pg.check("#ws-f-rsi")
        pg.click(".ws-layers-btn")
        pg.wait_for_function("() => state.ws.chart.panes().length === 2")
        box = pg.locator(".ws-chart").bounding_box()
        X, Y = box["x"], box["y"]

        def click(x, y):
            pg.mouse.move(X + x, Y + y)
            pg.mouse.click(X + x, Y + y)

        # a vertical line: drawn on the price pane and on the RSI pane
        pg.click(".ws-draw button[aria-label='Vertical line']")
        click(500, 200)
        assert pg.evaluate("() => state.ws.tools.paneLayers.length") == 1
        # selected after drawing: the mini toolbar edits its colour, width and style
        pg.wait_for_selector(".ws-dbar:not([hidden])")
        assert "Vertical line" in pg.inner_text(".ws-dbar")
        pg.fill(".ws-dbar input[type=color]", "#ff0000")
        pg.select_option(".ws-dbar select >> nth=0", "4")
        pg.select_option(".ws-dbar select >> nth=1", "dashed")
        st = pg.evaluate("() => state.ws.tools.items[0].st")
        assert st == {"color": "#ff0000", "width": 4, "style": "dashed"}
        # its settings window: the date label off
        pg.click(".ws-dbar button[aria-label='Drawing settings']")
        pg.uncheck(".ws-set input[type=checkbox]")
        pg.click(".ws-set .ws-primary")
        assert pg.evaluate("() => state.ws.tools.items[0].st.label") is False

        # Fib: levels on / off, their values and colours, extend, reverse
        pg.click(".ws-draw button[aria-label='Fib retracement']")
        click(700, 150)
        click(900, 400)
        pg.click(".ws-dbar button[aria-label='Drawing settings']")
        pg.uncheck(".ws-fib-levels label >> nth=1 >> input[type=checkbox]")
        pg.fill(".ws-fib-levels label >> nth=3 >> input[type=number]", "0.65")
        pg.dispatch_event(".ws-fib-levels label >> nth=3 >> input[type=number]", "change")
        pg.check(".ws-set label:has-text('extend to the right') input")
        pg.click(".ws-set .ws-primary")
        lv = pg.evaluate("() => { const T = state.ws.tools, it = T.items[1]; return { levels: T.geom(it).levels.map(l => l[0]), ext: it.st.extend }; }")
        assert lv["ext"] is True and 0.236 not in lv["levels"] and 0.65 in lv["levels"] and 0.5 not in lv["levels"]

        # full screen: confirmations are in-page windows inside the full-screen element
        pg.click(".ws-bar .icon-btn[aria-pressed]")
        pg.wait_for_function("() => document.querySelector('.chart-ws').classList.contains('ws-max')")
        pg.click(".ws-draw button[aria-label='Remove all drawings']")
        pg.wait_for_selector(".chart-ws .ws-modal .ws-danger")
        assert pg.evaluate("() => !!document.querySelector('.chart-ws .ws-modal')")  # inside the workspace root
        pg.click(".ws-modal .ws-danger")
        pg.wait_for_function("() => state.ws.tools.items.length === 0")
        assert errors == []
        pg.close()


def test_vwap_ema_bollinger_sessions_volume_atr_adx(data, browser):
    with serve() as url:
        pg, errors = _open(browser, url)
        pg.evaluate("() => localStorage.removeItem('smc-chart-v1')")
        pg.reload()
        pg.wait_for_function("() => state.ws && state.ws.bars.length > 0", timeout=60000)
        pg.click(".ws-layers-btn")
        groups = pg.eval_on_selector_all(".ws-group h3", "xs => xs.map(x => x.textContent)")
        assert groups == ["Levels", "Structure", "Overlays", "Panes"]
        before = pg.evaluate("() => state.ws.chart.panes()[0].getSeries().length")
        for f in ("vwap", "ema_fast", "ema_slow", "bb", "sessions"):
            pg.check(f"#ws-f-{f}")
        # price pane: VWAP 1 + EMA 2 + 2 + Bollinger 3 (sessions draw under the candles)
        assert pg.evaluate("() => state.ws.chart.panes()[0].getSeries().length") == before + 8
        for f in ("volume", "atr", "adx"):
            pg.check(f"#ws-f-{f}")
        pg.wait_for_function("() => state.ws.chart.panes().length === 4")
        titles = pg.evaluate("() => state.ws.chart.panes().slice(1).map(p => p.getSeries()[0].options().title)")
        assert titles == ["Vol", "ATR 14", "ADX 14"]

        # values: the session VWAP restarts each UTC day; ATR and ADX are filled once warmed up
        vals = pg.evaluate("""() => {
            const I = ChartIndicators, b = state.ws.bars, v = I.sessionVwap(b, 'day', 1).vwap;
            const i = b.findIndex((x, k) => k > 0 && I.sessionStart(x.t, 'day') !== I.sessionStart(b[k - 1].t, 'day'));
            const x = b[i];
            return { first: v[i], typ: (x.h + x.l + x.c) / 3, atr: I.atr(b, 14).slice(-1)[0], adx: I.adx(b, 14).adx.slice(-1)[0] };
        }""")
        assert abs(vals["first"] - vals["typ"]) < 1e-9 and vals["atr"] > 0 and 0 <= vals["adx"] <= 100

        # sessions: Tokyo, London and New York bands in the visible range on 1h
        n = pg.evaluate("""() => { const b = state.ws.bars; return ChartIndicators.sessions(b[b.length - 60].t, b[b.length - 1].t).map(s => s.id); }""")
        assert {"tokyo", "london", "newyork"} <= set(n)

        # a select setting: the VWAP restarts weekly
        pg.click("#ws-f-vwap >> xpath=ancestor::div[contains(@class,'ws-item')]//button[contains(@class,'ws-gear')]")
        pg.select_option("#ws-form-vwap select", "week")
        assert pg.evaluate("() => JSON.parse(localStorage.getItem('smc-chart-v1')).features.vwap.s.period") == "week"
        pg.wait_for_timeout(300)
        assert errors == []
        pg.close()


def test_market_combobox_switches_every_market_and_trades_the_active_one(data, browser, engine, tmp_path):  # noqa: F811
    from decimal import Decimal

    from sp2l.api.auth import set_login
    from tests.trading.fake_exchange import FakeExchange

    with engine.begin() as c:
        c.execute(text("DELETE FROM manual_trades"))
    ex = FakeExchange(wallet="1000")
    users = tmp_path / "auth" / "dashboard.auth"
    set_login(users, "admin", "test password")
    loaded = "(s) => state.symbol === s && state.ws && state.ws.barsFor === s && state.ws.bars.length > 0"
    last = "() => state.ws.bars[state.ws.bars.length - 1].c"
    with serve({"enabled": True, "poll_s": 1, "max_leverage": 100}, ex, {"enabled": True, "users_file": str(users)},
               markets=[SYM, "XRPUSDT", "SOLUSDT"]) as url:
        pg = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{url}/login")
        pg.fill("input[name=user]", "admin")
        pg.fill("input[name=password]", "test password")
        pg.click(".login-btn")
        pg.wait_for_function("() => typeof state !== 'undefined' && state.ws && state.ws.bars.length > 0 && state.ws.overlays", timeout=60000)
        pg.evaluate("() => localStorage.removeItem('smc-drawings-v1')")

        # the picker is a combobox: every market, searchable, picked with the keyboard
        assert pg.get_attribute("#sym-btn", "aria-haspopup") == "listbox"
        pg.click("#sym-btn")
        assert pg.eval_on_selector_all("#sym-list li .n", "xs => xs.map(x => x.textContent)") == ["BTC/USDT", "XRP/USDT", "SOL/USDT"]
        assert pg.eval_on_selector_all("#sym-list li .mini", "xs => xs.map(x => x.textContent)") == ["SMC"]  # the engine's market
        pg.fill("#sym-search", "sol")
        assert pg.eval_on_selector_all("#sym-list li", "xs => xs.length") == 1
        pg.keyboard.press("Enter")
        assert pg.is_hidden("#sym-pop")
        pg.wait_for_function(f"() => ({loaded})('SOLUSDT') && state.ws.overlays", timeout=30000)
        assert 100 < pg.evaluate(last) < 200  # SOL's own candles
        assert pg.evaluate("() => state.ws.candles.options().priceFormat.precision") == 2  # exchangeInfo's tick
        assert "SOL/USDT" in pg.inner_text("#sym-btn") and "Tabdeal feed" in pg.inner_text("#feed-pill")
        pg.click("#sym-btn")
        pg.keyboard.press("Escape")
        assert pg.is_hidden("#sym-pop")

        # quick switching: a late answer for an earlier market never lands on the chart
        pg.evaluate("() => { selectSymbol('BTCUSDT'); selectSymbol('SOLUSDT'); selectSymbol('BTCUSDT'); }")
        pg.wait_for_function(f"() => ({loaded})('BTCUSDT')", timeout=30000)
        time.sleep(1.5)
        assert pg.evaluate(last) > 1000 and pg.evaluate("() => state.ws.barsFor") == "BTCUSDT"

        # Tabdeal unreachable at first (XRP not loaded yet): the chart says so and tries again until it loads
        data.down["XRP_USDT"] = time.time() + 3
        try:
            pg.evaluate("() => selectSymbol('XRPUSDT')")
            pg.wait_for_function("() => (document.querySelector('.ws-note') || {}).textContent.includes('retrying')", timeout=15000)
            assert pg.evaluate("() => state.ws.bars.length") == 0  # BTC's candles are gone at once
            pg.wait_for_function(f"() => ({loaded})('XRPUSDT')", timeout=30000)
            assert pg.evaluate(last) < 2 and pg.is_hidden(".ws-note")
        finally:
            data.down.clear()

        # the market is kept across a reload, though the engine does not run on it
        pg.evaluate("() => selectSymbol('SOLUSDT')")
        pg.wait_for_function(f"() => ({loaded})('SOLUSDT')", timeout=30000)
        pg.reload()
        pg.wait_for_function(f"() => typeof state !== 'undefined' && ({loaded})('SOLUSDT') && state.ws.trading && state.ws.trading.cfg && state.ws.trading.cfg.ready", timeout=60000)

        # Trade on the active market: the order goes to SOL_USDT at SOL's precision
        d = pg.evaluate("""() => {
            const b = state.ws.bars, e = +b[b.length - 1].c.toFixed(2);
            const it = { id: "S1", type: "long", t1: b[b.length - 20].t, t2: b[b.length - 1].t + 3600 * 10, entry: e, sl: +(e * 0.95).toFixed(2), tp: +(e * 1.1).toFixed(2) };
            state.ws.tools.items.push(it); state.ws.tools.save(); state.ws.tools.redraw();
            return it;
        }""")
        pg.click(".ws-trade-btn")
        pg.wait_for_selector(".ws-modal .ws-trade-dlg")
        assert "SOL/USDT" in pg.inner_text(".ws-trade-dlg")
        pg.fill(".ws-trade-dlg .ws-field input >> nth=0", "50")
        pg.fill(".ws-trade-dlg .ws-field input >> nth=1", "20")
        pg.click(".ws-trade-dlg .ws-go")
        pg.wait_for_selector(".ws-trade-table .b-pending", timeout=15000)
        market, side, qty, price, _ = next(a for n, a in ex.calls if n == "limit_order")
        assert (market, side) == ("SOL_USDT", "BUY") and ex.leverage["SOL_USDT"] == 50
        assert Decimal(price) == Decimal(str(d["entry"])) and Decimal(price).as_tuple().exponent == -2
        assert Decimal(qty).as_tuple().exponent >= -2  # SOL's quantity step

        # the table under the chart lists the open trades of every market
        ex.manual("BTC_USDT", "0.001", "50000")
        pg.wait_for_function("() => document.querySelector('.ws-trade-table').innerText.includes('BTC')", timeout=15000)
        assert "SOL" in pg.inner_text(".ws-trade-table")
        assert errors == []
        pg.close()


def test_playbook_strategy_draws_setups_current_setup_checklist_backtest_and_scan(data, browser):
    with serve(markets=[SYM, "XRPUSDT"]) as url:
        pg, errors = _open(browser, url)
        pg.evaluate("() => { localStorage.removeItem('smc-playbook-v1'); localStorage.removeItem('smc-drawings-v1'); }")
        pg.evaluate("() => state.ws.setTf('4h')")
        pg.wait_for_function("() => state.ws.tf === '4h' && state.ws.bars.length > 0")
        opts = pg.eval_on_selector_all(".ws-pb-select option", "xs => xs.map(x => x.textContent)")
        assert opts == ["Strategy: off", "1. Previous-high breakout (long only)", "2. EMA 50 pullback", "3. Liquidity sweep + BOS + FVG / OB", "4. Anchored VWAP pullback"]
        assert pg.is_hidden(".ws-pb")

        # Donchian: the chart goes to 1h, draws its channel and every setup to its stop / exit
        pg.select_option(".ws-pb-select", "donchian")
        pg.wait_for_function("() => state.ws.tf === '1h' && state.ws.playbook.data && state.ws.bars.length > 0", timeout=60000)
        pg.wait_for_function("() => state.ws.playbook.layer.items.boxes.length > 0", timeout=30000)
        d = pg.evaluate("() => { const d = state.ws.playbook.data; return { n: d.setups.length, stats: d.stats, series: d.series.map(s => s.id), checks: d.checks.LONG.length }; }")
        assert d["stats"]["short"]["trades"] == 0
        assert d["n"] > 0 and d["stats"]["trades"] > 0 and d["checks"] >= 3
        assert set(d["series"]) == {"ema4", "prev_hi", "lo_out"}
        assert pg.evaluate("() => Object.keys(state.ws.playbook.data.checks)") == ["LONG"]  # long only
        assert pg.evaluate("() => state.ws.playbook.data.setups.every(s => s.side === 'LONG')")
        assert pg.evaluate("() => state.ws.handles.length") >= 1  # its lines on the chart
        pg.wait_for_selector(".ws-pb .ws-pb-checks li")
        txt = pg.inner_text(".ws-pb")
        assert "Current setup" in txt and "Checklist" in txt and "Backtest · 90 days" in txt and "Setups" in txt
        assert "previous high" in txt  # the checklist names the rule

        # a past setup from the table: selected on the chart; one drawn as a position (for Trade)
        pg.click(".ws-pb-table tbody tr >> nth=0")
        assert pg.evaluate("() => state.ws.playbook.sel") is not None
        sid = pg.evaluate("() => state.ws.playbook.data.setups.find(s => s.fill).id")
        pg.evaluate(f"() => state.ws.playbook.drawAsPosition(state.ws.playbook.data.setups.find(s => s.id === '{sid}'))")
        it = pg.evaluate(f"() => state.ws.tools.items.find(i => i.id === 'pb-{sid}')")
        assert it and it["type"] in ("long", "short") and it["sl"] and it["tp"]

        # the settings reload it; another strategy shows its own lines (ADX in its own pane)
        pg.select_option(".ws-pb-controls select", "30")
        pg.wait_for_function("() => state.ws.playbook.data && state.ws.playbook.data.days === 30", timeout=30000)
        pg.select_option(".ws-pb-select", "ema")
        pg.wait_for_function("() => state.ws.playbook.data && state.ws.playbook.data.strategy === 'ema'", timeout=30000)
        assert pg.evaluate("() => state.ws.chart.panes().length") >= 2
        assert "EMA 50" in pg.inner_text(".ws-pb")

        # a setup older than the chart's bars (a short a year back): clicked in the table, the
        # chart loads back to it, centres it and labels it as the short it is
        pg.select_option(".ws-pb-controls select", "365")
        pg.wait_for_function("() => state.ws.playbook.data && state.ws.playbook.data.days === 365", timeout=60000)
        short = pg.evaluate("() => state.ws.playbook.data.setups.find(s => s.side === 'SHORT' && s.fill)")
        assert short and short["signal_t"] < pg.evaluate("() => state.ws.bars[0].t")
        pg.click(".ws-pb-table tbody tr:has-text('Short') >> nth=0")
        pg.wait_for_function(f"() => state.ws.bars[0].t <= {short['signal_t']} && state.ws.playbook.sel === '{short['id']}'", timeout=60000)
        pg.wait_for_function(f"() => {{ const r = state.ws.chart.timeScale().getVisibleRange(); return r && r.from <= {short['signal_t']} && r.to >= {short['signal_t']}; }}", timeout=15000)
        marks = pg.evaluate("() => state.ws.playbook.layer.items.marks.map(m => m.text)")
        assert any(m.startswith("Short ") for m in marks) and not any(m.startswith("Long ") for m in marks)
        boxes = pg.evaluate(f"() => state.ws.playbook.layer.items.boxes.filter(b => b.t1 === {short['fill_t']})")
        stop = next(b for b in boxes if b["fill"].startswith("rgba(" + pg.evaluate("() => state.ws.colors().bear")))
        assert stop["bottom"] == pytest.approx(short["fill"]) and stop["top"] == pytest.approx(short["sl"])  # the stop above

        # every market: the scanner lists each with its current setup and backtest
        pg.click(".ws-pb-controls button:has-text('Scan markets')")
        pg.wait_for_function("() => state.ws.playbook.scan && !state.ws.playbook.scan.running", timeout=60000)
        rows = pg.eval_on_selector_all(".ws-pb-list:has-text('Markets') tbody tr td:first-child", "xs => xs.map(x => x.textContent)")
        assert sorted(rows) == ["BTC/USDT", "XRP/USDT"]
        pg.click(".ws-pb-list:has-text('Markets') tbody tr:has-text('XRP/USDT')")
        pg.wait_for_function("() => state.symbol === 'XRPUSDT' && state.ws.playbook.data && state.ws.barsFor === 'XRPUSDT'", timeout=60000)
        pg.screenshot(path="/tmp/playbook.png")

        # on another timeframe nothing is drawn; off: the panel goes
        pg.evaluate("() => state.ws.setTf('4h')")
        pg.wait_for_function("() => state.ws.tf === '4h' && state.ws.bars.length > 0", timeout=30000)
        assert pg.evaluate("() => state.ws.playbook.layer.items.boxes.length") == 0
        assert "shown on 1h only" in pg.inner_text(".ws-pb-summary")
        pg.select_option(".ws-pb-select", "")
        assert pg.is_hidden(".ws-pb")
        assert errors == []
        pg.close()
