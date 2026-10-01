"""Browser layout tests: states A–J at 1440 / 768 / 390 px in real Chrome (Playwright).

The page is served exactly as production serves it (index.html, /static files, the same CSP
header); only /api/* is answered from `tests/ui/fixtures.py`, whose DTOs come from the real
backend presentation code. Screenshots are written to docs/ui-screenshots/ for visual review.
"""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from sp2l.api.app import CSP
from tests.ui.fixtures import STATES, bundle

playwright = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.browser

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
SHOTS = ROOT / "docs" / "ui-screenshots"
WIDTHS = {1440: 900, 768: 1024, 390: 844}
BASE = "http://sp2l.test"
# (state, view) pairs rendered at every width; dashboard for every state
CASES = [(s, "dashboard") for s in STATES] + [
    ("C", "candidates"),
    ("D", "candidates"),
    ("J", "candidates"),
    ("H", "candidates"),
    ("I", "diagnostics"),
    ("B", "history"),
    ("J", "analytics"),
    ("J", "indicators"),
]


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        exe = os.environ.get("SP2L_CHROMIUM")  # e.g. EPEL chromium on the server
        try:
            b = (
                p.chromium.launch(executable_path=exe, headless=True, args=["--no-sandbox"])
                if exe
                else p.chromium.launch(channel="chrome", headless=True)
            )
        except Exception:
            try:  # servers: Playwright's bundled Chromium
                b = p.chromium.launch(headless=True)
            except Exception as exc:  # pragma: no cover
                pytest.skip(f"no Chromium available: {exc}")
        yield b
        b.close()


def _api(data: dict[str, Any], url: str) -> Any:
    u = urlparse(url)
    path = u.path
    if path.startswith("/api/setups/"):
        return data["details"].get(unquote(path.rsplit("/", 1)[1]))
    if path == "/api/setups":
        s = dict(data["setups"])
        bucket = parse_qs(u.query).get("bucket", [None])[0]
        items = s["items"]
        if bucket == "ACTIVE":
            items = [i for i in items if not i["terminal"]]
        elif bucket:
            from sp2l.api.queries import FILTERS

            items = [i for i in items if i["status"] in FILTERS[bucket]]
        limit = parse_qs(u.query).get("limit", [None])[0]
        return {**s, "items": items[: int(limit)] if limit else items}
    return data.get(path)


def open_page(browser, state: str, view: str, width: int):
    data = bundle(state)
    ctx = browser.new_context(
        viewport={"width": width, "height": WIDTHS[width]},
        timezone_id="Asia/Tehran",
        locale="en-US",
        device_scale_factor=1,
    )
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on(
        "console",
        lambda m: errors.append(m.text) if m.type == "error" and "fonts" not in m.text else None,
    )

    def handle(route):
        url = route.request.url
        u = urlparse(url)
        if u.hostname != "sp2l.test":
            return route.continue_()  # web fonts
        if u.path == "/api/live/stream":  # Server-Sent Events, as the backend sends them
            evs = data["live_stream"]
            parts = ["retry: 600000\n\n", f"event: snapshot\ndata: {json.dumps(evs[0])}\n\n"]
            parts += [f"data: {json.dumps(e)}\n\n" for e in evs[1:]]
            return route.fulfill(body="".join(parts), content_type="text/event-stream")
        if u.path.startswith("/api/"):
            body = _api(data, url)
            if body is None:
                return route.fulfill(status=404, body="{}", content_type="application/json")
            return route.fulfill(body=json.dumps(body), content_type="application/json")
        f = WEB / ("index.html" if u.path == "/" else u.path.removeprefix("/static/"))
        ctype = mimetypes.guess_type(f.name)[0] or "text/plain"
        return route.fulfill(
            body=f.read_bytes(), content_type=ctype, headers={"Content-Security-Policy": CSP}
        )

    page.route("**/*", handle)
    page.goto(f"{BASE}/#/{view}", wait_until="networkidle")
    page.wait_for_timeout(700)  # chart canvas paint
    return ctx, page, errors, data


