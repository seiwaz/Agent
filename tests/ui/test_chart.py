# ruff: noqa: E501  (selectors and in-page scripts are clearer on one line)
"""The chart workspace in a real browser, served by the real API from a test database with 20
days of 1-minute history: timeframes, the per-timeframe trend strip, every feature switched on
and off on its own, indicators in their own panes, settings applied and kept, a timeframe change
re-applying the active features, full screen, and no page errors (Playwright)."""

from __future__ import annotations

import io
import math
import random
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.db.conftest import URL, engine  # noqa: F401  (module-scoped fresh schema)

pytestmark = [pytest.mark.db, pytest.mark.browser]
SYM = "BTCUSDT"
END = datetime(2026, 3, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def data(engine) -> None:  # noqa: F811
    """20 days of a trending random walk, one bar per minute."""
    r, px, t, rows = random.Random(5), 50_000.0, END - timedelta(days=20), io.StringIO()
    drift = 0.0
    for n in range(20 * 1440):
        if n % 720 == 0:
            drift = r.gauss(0, 0.0001)
        o = px
        c = o * math.exp(drift + r.gauss(0, 0.0009))
        h, lo = max(o, c) * 1.0003, min(o, c) * 0.9997
        rows.write(f"{SYM}\t{t.isoformat()}\t{o:.1f}\t{h:.1f}\t{lo:.1f}\t{c:.1f}\t1\n")
        px, t = c, t + timedelta(minutes=1)
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur, cur.copy(
            "COPY exchange_m1 (symbol, open_time, open, high, low, close, volume) FROM STDIN"
        ) as cp:
            cp.write(rows.getvalue())
        raw.commit()
    finally:
        raw.close()
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM exchange_m1")).scalar_one() == 20 * 1440


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
