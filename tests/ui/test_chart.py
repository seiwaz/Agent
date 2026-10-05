"""The chart in a real browser: the default view draws only the tradable setups, the debug
switch draws every zone. Served by the real API from a test database holding the hand-built
setup of tests/smc/fixtures.py (WebKit, Playwright)."""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from sp2l.smc.history import ensure_history
from tests.db.conftest import URL, engine  # noqa: F401  (module-scoped fresh schema)
from tests.smc.fixtures import ARMING_BAR, SETUP, T0, expand

pytestmark = pytest.mark.db
SYM = "BTCUSDT"
SHOTS = os.environ.get("SMC_UI_SHOTS")  # optional folder for screenshots


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def server(engine) -> Iterator[str]:  # noqa: F811
    import uvicorn

    from sp2l.api.app import create_app
    from sp2l.config import RuntimeConfig

    m1 = expand(SETUP)[: ARMING_BAR * 60]  # up to the bar before price comes back

    def fetch(a, b):
        return [
            {
                "time": int(c.open_time.timestamp()),
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
                "volume": 1.0,
            }
            for c in m1
            if a <= c.open_time < b
        ]

    ensure_history(engine, SYM, fetch, days=2, now=T0 + timedelta(hours=ARMING_BAR, seconds=20))
    cfg = RuntimeConfig(
        {
            "database_url": URL,
            "symbols": [SYM],
            "instruments": {SYM: {"tick": "0.01", "step": "0.001"}},
            "costs": {"maker_fee": "0", "taker_fee": "0", "slippage_allowance": "0"},
            "smc": {
                "swing_len": 2,
                "atr_len": 3,
                "fvg_min_atr": 0,
                "bias_tf": "1h",
                "max_risk_pct": 0.2,
                "max_cost_frac": 1,
                "min_net_rr_tp2": 0,
                "history_days": 2,
            },
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
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(timeout=10)


@pytest.fixture(scope="module")
def page(server):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        browser = None
        cache = Path.home() / "Library" / "Caches" / "ms-playwright"
        tries = [None, *sorted(str(x) for x in cache.glob("webkit-*/pw_run.sh"))]
        for exe in tries:  # the bundled build, else any installed WebKit build
            try:
                browser = pw.webkit.launch(executable_path=exe)
                break
            except Exception:  # pragma: no cover
                continue
        if browser is None:  # pragma: no cover
            pytest.skip("no Playwright WebKit installed (uv run playwright install webkit)")
        pg = browser.new_page(viewport={"width": 1500, "height": 950})
        pg.goto(f"{server}/#/chart")
        pg.wait_for_function(
            "() => state.analysis && state.analysis.ready && state.data.length > 0", timeout=60000
        )
        yield pg
        browser.close()


def _drawn(pg) -> dict:
    return pg.evaluate(
        """() => { const a = shown(); return {
            mode: state.mode, zones: a.zones.length, htf: a.htf_zones.length,
            liquidity: a.liquidity.length, setups: a.setups.map((s) => [s.key, s.state]),
            all: (state.analysis.setups_all || []).length }; }"""
    )


def test_default_chart_draws_only_the_setup_and_debug_draws_every_zone(page):
    page.evaluate("() => { state.tf = '1h'; renderControls(); return loadChart(false); }")
    page.wait_for_function("() => state.analysis.tf === '1h'")
    d = _drawn(page)
    assert d["mode"] == "setups"
    assert d["zones"] == d["htf"] == d["liquidity"] == 0  # no standalone zone or level
    assert d["setups"] == [["1h:OB:LONG:1767616200", "waiting"]]
    if SHOTS:
        page.locator("#chart-card").screenshot(path=str(Path(SHOTS) / "setups.png"))
    page.click("[data-mode=debug]")
    d = _drawn(page)
    assert d["mode"] == "debug" and d["zones"] > 0 and d["all"] >= 1
    if SHOTS:
        page.locator("#chart-card").screenshot(path=str(Path(SHOTS) / "debug.png"))
    page.click("[data-mode=setups]")
    assert _drawn(page)["zones"] == 0