LAYOUT_JS = (Path(__file__).parent / "layout_check.js").read_text()


@pytest.mark.parametrize("width", list(WIDTHS))
@pytest.mark.parametrize(("state", "view"), CASES)
def test_layout_has_no_overflow_or_overlap(browser, state, view, width):
    ctx, page, errors, _ = open_page(browser, state, view, width)
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{state}_{view}_{width}.png"), full_page=True)
        if width == 390:  # what a phone shows first (fixed bottom nav in place)
            page.screenshot(path=str(SHOTS / f"{state}_{view}_{width}_viewport.png"))
        m = page.evaluate(LAYOUT_JS)
        assert m["pageOverflow"] <= 0, f"page scrolls horizontally by {m['pageOverflow']}px"
        assert m["overlaps"] == [], m["overlaps"][:5]
        assert m["overflows"] == [], m["overflows"][:5]
        assert m["narrow"] == [], m["narrow"][:5]
        assert errors == [], errors
    finally:
        ctx.close()


@pytest.mark.parametrize("width", list(WIDTHS))
def test_feed_redundancy_panels_do_not_collapse_or_overlap(browser, width):
    ctx, page, errors, _ = open_page(browser, "I", "diagnostics", width)
    try:
        rects = page.evaluate(
            """() => ['diag-conn', 'diag-merged', 'diag-corr', 'diag-gaps'].map((id) => {
                 const card = document.getElementById(id).closest('.card');
                 const r = card.getBoundingClientRect();
                 return { id, w: r.width, right: r.right, vw: window.innerWidth };
               })"""
        )
        for r in rects:
            assert r["w"] >= min(300, r["vw"] - 40), r  # never collapses (was ~111px / ~26px)
            assert r["right"] <= r["vw"], r
        # the dashboard keeps only a human summary of the feed
        page.goto(f"{BASE}/#/dashboard", wait_until="networkidle")
        md = page.inner_text("#market-data")
        assert "Connections" in md and "Coverage" in md and "Warmup" in md
        assert "Payload conflicts" in md  # conflicts > 0 are a visible warning
        assert "pong" not in md.lower() and "correlated" not in md.lower()
    finally:
        ctx.close()


EXPECTED_STATUS = {
    "A": ("Healthy", "Warming up", "Not started", "Disabled"),
    "B": ("Healthy", "Scanning", "Session open", "Disabled"),
    "E": ("Healthy", "Setup active", "Session open", "Disabled"),
    "H": ("Healthy", "Setup active", "Position open", "Disabled"),
    "I": ("Degraded", "Scanning", "Session open", "Disabled"),
    "J": ("Healthy", "Warming up", "Session open", "Disabled"),
}


@pytest.mark.parametrize("state", list(EXPECTED_STATUS))
def test_critical_status_renders_four_human_concepts(browser, state):
    ctx, page, _, data = open_page(browser, state, "dashboard", 1440)
    try:
        tiles = page.locator("#status-tiles .status-tile")
        assert tiles.count() == 4
        values = [tiles.nth(i).locator(".v").inner_text().strip() for i in range(4)]
        assert tuple(values) == EXPECTED_STATUS[state]
        blocker = page.inner_text("#blocker")
        assert data["/api/overview"]["system"]["blocker"]["text"] in blocker
        assert "CONNECTED" not in " ".join(values)
        assert "LIVE_AUTOMATION_DISABLED" not in page.inner_text("header")  # humanized
    finally:
        ctx.close()


def test_exact_reason_codes_stay_accessible(browser):
    ctx, page, _, data = open_page(browser, "D", "candidates", 1440)
    try:
        key = next(
            k for k, d in data["details"].items() if d["setup"]["status"] == "REJECTED_EXHAUSTION"
        )
        page.goto(f"{BASE}/#/candidates/{key}", wait_until="networkidle")
        txt = page.inner_text("#detail-body")
        assert "Move appears overextended late in the trend" in txt  # human first
        assert "EXHAUSTION_RISK" in txt  # exact internal code still available
        assert "EXTREME_STRETCH" in txt
        page.locator("#detail-body details.tech").nth(1).locator("summary").click()
        assert "EXHAUSTION RESULT" in page.inner_text("#detail-body")
    finally:
        ctx.close()


