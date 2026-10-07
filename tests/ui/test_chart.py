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


@contextmanager
def serve() -> Iterator[str]:
    import uvicorn

    from sp2l.api.app import create_app
    from sp2l.config import RuntimeConfig

    cfg = RuntimeConfig(
        {
            "database_url": URL,
            "symbols": [SYM],
            "instruments": {SYM: {"tick": "0.1", "step": "0.001"}},
            "costs": {"maker_fee": "0", "taker_fee": "0", "slippage_allowance": "0"},
            "smc": {"htf_grid": "utc"},
        }
    )
    port = _free_port()
    srv = uvicorn.Server(
        uvicorn.Config(create_app(cfg), host="127.0.0.1", port=port, log_level="warning")
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
            "Trend line", "Horizontal line", "Long position", "Short position", "Price range", "Path", "Remove all drawings"]
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
