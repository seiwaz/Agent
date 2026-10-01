"""Measure trade_receive_to_webui_update_ms on the server (same clock as the collector).

A headless Chromium opens the real WebUI; the page records, for every live candle update it
applies, (browser time - collector receive time of that trade). Also captures two
screenshots of the forming candle moving inside one minute.

    SP2L_CHROMIUM=/usr/lib64/chromium-browser/headless_shell \\
        python scripts/live_latency.py --url http://127.0.0.1:4000 --seconds 300
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * (len(xs) - 1)))], 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:4000")
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--shots", default="docs/ui-screenshots")
    a = ap.parse_args()
    with sync_playwright() as p:
        exe = os.environ.get("SP2L_CHROMIUM")
        b = p.chromium.launch(executable_path=exe, headless=True, args=["--no-sandbox"])
        page = b.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"{a.url}/#/dashboard", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => window.sp2lLatency && window.sp2lLatency.length > 0", timeout=120000
        )
        # evidence: the same minute's candle before and after a few more live trades
        last = (
            "() => { const d = state.series.data(); const b = d[d.length - 1];"
            " return [b.time, b.open, b.high, b.low, b.close,"
            " document.querySelector('#chart-price').textContent]; }"
        )
        first = page.evaluate(last)
        page.screenshot(path=str(Path(a.shots) / "LIVE_server_before.png"))
        deadline = time.time() + 50
        second = first
        while time.time() < deadline:
            time.sleep(1)
            second = page.evaluate(last)
            if second[0] == first[0] and second[1:] != first[1:]:
                break
        page.screenshot(path=str(Path(a.shots) / "LIVE_server_after.png"))
        time.sleep(max(0, a.seconds - 50))
        lat = page.evaluate("() => window.sp2lLatency")
        creates = page.evaluate("() => window.sp2lChartCreates")
        b.close()
    print(
        json.dumps(
            {
                "samples": len(lat),
                "trade_receive_to_webui_update_ms": {
                    "p50": pct(lat, 0.5),
                    "p90": pct(lat, 0.9),
                    "p99": pct(lat, 0.99),
                    "max": pct(lat, 1.0),
                },
                "chart_created": creates,
                "same_minute_candle_before": first,
                "same_minute_candle_after": second,
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