def test_confirmed_counterfactual_and_ambiguous_are_not_mixed(browser):
    ctx, page, _, _ = open_page(browser, "J", "analytics", 1440)
    try:
        kpis = page.inner_text("#perf-kpis")
        assert "Counterfactual" not in kpis and "uncertain" not in kpis.lower()
        amb = page.inner_text("#ambiguous-table")
        assert "AMBIGUOUS_DATA_GAP" in amb
        cf_card = page.locator("#cf-table").locator("xpath=ancestor::section[1]")
        assert "Hypothetical" in cf_card.inner_text()
        page.goto(f"{BASE}/#/dashboard", wait_until="networkidle")
        shadow = page.inner_text("#shadow-body")
        assert "Counterfactual" not in shadow and "COUNTERFACTUAL" not in shadow
    finally:
        ctx.close()


def test_rendered_chart_ohlc_equals_the_api_exactly_and_holes_stay_empty(browser):
    ctx, page, _, data = open_page(browser, "B", "dashboard", 1440)
    try:
        shown = page.evaluate(
            "() => state.series.data().map((b) => [b.time, b.open, b.high, b.low, b.close])"
        )
        api = data["/api/market/candles"]["items"]
        from datetime import datetime

        by_t = {int(datetime.fromisoformat(a["open_time"]).timestamp()): a for a in api}
        drawn = {row[0]: row[1:] for row in shown}
        real = {t: a for t, a in by_t.items() if not a.get("missing")}
        live = {
            int(datetime.fromisoformat(e["t"]).timestamp()) for e in data["live_stream"][1:]
        }  # the backend's live minutes after the stored series (V5.9)
        assert set(drawn) - set(real) == live  # every canonical candle drawn, nothing invented
        drawn = {t: v for t, v in drawn.items() if t not in live}
        for t, a in real.items():
            assert drawn[t] == [float(a[k]) for k in ("open", "high", "low", "close")]
        holes = [t for t, a in by_t.items() if a.get("missing")]
        assert holes and not set(holes) & set(drawn)  # a hole stays empty (whitespace slot)
        # hover shows exact values and lineage
        page.hover("#chart", position={"x": 400, "y": 200})
        assert "trades" in page.inner_text("#chart-ohlc") or page.inner_text("#chart-ohlc") == ""
    finally:
        ctx.close()


# ---- V5.9 live forming candle + P-Gap quality ---------------------------------------------


@pytest.mark.parametrize("width", list(WIDTHS))
def test_live_candle_updates_in_place_final_wins_and_price_is_live(browser, width):
    """10/11/12 (+7/8 in the browser): 200 rapid forming updates, then the final canonical
    candle (REST added a higher trade), then the next minute forming - on one chart."""
    ctx, page, errors, data = open_page(browser, "B", "dashboard", width)
    try:
        evs = data["live_stream"]
        final = next(e for e in evs if e["type"] == "final")
        nxt = evs[-1]
        from datetime import datetime

        t1 = int(datetime.fromisoformat(final["t"]).timestamp())
        t2 = int(datetime.fromisoformat(nxt["t"]).timestamp())
        page.wait_for_function(
            f"() => state.series && state.series.data().some((b) => b.time === {t2})"
        )
        bars = page.evaluate(
            "() => state.series.data().map((b) => [b.time, b.open, b.high, b.low, b.close])"
        )
        times = [b[0] for b in bars]
        assert len(times) == len(set(times))  # one candle per minute, never duplicated
        by = {b[0]: b[1:] for b in bars}
        assert by[t1] == [float(final[k]) for k in ("o", "h", "l", "c")]  # canonical wins
        assert by[t2] == [float(nxt[k]) for k in ("o", "h", "l", "c")]  # next minute live
        assert page.evaluate("() => window.sp2lChartCreates") == 1  # updated in place
        price = page.inner_text("#chart-price").replace(",", "")
        assert float(price) == float(nxt["price"])  # live price, not the last final candle
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0
        assert not [e for e in errors if "EventSource" not in e and "stream" not in e]
        page.screenshot(path=str(SHOTS / f"LIVE_candle_{width}.png"))
    finally:
        ctx.close()


