"""The chart in a real browser: the default view draws only the tradable setups, the debug
switch draws every zone, and a setup rejected by a filter only shows up in debug, with its
reason. Served by the real API from a test database holding the hand-built setup of
tests/smc/fixtures.py (WebKit, Playwright)."""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from sp2l.smc.history import ensure_history
from tests.db.conftest import URL, engine  # noqa: F401  (module-scoped fresh schema)
from tests.smc.fixtures import ARMING_BAR, SETUP, T0, expand

pytestmark = pytest.mark.db
SYM = "BTCUSDT"
KEY = "1h:OB:LONG:1767616200"
SHOTS = os.environ.get("SMC_UI_SHOTS")  # optional folder for screenshots
SMC = {"swing_len": 2, "atr_len": 3, "fvg_min_atr": 0, "bias_tf": "1h", "history_days": 2}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def data(engine) -> None:  # noqa: F811
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


@contextmanager
def serve(smc: dict[str, Any]) -> Iterator[str]:
    import uvicorn

    from sp2l.api.app import create_app
    from sp2l.config import RuntimeConfig

    cfg = RuntimeConfig(
        {
            "database_url": URL,
            "symbols": [SYM],
            "instruments": {SYM: {"tick": "0.01", "step": "0.001"}},
            "costs": {"maker_fee": "0", "taker_fee": "0", "slippage_allowance": "0"},
            "smc": smc,
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
        b = None
        cache = Path.home() / "Library" / "Caches" / "ms-playwright"
        tries = [None, *sorted(str(x) for x in cache.glob("webkit-*/pw_run.sh"))]
        for exe in tries:  # the bundled build, else any installed WebKit build
            try:
                b = pw.webkit.launch(executable_path=exe)
                break
            except Exception:  # pragma: no cover
                continue
        if b is None:  # pragma: no cover
            pytest.skip("no Playwright WebKit installed (uv run playwright install webkit)")
        yield b
        b.close()


def _open(browser, url: str):
    pg = browser.new_page(viewport={"width": 1500, "height": 950})
    pg.goto(f"{url}/#/chart")
    pg.wait_for_function(
        "() => state.analysis && state.analysis.ready && state.data.length > 0", timeout=60000
    )
    pg.evaluate("() => { state.tf = '1h'; renderControls(); return loadChart(false); }")
    pg.wait_for_function("() => state.analysis.tf === '1h'")
    return pg


def _drawn(pg) -> dict:
    return pg.evaluate(
        """() => { const a = shown(); return {
            mode: state.mode, zones: a.zones.length, htf: a.htf_zones.length,
            liquidity: a.liquidity.length, setups: a.setups.map((s) => [s.key, s.state]),
            reasons: a.setups.map((s) => s.plan ? s.plan.reasons.map((r) => r.code) : []),
            all: (state.analysis.setups_all || []).length }; }"""
    )


def test_default_chart_draws_only_the_setup_and_debug_draws_every_zone(data, browser):
    with serve(SMC) as url:
        page = _open(browser, url)
        d = _drawn(page)
        assert d["mode"] == "setups"
        assert d["zones"] == d["htf"] == d["liquidity"] == 0  # no standalone zone or level
        assert d["setups"] == [[KEY, "waiting"]] and d["reasons"] == [[]]
        plan = page.evaluate("() => state.analysis.setups[0].plan")
        assert plan["range_mid"] and plan["tp"]["level"] and plan["cost_frac"] is not None
        if SHOTS:
            page.locator("#chart-card").screenshot(path=str(Path(SHOTS) / "setups.png"))
        page.click("[data-mode=debug]")
        d = _drawn(page)
        assert d["mode"] == "debug" and d["zones"] > 0 and d["all"] >= 1
        if SHOTS:
            page.locator("#chart-card").screenshot(path=str(Path(SHOTS) / "debug.png"))
        page.click("[data-mode=setups]")
        assert _drawn(page)["zones"] == 0


def test_a_setup_rejected_by_a_filter_is_hidden_by_default_and_shown_in_debug(data, browser):
    with serve({**SMC, "min_net_rr": 50}) as url:
        page = _open(browser, url)
        page.evaluate("() => { state.mode = 'setups'; state.layer.update(); }")
        assert _drawn(page)["setups"] == []  # LOW_NET_RR: not in the default view
        page.click("[data-mode=debug]")
        d = _drawn(page)
        assert [KEY, "waiting"] in d["setups"]
        assert "LOW_NET_RR" in d["reasons"][d["setups"].index([KEY, "waiting"])]
        if SHOTS:
            page.locator("#chart-card").screenshot(path=str(Path(SHOTS) / "rejected_debug.png"))
        page.click("[data-mode=setups]")


SMC23 = {
    **SMC,
    "sl_mode": "structure",
    "tp_mode": "liquidity",
    "discount_ref": "displacement",
    "max_cost_frac": 0.2,
    "min_net_rr": 2,
    "liq_buffer_r": 1,
}


def test_smc23_default_view_shows_the_liquidity_target_and_hides_liq_limited(data, browser):
    with serve(SMC23) as url:
        page = _open(browser, url)
        d = _drawn(page)
        assert d["setups"] == [[KEY, "waiting"]] and d["reasons"] == [[]]
        plan = page.evaluate("() => state.analysis.setups[0].plan")
        assert plan["tp"]["source"].startswith("1h")  # nearest unswept liquidity
        assert float(plan["sl"]) < 94  # beyond the OB / sweep wick (94) by the ATR buffer
        assert float(plan["range_mid"]) == 100  # sweep wick 94 -> displacement high 106
        page.goto(f"{url}/#/strategy")
        page.wait_for_function(
            "() => document.getElementById('model-flow').innerText.includes('liquidity')",
            timeout=30000,
        )
        flow = page.inner_text("#model-flow")
        assert "nearest unswept liquidity" in flow and "ATR" in flow
    with serve({**SMC23, "liq_buffer_r": 10**9}) as url:
        page = _open(browser, url)
        assert _drawn(page)["setups"] == []  # LIQ_LIMITED: not in the default view
        page.click("[data-mode=debug]")
        d = _drawn(page)
        assert "LIQ_LIMITED" in d["reasons"][d["setups"].index([KEY, "waiting"])]
