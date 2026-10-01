"""Service bookkeeping for the always-on collector: runs, restarts, heartbeats, logging.

Each process start inserts a collector_runs row. A previous run without ended_at means the
process died without a clean shutdown; this is logged as a restart. Heartbeats (DB + log)
report connection/coverage health every `interval` seconds. Reporting only: nothing here
affects coverage, DATA_GAP or strategy decisions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

log = logging.getLogger("sp2l.service")


def setup_logging(log_file: Path | None) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stderr = logging.StreamHandler()
    stderr.setFormatter(fmt)
    root.addHandler(stderr)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=10_000_000, backupCount=5)
        fh.setFormatter(fmt)
        root.addHandler(fh)


class RunRecorder:
    def __init__(
        self, engine: Engine, *, symbol: str, mode: str, spec_version: str, spec_sha256: str
    ) -> None:
        self.engine = engine
        now = datetime.now(UTC)
        with engine.begin() as c:
            prev = c.execute(
                text(
                    "SELECT id, started_at, ended_at, exit_reason FROM collector_runs"
                    " WHERE symbol = :s ORDER BY id DESC LIMIT 1"
                ),
                {"s": symbol},
            ).first()
            self.run_id: int = c.execute(
                text(
                    "INSERT INTO collector_runs (started_at, pid, host, symbol, mode,"
                    " spec_version, spec_sha256) VALUES (:t, :pid, :h, :s, :m, :v, :sha)"
                    " RETURNING id"
                ),
                {
                    "t": now,
                    "pid": os.getpid(),
                    "h": socket.gethostname(),
                    "s": symbol,
                    "m": mode,
                    "v": spec_version,
                    "sha": spec_sha256,
                },
            ).scalar_one()
        if prev is None:
            log.info("service start: first run (run_id=%s pid=%s)", self.run_id, os.getpid())
        elif prev[2] is None:
            log.warning(
                "service RESTART after unclean exit of run %s (started %s); "
                "downtime is a coverage gap",
                prev[0],
                prev[1].isoformat(),
            )
        else:
            log.info(
                "service start (run_id=%s); previous run %s ended %s (%s)",
                self.run_id,
                prev[0],
                prev[2].isoformat(),
                prev[3],
            )

    def heartbeat(self, h: dict[str, Any]) -> None:
        with self.engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO collector_heartbeats (run_id, ts, connected, healthy_until,"
                    " coverage_lag_ms, last_trade_exch_ts, trades_total, late_total, m1_ok,"
                    " m1_synthetic, m1_data_gap, m1_unanchored, gaps_total, reconnects, detail)"
                    " VALUES (:r, :ts, :connected, :healthy_until, :coverage_lag_ms,"
                    " :last_trade_exch_ts, :trades_total, :late_total, :m1_ok, :m1_synthetic,"
                    " :m1_data_gap, :m1_unanchored, :gaps_total, :reconnects,"
                    " CAST(:detail AS jsonb))"
                ),
                {
                    "r": self.run_id,
                    "ts": datetime.now(UTC),
                    **h,
                    "detail": json.dumps(
                        {
                            k: h.get(k)
                            for k in (
                                "connections",
                                "merged_status",
                                "duplicates",
                                "conflicts",
                                "multi_fill_sequences",
                                "orphans",
                                "host_sleeps",
                                "repair",
                                "gaps_repaired",
                                "gaps_unrecovered",
                                "rest",
                                "rest_polls",
                                "rest_errors",
                                "rest_trades",
                                "rest_only",
                                "ws_only",
                                "rest_revisions",
                                "trades_total",
                            )
                        },
                        default=str,
                    ),
                },
            )
        conns = h.get("connections") or {}
        log.info(
            "feeds %s merged=%s dup=%s conflicts=%s",
            " ".join(
                f"{n}:{'UP' if c['connected'] and c['confirmed'] else 'DOWN'}"
                f"(lag={c['coverage_lag_ms']}ms,disc={c['disconnects']})"
                for n, c in conns.items()
            ),
            h.get("merged_status"),
            h.get("duplicates"),
            h.get("conflicts"),
        )
        log.info(
            "health connected=%s lag_ms=%s trades=%s late=%s m1(ok/syn/gap/unanch)=%s/%s/%s/%s "
            "gaps=%s reconnects=%s",
            h["connected"],
            h["coverage_lag_ms"],
            h["trades_total"],
            h["late_total"],
            h["m1_ok"],
            h["m1_synthetic"],
            h["m1_data_gap"],
            h["m1_unanchored"],
            h["gaps_total"],
            h["reconnects"],
        )

    def ended(self, reason: str) -> None:
        with self.engine.begin() as c:
            c.execute(
                text("UPDATE collector_runs SET ended_at = :t, exit_reason = :r WHERE id = :id"),
                {"t": datetime.now(UTC), "r": reason, "id": self.run_id},
            )
        log.info("service stop (run_id=%s): %s", self.run_id, reason)


async def history_heal_loop(
    heal: Callable[[], dict[str, Any]],
    stop: asyncio.Event,
    interval: float = 600.0,
    first_after: float = 60.0,
) -> None:
    """Retry holes left by UNRECOVERED gaps with validated Tabdeal chart history (the audited
    V5.8 backfill: insert-only, M5 rebuilt, every decision recorded in gap_repairs), so a
    break heals by itself once history is reachable instead of forcing a new warmup."""
    wait = min(first_after, interval)  # also heal soon after a (re)start
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=wait)
        wait = interval
        if stop.is_set():
            return
        try:
            out = await asyncio.to_thread(heal)
        except Exception:  # never stops the collector; retried on the next interval
            log.exception("history heal pass failed")
            continue
        if out.get("repaired") or out.get("failed"):
            log.warning(
                "history heal: %s hole(s), repaired %s, still failing %s",
                out.get("holes"),
                [(r["from"], r["to"]) for r in out.get("repaired", [])],
                [(f["from"], f["to"], f.get("reason")) for f in out.get("failed", [])],
            )


async def history_bootstrap_task(
    bootstrap: Callable[[], dict[str, Any]],
    ready: Callable[[], bool],
    stop: asyncio.Event,
    *,
    poll: float = 60.0,
    retry: float = 300.0,
    attempts: int = 6,
) -> None:
    """Once per collector start: as soon as enough live canonical minutes exist to validate
    against (`ready`), fill the stored series from Tabdeal's chart (history bootstrap). A
    fetch failure is retried a few times; a validation failure is final (fail closed: the
    normal 150-bar warmup simply continues)."""
    tries = 0
    first = True
    while not stop.is_set():
        if not first:  # the first check runs immediately at start
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll)
        first = False
        if stop.is_set():
            return
        try:
            if not await asyncio.to_thread(ready):
                continue
            tries += 1
            out = await asyncio.to_thread(bootstrap)
        except Exception:
            log.exception("history bootstrap pass failed")
            out = {"failure": "EXCEPTION"}
        failure = str(out.get("failure") or "")
        if not failure:
            log.warning(
                "history bootstrap: %s chart minutes + %s synthetic no-trade minutes stored"
                " (%s .. %s), %s gap(s) left, validation %s",
                out.get("minutes_from_chart"),
                out.get("synthetic_no_trade_minutes"),
                out.get("chart_first"),
                out.get("to"),
                len(out.get("gaps_left") or []),
                out.get("validation"),
            )
            return
        if failure.startswith(("FETCH_FAILED", "EXCEPTION")) and tries < attempts:
            log.warning("history bootstrap: %s; retrying in %.0f s", failure, retry)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=retry)
            continue
        log.warning("history bootstrap not applied (%s); normal warmup continues", failure)
        return


async def heartbeat_loop(
    runs: RunRecorder,
    health: Callable[[], dict[str, object]],
    stop: asyncio.Event,
    interval: float = 30.0,
) -> None:
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
        runs.heartbeat(health())