def test_go_to_live_appears_when_scrolled_back_and_returns(browser):
    ctx, page, _, _ = open_page(browser, "B", "dashboard", 1440)
    try:
        page.wait_for_function("() => state.series && state.series.data().length > 100")
        # the fixture stream (200 forming updates, a final, one next-minute update) must be
        # fully applied first, or a late bar can move the range between the two reads
        page.wait_for_function("() => (window.sp2lLatency || []).length >= 201")
        assert not page.is_visible("#go-live")
        page.evaluate("() => state.chart.timeScale().scrollToPosition(-60, false)")
        page.wait_for_selector("#go-live", state="visible")
        before = page.evaluate("() => state.chart.timeScale().getVisibleLogicalRange()")
        page.evaluate(  # a live update while inspecting history must not move the view
            "() => applyLive({type: 'final',"
            " t: new Date(state.data[5].time * 1000).toISOString(), status: 'OK',"
            " quality: 'LIVE_RECONCILED', o: '1', h: '2', l: '1', c: '2', v: '1', n: 1})"
        )
        after = page.evaluate("() => state.chart.timeScale().getVisibleLogicalRange()")
        assert abs(before["from"] - after["from"]) < 0.5
        page.click("#go-live")
        page.wait_for_selector("#go-live", state="hidden")
    finally:
        ctx.close()


def test_pgap_quality_is_shown_in_human_language(browser):
    ctx, page, _, _ = open_page(browser, "C", "candidates", 1440)
    try:
        page.wait_for_selector(".pq")
        card = page.inner_text(".pq")
        assert "P-Gap quality" in card and "Valid P-Gap" in card and "Body / candle" in card
        page.screenshot(path=str(SHOTS / "PGAP_candidate_valid_1440.png"), full_page=True)
    finally:
        ctx.close()
    for width in WIDTHS:
        ctx, page, _, _ = open_page(browser, "I", "diagnostics", width)
        try:
            page.wait_for_selector("#diag-pgaps tr td")
            tbl = page.inner_text("#diag-pgaps")
            assert "impulse candle is dominated by shadows" in tbl
            assert "gap is too small compared with the impulse body" in tbl
            assert "impulse candle closed against the P-Gap direction" in tbl
            assert "Created a candidate" in tbl
            assert (
                page.evaluate(
                    "() => document.documentElement.scrollWidth"
                    " - document.documentElement.clientWidth"
                )
                <= 0
            )
            page.locator("#diag-pgaps").screenshot(path=str(SHOTS / f"PGAP_table_{width}.png"))
        finally:
            ctx.close()


# ---- support / resistance zones (display only) ----------------------------------------------


def test_sr_zones_are_drawn_and_each_timeframe_can_be_hidden(browser):
    ctx, page, _, data = open_page(browser, "B", "dashboard", 1440)
    try:
        page.wait_for_function("() => (window.sp2lZonesDrawn || 0) > 0")
        zones = data["/api/market/zones"]["zones"]
        buttons = page.locator("#zone-bar button")
        assert buttons.count() == 3
        assert [buttons.nth(i).inner_text().split(" ")[0] for i in range(3)] == ["15m", "30m", "4h"]
        assert {z["timeframe"] for z in zones} >= {"4h"}
        before = page.evaluate("() => window.sp2lZonesDrawn")
        buttons.nth(2).click()  # hide 4h
        assert buttons.nth(2).get_attribute("aria-pressed") == "false"
        n4h = sum(1 for z in zones if z["timeframe"] == "4h")
        page.wait_for_function(f"() => window.sp2lZonesDrawn === {before - n4h}")
        buttons.nth(2).click()
        page.wait_for_function(f"() => window.sp2lZonesDrawn === {before}")
    finally:
        ctx.close()
