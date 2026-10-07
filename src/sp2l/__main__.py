"""Command line.

    python -m sp2l [--config PATH] [--symbol S] collect [--duration S] [--log-file PATH]
    python -m sp2l [--config PATH] smc [--duration S] [--log-file PATH]
    python -m sp2l [--config PATH] [--symbol S] backtest [--days N] [--set key=value ...]
    python -m sp2l [--config PATH] [--symbol S] trend-backtest (--csv PATH | --days N) [--grid]
    python -m sp2l [--config PATH] trend [--duration S] [--log-file PATH]
    python -m sp2l [--config PATH] trend-status
    python -m sp2l [--config PATH] api [--host 127.0.0.1] [--port 8765]
    python -m sp2l [--config PATH] validate-readonly [--only ITEM,ITEM]
    python -m sp2l [--config PATH] reconcile-history [--apply]

`collect` runs the always-on Tabdeal market-data collector of ONE market (`--symbol`, default
the first of `symbols`) on the public stream (no API key): raw trades, candles, gaps and the
market-event journal (live chart + canonical M1). Run one collector per market.
`smc` runs the Smart Money Concepts engine for every configured market on one shared wallet:
it loads the full warmup from Tabdeal's chart history (no live warmup), analyses every
timeframe and creates / tracks M1 positions.
It requires validated maker_fee / taker_fee / slippage_allowance.
`backtest` replays the same engine over the stored history and prints the statistics.
`trend-backtest` runs System B (daily Donchian trend following, docs/TREND_STRATEGY.md) over a
daily CSV or the stored history aggregated to UTC days, against buy & hold.
`trend` runs System B on paper (`trend_live`): it keeps the Tabdeal history current and journals
the engine's decision after each closed UTC day; `trend-status` prints that state once.
`validate-readonly` performs authenticated READ-ONLY account checks and records the
evidence in runtime_validation_runs. It never places, cancels or modifies anything.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

from sp2l.config import ConfigError, RuntimeConfig
from sp2l.marketdata.collector import Collector, CollectorConfig, CollectorSink, utcnow
from sp2l.marketdata.history import HistoryPolicy
from sp2l.persistence.market_store import MarketStore
from sp2l.runtime.service import RunRecorder, heartbeat_loop, history_heal_loop, setup_logging
from sp2l.runtime.sinks import FanoutSink, MarketStoreSink
from sp2l.smc.model import VERSION

log = logging.getLogger("sp2l")


async def collect(args: argparse.Namespace, cfg: RuntimeConfig) -> None:
    params = cfg.smc_params()
    symbol = cfg.symbol
    db = create_engine(cfg.database_url, pool_pre_ping=True)
    store = MarketStore(db, symbol)
    sinks: list[CollectorSink] = [MarketStoreSink(store)]
    c = cfg.section("collector")
    rep = c.get("repair") or {}
    rc = c.get("rest_reconciliation") or {}
    hc = c.get("history_recovery") or {}
    history_on = bool(hc.get("enabled", False))
    history_max_gap = timedelta(seconds=float(hc.get("max_gap_s", 7 * 86400)))
    # V5.8 bootstrap: resume after the last canonical minute however old it is (within the
    # history window); tiers 2 and 3 then repair the interval before anything goes live
    resume_from, resume_m1 = store.resume_point(
        utcnow(), history_max_gap if history_on else timedelta(minutes=30)
    )
    if resume_from is not None:
        log.info("resuming the canonical series at %s (no warmup reset)", resume_from.isoformat())
    collector = Collector(
        CollectorConfig(
            symbol=symbol,
            grace=timedelta(milliseconds=int(c.get("grace_ms", 2000))),
            ping_interval=float(c.get("ping_interval_s", 1.0)),
            ping_timeout=float(c.get("ping_timeout_s", 3.0)),
            clock_safety=timedelta(milliseconds=int(c.get("clock_safety_ms", 1000))),
            connections=tuple(c.get("connections", ["A"])),
            stagger=float(c.get("stagger_s", 0)),
            repair_enabled=bool(rep.get("enabled", False)),
            repair_max_gap=timedelta(seconds=float(rep.get("max_gap_s", 120))),
            repair_delta_max=timedelta(milliseconds=int(rep.get("rest_delay_max_ms", 500))),
            repair_delta_min=timedelta(milliseconds=int(rep.get("rest_delay_min_ms", -100))),
            rest_enabled=bool(rc.get("enabled", False)),
            reconcile_deadline=timedelta(seconds=float(rc.get("reconcile_deadline_s", 60))),
            rest_stale=timedelta(seconds=float(rc.get("stale_s", 45))),
            rest_poll_min=float(rc.get("poll_min_s", 3)),
            rest_poll_max=float(rc.get("poll_max_s", 15)),
            rest_settle=timedelta(seconds=float(rc.get("settle_s", 1.0))),
            rest_cover_delta=timedelta(seconds=float(rc.get("cover_delta_s", 1.0))),
            rest_phase_align=bool(rc.get("phase_align", True)),
            history_enabled=history_on,
            history_max_gap=history_max_gap,
            history_deadline=timedelta(seconds=float(hc.get("deadline_s", 300))),
            history_policy=HistoryPolicy(
                settle=timedelta(seconds=float(hc.get("settle_s", 5))),
                min_overlap=int(hc.get("min_overlap_minutes", 5)),
                min_agreement=Fraction(str(hc.get("min_agreement", "0.8"))),
                max_rel_diff=Fraction(str(hc.get("max_hl_rel_diff", "0.001"))),
                overlap=timedelta(minutes=int(hc.get("overlap_minutes", 15))),
            ),
        ),
        FanoutSink(*sinks),
        resume_from=resume_from,
        resume_m1=resume_m1,
        resume_trades=(
            store.stored_trades_since(resume_from - timedelta(minutes=5)) if resume_from else None
        ),
        resume_recent=store.recent_m1(resume_from, 30) if resume_from else None,
    )
    runs = RunRecorder(
        db,
        symbol=symbol,
        mode="COLLECT",
        spec_version=VERSION,
        spec_sha256=params.digest(),
    )
    store.run_id = runs.run_id
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    reason = {"why": "STOPPED"}

    def request_stop(why: str) -> None:
        reason["why"] = why
        stop.set()

    loop.add_signal_handler(signal.SIGINT, request_stop, "SIGINT")
    loop.add_signal_handler(signal.SIGTERM, request_stop, "SIGTERM")
    if args.duration:
        loop.call_later(args.duration, request_stop, "DURATION_ELAPSED")
    log.info("collecting %s (%s params %s)", symbol, VERSION, params.digest())
    hb = asyncio.create_task(
        heartbeat_loop(
            runs, lambda: collector.health(utcnow()), stop, float(c.get("heartbeat_s", 30))
        )
    )
    from sp2l.marketdata.tabdeal_public import chart_history, recent_trades
    from sp2l.marketdata.tabdeal_ws import ws_market

    market = ws_market(symbol)
    heal = None
    boot = None
    bc = c.get("history_bootstrap") or {}
    if history_on and bool(bc.get("enabled", False)):
        from sp2l.runtime.reconcile import LIVE_QUALITIES, bootstrap_history
        from sp2l.runtime.service import history_bootstrap_task

        need = int(bc.get("validate_minutes", 60))
        lookback_s = float(bc.get("max_lookback_s", 7 * 86400))
        boot_timeout = float(bc.get("timeout_s", 180))

        def live_ready() -> bool:  # enough stored live minutes (any run) to validate against
            from sqlalchemy import text as _text

            with db.connect() as x:
                n: int = x.execute(
                    _text(
                        "SELECT COUNT(*) FROM candles_1m WHERE symbol = :s AND open_time >= :a"
                        " AND quality = ANY(:q) AND NOT COALESCE(synthetic_no_trade, false)"
                    ),
                    {
                        "s": symbol,
                        "a": utcnow() - timedelta(seconds=lookback_s),
                        "q": list(LIVE_QUALITIES),
                    },
                ).scalar_one()
            return int(n) >= min(need, int(bc.get("min_live_minutes", 5)))

        def boot_once() -> dict[str, Any]:
            return bootstrap_history(
                db,
                symbol,
                lambda a, b: chart_history(market, "1", a, b, timeout=boot_timeout),
                lookback=timedelta(seconds=lookback_s),
                apply=True,
                max_fill_gap=int(bc.get("max_fill_gap_minutes", 2)),
                validate_minutes=need,
            )

        boot = asyncio.create_task(
            history_bootstrap_task(boot_once, live_ready, stop, poll=10.0)  # at once, then 10 s
        )
    if history_on:
        from sp2l.runtime.reconcile import backfill_history

        def heal_once() -> dict[str, Any]:
            since = utcnow() - timedelta(hours=float(hc.get("heal_lookback_h", 24)))
            return backfill_history(
                db, symbol, since, lambda a, b: chart_history(market, "1", a, b), apply=True
            )

        heal = asyncio.create_task(
            history_heal_loop(heal_once, stop, float(hc.get("heal_interval_s", 600)))
        )
    try:
        await collector.run(
            stop,
            fetch_recent=lambda: recent_trades(market),
            fetch_history=lambda a, b: chart_history(market, "1", a, b),
        )
    finally:
        await hb
        if heal is not None:
            await heal
        if boot is not None:
            await boot
        runs.ended(reason["why"])


async def smc(args: argparse.Namespace, cfg: RuntimeConfig) -> None:
    from sp2l.marketdata.tabdeal_public import chart_history
    from sp2l.marketdata.tabdeal_ws import ws_market
    from sp2l.smc.runner import SmcRunner, run_smc
    from sp2l.smc.wallet import Wallet

    costs = cfg.costs()
    db = create_engine(cfg.database_url, pool_pre_ping=True)
    wallet = Wallet(db, cfg.smc_params(), cfg.symbols)
    log.info("shared wallet %s for %s", wallet.id, ", ".join(cfg.symbols))
    runners = []
    for sym in cfg.symbols:
        market = ws_market(sym)

        def fetch(a: Any, b: Any, m: str = market) -> Any:
            return chart_history(m, "1", a, b, timeout=180)

        runners.append(
            SmcRunner(db, sym, cfg.symbol_params(sym), costs, fetch, wallet, cfg.instrument(sym)[1])
        )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop.set)
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    if args.duration:
        loop.call_later(args.duration, stop.set)
    await run_smc(runners, stop)


def backtest(args: argparse.Namespace, cfg: RuntimeConfig) -> None:
    import json as _json
    from datetime import UTC, datetime

    from sp2l.marketdata.tabdeal_public import chart_history
    from sp2l.marketdata.tabdeal_ws import ws_market
    from sp2l.smc.backtest import run
    from sp2l.smc.history import ensure_history, load_bars, series_end
    from sp2l.smc.model import SmcParams

    base = cfg.symbol_params(cfg.symbol).as_dict()
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        base[k] = v
    params = SmcParams.from_mapping(base)
    db = create_engine(cfg.database_url)
    mk = ws_market(cfg.symbol)
    ensure_history(
        db, cfg.symbol, lambda a, b: chart_history(mk, "1", a, b, timeout=300), args.days
    )
    upto = series_end(db, cfg.symbol) or datetime.now(UTC)
    m1 = load_bars(db, cfg.symbol, "1m", int(args.days * 1440), upto)
    res = run(m1, params, cfg.costs())
    out = {"symbol": cfg.symbol, "params": params.digest(), "bars": len(m1), **res["stats"]}
    print(_json.dumps(out, indent=2))


GRID_AXES: dict[str, list[Any]] = {
    "entry_len": [10, 20, 30, 55],
    "exit_len": [5, 10, 20],
    "regime_ma": [0, 200],
    "sizing": ["risk", "full"],
}


def trend_backtest(args: argparse.Namespace, cfg: RuntimeConfig) -> None:
    """System B on daily bars: a CSV (`--csv`) or the stored Tabdeal history (`--days`)."""
    import csv as _csv
    import json as _json
    from datetime import UTC, datetime

    from sp2l.trend import backtest as tb
    from sp2l.trend.data import drop_forming, load_csv, quality
    from sp2l.trend.model import TrendParams

    base = dict(cfg.section("trend"))
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        base[k] = v
    try:
        params = TrendParams.from_mapping(base)
    except (TypeError, ValueError) as e:
        raise ConfigError(str(e)) from e
    if args.fee is not None and args.slippage is not None:
        fees = tb.Fees(args.fee, args.slippage)
    else:  # market orders: the taker fee and the measured slippage allowance of `costs`
        c = cfg.costs()
        fees = tb.Fees(
            float(c.taker_fee) if args.fee is None else args.fee,
            float(c.slippage) if args.slippage is None else args.slippage,
        )
    if args.csv:
        bars, q = load_csv(args.csv)
        bars = drop_forming(bars)
    else:
        from sp2l.smc.history import load_bars, series_end

        db = create_engine(cfg.database_url)
        upto = series_end(db, cfg.symbol) or datetime.now(UTC)
        bars = load_bars(db, cfg.symbol, "1d", int(args.days), upto, grid="utc")
        q = quality(bars, source=f"database {cfg.symbol} (1m -> UTC days)")
    funding = None
    if args.funding:
        from sp2l.trend.data import load_funding_csv

        funding = load_funding_csv(args.funding)
        q["funding_days"] = len(funding)
    res = tb.run(bars, params, fees, funding)
    out: dict[str, Any] = {"version": "TREND-1.0", "data": q, **tb.summary(res, fees, funding)}
    if args.grid:
        out["grid"] = tb.grid(bars, params, fees, GRID_AXES, funding)
    print(_json.dumps(out, indent=1))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "summary.json").write_text(_json.dumps(out, indent=1))
        with (args.out / "trades.csv").open("w", newline="") as f:
            rows = [t.as_dict() for t in res.trades]
            if rows:
                w = _csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
        with (args.out / "equity.csv").open("w", newline="") as f:
            w2 = _csv.writer(f)
            w2.writerow(["date", "close", "equity", "in_market"])
            for t, px, eq, m in zip(res.times, res.close, res.equity, res.in_market, strict=True):
                w2.writerow([t.date().isoformat(), px, round(eq, 2), int(m)])


async def trend(args: argparse.Namespace, cfg: RuntimeConfig) -> None:
    """System B paper service: keep the history current and journal each closed UTC day."""
    from sp2l.marketdata.tabdeal_public import chart_history
    from sp2l.marketdata.tabdeal_ws import ws_market
    from sp2l.trend import live as tl

    try:
        params, lc = tl.from_runtime(cfg)
    except ValueError as e:
        raise ConfigError(str(e)) from e
    db = create_engine(cfg.database_url, pool_pre_ping=True)
    market = ws_market(lc.symbol)

    def fetch(a: Any, b: Any) -> Any:
        return chart_history(market, "1", a, b, timeout=180)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop.set)
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    if args.duration:
        loop.call_later(args.duration, stop.set)
    await tl.run_trend(db, params, lc, fetch, stop)


def trend_status(cfg: RuntimeConfig) -> None:
    import json as _json

    from sp2l.trend import live as tl

    try:
        params, lc = tl.from_runtime(cfg)
    except ValueError as e:
        raise ConfigError(str(e)) from e
    s = tl.current(create_engine(cfg.database_url), params, lc)
    s.pop("curve", None)
    print(_json.dumps(s, indent=1, default=str))


def main() -> None:
    p = argparse.ArgumentParser(prog="sp2l", description="SMC trading-signal system")
    p.add_argument("--config", default="config/runtime.yaml")
    p.add_argument("--symbol", default=None, help="market for per-market commands (BTCUSDT)")
    sub = p.add_subparsers(dest="cmd", required=True)
    col = sub.add_parser("collect", help="run the market-data collector")
    col.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    col.add_argument("--log-file", type=Path, default=None)
    sm = sub.add_parser("smc", help="run the Smart Money Concepts engine (signals)")
    sm.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    sm.add_argument("--log-file", type=Path, default=None)
    bt = sub.add_parser("backtest", help="backtest the SMC engine on Tabdeal history")
    bt.add_argument("--days", type=float, default=30.0)
    bt.add_argument("--set", action="append", help="override a parameter: key=value")
    tl_ = sub.add_parser("trend", help="run System B on paper (journal each closed UTC day)")
    tl_.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    tl_.add_argument("--log-file", type=Path, default=None)
    sub.add_parser("trend-status", help="print System B's paper state from the stored history")
    tr = sub.add_parser("trend-backtest", help="backtest System B (daily trend following)")
    src = tr.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", type=Path, help="daily OHLC CSV (scripts/fetch_daily.py)")
    src.add_argument("--days", type=float, help="stored Tabdeal 1m history, as UTC days")
    tr.add_argument("--set", action="append", help="override a parameter: key=value")
    tr.add_argument("--fee", type=float, default=None, help="per fill (default: costs.taker_fee)")
    tr.add_argument("--slippage", type=float, default=None, help="per fill (default: costs)")
    tr.add_argument("--funding", type=Path, default=None, help="futures funding rates CSV")
    tr.add_argument("--grid", action="store_true", help="add the sensitivity table")
    tr.add_argument("--out", type=Path, default=None, help="write summary/trades/equity here")
    api = sub.add_parser("api", help="serve the read-only API and WebUI")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8765)
    fr = sub.add_parser("feed-report", help="B46 transport-redundancy measurement report")
    fr.add_argument("--hours", type=float, default=24.0)
    rh = sub.add_parser(
        "reconcile-history", help="V5.6 audited correction of stored candles (dry run by default)"
    )
    rh.add_argument("--apply", action="store_true", help="write revisions (default: report only)")
    rh.add_argument(
        "--dedupe-rest-since", default=None, help="V5.7: re-align REST-only rows since ISO time"
    )
    bf = sub.add_parser(
        "backfill-history", help="V5.8: fill stored holes from validated Tabdeal chart history"
    )
    bf.add_argument("--since", required=True, help="ISO time")
    bf.add_argument("--apply", action="store_true", help="write (default: report only)")
    bs = sub.add_parser(
        "bootstrap-history",
        help="V5.12: fill the canonical series from validated Tabdeal chart history (cold start)",
    )
    bs.add_argument("--lookback-s", type=float, default=7 * 86400)
    bs.add_argument("--validate-minutes", type=int, default=60)
    bs.add_argument("--timeout-s", type=float, default=180)
    bs.add_argument("--apply", action="store_true", help="write (default: report only)")
    sub.add_parser(
        "trade-token", help="write a new token that lets the dashboard trade (trading.token_file)"
    )
    ro = sub.add_parser("validate-readonly", help="authenticated READ-ONLY Tabdeal checks")
    ro.add_argument("--only", default=None, help="comma-separated items, e.g. CROSS_10X")
    args = p.parse_args()
    setup_logging(getattr(args, "log_file", None))
    try:
        cfg = RuntimeConfig.load(Path(args.config))
        if args.symbol:
            if args.cmd in ("smc", "api"):
                raise ConfigError("--symbol is for per-market commands; smc/api run every market")
            cfg = cfg.with_symbol(args.symbol)
        if args.cmd == "collect":
            asyncio.run(collect(args, cfg))
        elif args.cmd == "smc":
            cfg.costs()  # fail fast with a clear message
            asyncio.run(smc(args, cfg))
        elif args.cmd == "backtest":
            backtest(args, cfg)
        elif args.cmd == "trend-backtest":
            trend_backtest(args, cfg)
        elif args.cmd == "trend":
            asyncio.run(trend(args, cfg))
        elif args.cmd == "trend-status":
            trend_status(cfg)
        elif args.cmd == "api":
            import uvicorn

            from sp2l.api.app import create_app

            # open SSE (live chart) clients never hold a restart for systemd's 90 s stop timeout
            uvicorn.run(
                create_app(cfg),
                host=args.host,
                port=args.port,
                log_level="info",
                timeout_graceful_shutdown=5,
            )
        elif args.cmd == "feed-report":
            import json as _json
            from datetime import UTC, datetime

            from sp2l.runtime.feed_report import report

            db = create_engine(cfg.database_url)
            since = datetime.now(UTC) - timedelta(hours=args.hours)
            print(_json.dumps(report(db, cfg.symbol, since), indent=2, default=str))
        elif args.cmd == "reconcile-history":
            import json as _json

            from sp2l.runtime.reconcile import reconcile_history

            db = create_engine(cfg.database_url)
            if args.dedupe_rest_since:
                from datetime import datetime as _dt

                from sp2l.runtime.reconcile import dedupe_rest_history

                rep_ = dedupe_rest_history(
                    db, cfg.symbol, _dt.fromisoformat(args.dedupe_rest_since), apply=args.apply
                )
                print(_json.dumps(rep_, indent=2, default=str))
            else:
                print(_json.dumps(reconcile_history(db, cfg.symbol, apply=args.apply), indent=2))
        elif args.cmd == "backfill-history":
            import json as _json
            from datetime import datetime as _dt

            from sp2l.marketdata.tabdeal_public import chart_history
            from sp2l.marketdata.tabdeal_ws import ws_market
            from sp2l.runtime.reconcile import backfill_history

            db = create_engine(cfg.database_url)
            mk = ws_market(cfg.symbol)
            rep_ = backfill_history(
                db,
                cfg.symbol,
                _dt.fromisoformat(args.since),
                lambda a, b: chart_history(mk, "1", a, b),
                apply=args.apply,
            )
            print(_json.dumps(rep_, indent=2, default=str))
        elif args.cmd == "bootstrap-history":
            import json as _json
            from datetime import timedelta as _td

            from sp2l.marketdata.tabdeal_public import chart_history
            from sp2l.marketdata.tabdeal_ws import ws_market
            from sp2l.runtime.reconcile import bootstrap_history

            db = create_engine(cfg.database_url)
            mk = ws_market(cfg.symbol)
            hb = cfg.section("collector").get("history_bootstrap") or {}
            rep_ = bootstrap_history(
                db,
                cfg.symbol,
                lambda a, b: chart_history(mk, "1", a, b, timeout=args.timeout_s),
                lookback=_td(seconds=args.lookback_s),
                apply=args.apply,
                max_fill_gap=int(hb.get("max_fill_gap_minutes", 2)),
                validate_minutes=args.validate_minutes,
            )
            print(_json.dumps(rep_, indent=2, default=str))
        elif args.cmd == "validate-readonly":
            from sp2l.validation.readonly import run_readonly_checks

            only = set(args.only.split(",")) if args.only else None
            run_readonly_checks(cfg, only=only)
        elif args.cmd == "trade-token":
            from sp2l.trading.manager import TradingConfig, new_token

            try:
                tc = TradingConfig.from_mapping(dict(cfg.section("trading")))
            except ValueError as e:
                raise ConfigError(str(e)) from e
            path = Path(tc.token_file).expanduser()
            print(new_token(path))
            print(f"(saved in {path}; enter it once in the dashboard's trade dialog)",
                  file=sys.stderr)
    except ConfigError as e:
        log.error("%s", e)
        sys.exit(2)


if __name__ == "__main__":
    main()
