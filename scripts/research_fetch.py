"""Read-only: cache Tabdeal 1-minute chart history for the research markets (local database).

Uses the engine's history loader (requests with a lead, inner holes re-fetched); a full
re-fetch also overwrites rows stored before those fixes. Only GET requests.

    uv run python scripts/research_fetch.py [--markets BTCUSDT,...] [--days N]
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine

from sp2l.config import RuntimeConfig
from sp2l.marketdata.tabdeal_public import chart_history
from sp2l.marketdata.tabdeal_ws import ws_market
from sp2l.smc.history import coverage, fetch_range, gaps, repair_gaps

# measured 2026-10-05: the oldest 1-minute bar the chart still returns, in days
COVERAGE = {
    "BTCUSDT": 296, "XRPUSDT": 296, "ETHUSDT": 296, "SOLUSDT": 253, "DOGEUSDT": 253,
    "ADAUSDT": 253, "BNBUSDT": 202, "LTCUSDT": 197, "AVAXUSDT": 167,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default=",".join(COVERAGE))
    args = ap.parse_args()
    cfg = RuntimeConfig.load(Path("config/runtime.yaml"))
    db = create_engine(cfg.database_url)
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    out = {}
    for sym in args.markets.split(","):
        mk = ws_market(sym)

        def fetch(a: datetime, b: datetime, mk: str = mk) -> list[dict]:
            return chart_history(mk, "1", a, b, timeout=120)

        start = now - timedelta(days=COVERAGE[sym])
        n = fetch_range(db, sym, fetch, start, now, now)
        r = repair_gaps(db, sym, fetch, start, now)
        first, last = coverage(db, sym)
        holes = gaps(db, sym, start, now)
        out[sym] = {
            "stored": n, "repaired": r, "first": str(first), "last": str(last),
            "holes_left": [(str(a), str(b)) for a, b in holes],
        }
        print(sym, out[sym], flush=True)
    Path("docs/research/coverage.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
