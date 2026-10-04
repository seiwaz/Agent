"""Market-data persistence: raw trades, canonical candles, data gaps (MKT-04, DATA-01).

All writes are idempotent (ON CONFLICT DO NOTHING) so the collector and a replay can both
write the same candles safely. A finalized candle is never silently changed (B05); V5.6 adds
auditable reconciliation: `reconcile()` keeps the previous version in candle_revisions and
promotes the corrected candle (a DB trigger rejects any other change), and M5 is always
rebuilt from the canonical M1 bars.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import Engine, text

from sp2l.core.types import Candle
from sp2l.marketdata.m1_builder import M1Result, M1Status, Quality, Trade
from sp2l.marketdata.m5_aggregator import M5Aggregator, M5Result, m5_bucket

Timeframe = Literal["1m", "5m"]
STEP = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5)}


def candle_hash(c: Candle) -> str:
    raw = "|".join(
        str(x)
        for x in (
            c.open_time.isoformat(),
            c.open,
            c.high,
            c.low,
            c.close,
            c.volume,
            c.trade_count,
            c.synthetic,
        )
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _same(a: object, b: object) -> bool:
    """Value equality across DB padding (numeric(38,18)) and Python types."""
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (ArithmeticError, ValueError):
        return str(a) == str(b)


class MarketStore:
    def __init__(self, engine: Engine, symbol: str) -> None:
        self.engine = engine
        self.symbol = symbol
        self.run_id: int | None = None  # set by the service (collector_runs)
        self._ids: dict[tuple[str, datetime], int] = {}

    def insert_trade(self, t: Trade, late: bool) -> None:
        with self.engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO raw_trades (symbol, trade_id, exch_ts, recv_ts, price, qty,"
                    " taker_side, late, raw, source) VALUES (:s, :id, :e, :r, :p, :q, :side,"
                    " :late, CAST(:raw AS jsonb), :src) ON CONFLICT DO NOTHING"
                ),
                {
                    "s": self.symbol,
                    "id": t.trade_id,
                    "e": t.exch_ts,
                    "r": t.recv_ts,
                    "p": t.price,
                    "q": t.qty,
                    "side": t.taker_side,
                    "late": late,
                    "raw": t.raw,
                    "src": t.source,
                },
            )

    def upsert_m1(self, m1: M1Result, lineage: bool = True) -> int | None:
        if m1.candle is None:
            return None
        q = m1.quality or (
            Quality.SYNTHETIC_NO_TRADE if m1.candle.synthetic else Quality.LIVE_PROVEN_RAW
        )
        lin = m1.lineage or {}
        extra: dict[str, object] = {"synthetic_no_trade": m1.candle.synthetic, "quality": q.value}
        if m1.repair_type:
            extra["repair_type"] = m1.repair_type
        if "ws_a" in lin:
            extra.update(
                ws_a_trade_count=lin["ws_a"],
                ws_b_trade_count=lin["ws_b"],
                rest_trade_count=lin["rest"],
                rest_only_trade_count=lin["rest_only"],
            )
        cid = self._upsert("1m", m1.candle, extra)
        if lineage:
            self.write_lineage(m1)
        return cid

    def write_lineage(self, m1: M1Result) -> None:
        """Per-trade source membership (A,B,REST) of a finalized minute."""
        sources = (m1.lineage or {}).get("sources")
        if isinstance(sources, dict) and sources:
            with self.engine.begin() as c:
                c.execute(
                    text(
                        "UPDATE raw_trades SET sources = :src, reconciled_at = now()"
                        " WHERE symbol = :s AND trade_id = :id"
                    ),
                    [{"s": self.symbol, "id": k, "src": v} for k, v in sources.items()],
                )

    # ---- V5.9 live chart channel (display only) ------------------------------------------------

    LIVE_CHANNEL = "sp2l_live"

    def notify_live(self, event: dict[str, object]) -> None:
        """Publish a display event (forming candle / final / revised) to API listeners."""
        payload = json.dumps(
            {"symbol": self.symbol, "sent_at": datetime.now(UTC).isoformat(), **event}
        )
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_notify(:ch, :p)"), {"ch": self.LIVE_CHANNEL, "p": payload})

    def notify_candle(self, ts: datetime) -> None:
        """The canonical candle of ts's minute, as stored now (after a revision)."""
        minute = ts.astimezone(UTC).replace(second=0, microsecond=0)
        with self.engine.connect() as c:
            r = c.execute(
                text(
                    "SELECT open, high, low, close, volume, trade_count, quality, revision"
                    " FROM candles_1m WHERE symbol = :s AND open_time = :t"
                ),
                {"s": self.symbol, "t": minute},
            ).first()
        if r is None:
            return
        self.notify_live(
            {
                "type": "revised",
                "t": minute.isoformat(),
                "status": "OK",
                "o": str(r[0]),
                "h": str(r[1]),
                "l": str(r[2]),
                "c": str(r[3]),
                "v": str(r[4]),
                "n": r[5],
                "quality": r[6],
                "revision": r[7],
            }
        )

    def record_latency(self, minute: datetime, stages: dict[str, float | None]) -> None:
        rows = [
            {"s": self.symbol, "m": minute, "st": k, "ms": v}
            for k, v in stages.items()
            if v is not None
        ]
        if rows:
            with self.engine.begin() as c:
                c.execute(
                    text(
                        "INSERT INTO pipeline_latency (symbol, minute, stage, ms)"
                        " VALUES (:s, :m, :st, :ms)"
                    ),
                    rows,
                )

    def upsert_m5(self, m5: M5Result) -> int | None:
        if m5.candle is None:
            return None
        q = m5.quality or Quality.LIVE_PROVEN_RAW
        return self._upsert(
            "5m",
            m5.candle,
            {
                "synthetic_m1_count": m5.synthetic_m1_count,
                "repaired_m1_count": m5.repaired_m1_count,
                "history_m1_count": m5.history_m1_count,
                "quality": q.value,
            },
        )

    def _upsert(self, tf: Timeframe, c: Candle, extra: dict[str, object]) -> int:
        key = (tf, c.open_time)
        if key in self._ids:
            return self._ids[key]
        cols = ", ".join(extra)
        vals = ", ".join(f":{k}" for k in extra)
        params = {
            "s": self.symbol,
            "ot": c.open_time,
            "ct": c.open_time + STEP[tf],
            "o": c.open,
            "h": c.high,
            "l": c.low,
            "c": c.close,
            "v": c.volume,
            "n": c.trade_count,
            "fa": datetime.now(UTC),
            "hash": candle_hash(c),
            **extra,
        }
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    f"INSERT INTO candles_{tf} (symbol, open_time, close_time, open, high, low,"
                    f" close, volume, trade_count, finalized_at, source_hash, {cols})"
                    f" VALUES (:s, :ot, :ct, :o, :h, :l, :c, :v, :n, :fa, :hash, {vals})"
                    " ON CONFLICT (symbol, open_time) DO NOTHING RETURNING id"
                ),
                params,
            ).first()
            if row is None:
                row = conn.execute(
                    text(
                        f"SELECT id, source_hash FROM candles_{tf}"
                        " WHERE symbol = :s AND open_time = :ot"
                    ),
                    params,
                ).first()
                assert row is not None
                if row[1] != params["hash"]:
                    raise RuntimeError(
                        f"candles_{tf} {c.open_time.isoformat()} already finalized differently"
                    )
        self._ids[key] = int(row[0])
        return int(row[0])

    def candle_id(self, tf: Timeframe, open_time: datetime) -> int | None:
        key = (tf, open_time)
        if key in self._ids:
            return self._ids[key]
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT id FROM candles_{tf} WHERE symbol = :s AND open_time = :ot"),
                {"s": self.symbol, "ot": open_time},
            ).first()
        if row is None:
            return None
        self._ids[key] = int(row[0])
        return int(row[0])

    # ---- market-event journal (V5.5 B39): the Shadow runner's causal input ---------------

    def journal(self, kind: str, ts: datetime, payload: dict[str, object]) -> int:
        with self.engine.begin() as c:
            seq: int = c.execute(
                text(
                    "INSERT INTO market_events (symbol, kind, ts, payload)"
                    " VALUES (:s, :k, :t, CAST(:p AS jsonb)) RETURNING seq"
                ),
                {"s": self.symbol, "k": kind, "t": ts, "p": json.dumps(payload)},
            ).scalar_one()
        return seq

    def journal_trade(self, t: Trade, late: bool, in_gap: bool) -> int:
        return self.journal(
            "TRADE",
            t.recv_ts,
            {
                "trade_id": t.trade_id,
                "exch_ts": t.exch_ts.isoformat(),
                "recv_ts": t.recv_ts.isoformat(),
                "price": str(t.price),
                "qty": str(t.qty),
                "late": late,
                "in_gap": in_gap,
                "source": t.source,
            },
        )

    def journal_m1(self, m1: M1Result) -> int:
        c = m1.candle
        payload: dict[str, object] = {
            "t": m1.open_time.isoformat(),
            "status": m1.status.value,
            "candle": None
            if c is None
            else {
                "t": c.open_time.isoformat(),
                "o": str(c.open),
                "h": str(c.high),
                "l": str(c.low),
                "c": str(c.close),
                "v": str(c.volume),
                "n": c.trade_count,
                "syn": c.synthetic,
            },
            "quality": m1.quality.value if m1.quality else None,
        }
        return self.journal("M1", datetime.now(UTC), payload)

    def journal_gap(self, start: datetime, ts: datetime, reason: str) -> int:
        return self.journal("GAP", ts, {"start": start.isoformat(), "reason": reason})

    def connection_event(
        self, conn: str, kind: str, ts: datetime, reason: str | None, detail: dict[str, object]
    ) -> None:
        with self.engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO feed_connection_events (run_id, symbol, conn, kind, ts, reason,"
                    " detail) VALUES (:r, :s, :c, :k, :t, :why, CAST(:d AS jsonb))"
                ),
                {
                    "r": self.run_id,
                    "s": self.symbol,
                    "c": conn,
                    "k": kind,
                    "t": ts,
                    "why": reason,
                    "d": json.dumps(detail, default=str),
                },
            )

    def conflict(self, trade_id: str, first: dict[str, object], other: dict[str, object]) -> None:
        with self.engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO feed_conflicts (run_id, symbol, ts, trade_id, first, other)"
                    " VALUES (:r, :s, now(), :t, CAST(:a AS jsonb), CAST(:b AS jsonb))"
                ),
                {
                    "r": self.run_id,
                    "s": self.symbol,
                    "t": trade_id,
                    "a": json.dumps(first, default=str),
                    "b": json.dumps(other, default=str),
                },
            )

    def insert_host_sleep(self, start: datetime, end: datetime) -> None:
        with self.engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO data_gaps (symbol, timeframe, kind, gap_start, gap_end, reason)"
                    " VALUES (:s, 'trades', 'HOST_SLEEP', :a, :b, 'HOST_SLEEP_SUSPECTED')"
                ),
                {"s": self.symbol, "a": start, "b": end},
            )

    # ---- V5.6 gap repair and candle reconciliation ------------------------------------------

    def record_repair(
        self,
        start: datetime,
        end: datetime,
        reason: str,
        status: str,
        method: str,
        failure: str | None,
        recovered: int,
        detail: dict[str, object],
    ) -> None:
        with self.engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO gap_repairs (run_id, symbol, gap_start, gap_end, reason, status,"
                    " method, failure, trades_recovered, detail) VALUES (:r, :s, :a, :b, :why,"
                    " :st, :m, :f, :n, CAST(:d AS jsonb))"
                ),
                {
                    "r": self.run_id,
                    "s": self.symbol,
                    "a": start,
                    "b": end,
                    "why": reason,
                    "st": status,
                    "m": method,
                    "f": failure,
                    "n": recovered,
                    "d": json.dumps(detail, default=str),
                },
            )
            c.execute(
                text(
                    "UPDATE data_gaps SET resolution = :st WHERE symbol = :s AND"
                    " kind = 'DATA_GAP' AND gap_start = :a AND gap_end = :b"
                ),
                {"st": status, "s": self.symbol, "a": start, "b": end},
            )
        self.journal(
            "GAP_RESOLVED",
            datetime.now(UTC),
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "status": status,
                "method": method,
                "failure": failure,
            },
        )

    def reconcile(
        self,
        tf: Timeframe,
        c: Candle,
        quality: Quality,
        *,
        source: str,
        reason: str,
        extra: dict[str, object] | None = None,
    ) -> str:
        """Promote `c` to canonical for its open_time, keeping any previous version.
        Returns INSERTED / REVISED / UNCHANGED. Never deletes and never rewrites silently."""
        extra = dict(extra or {})
        cols = ["open", "high", "low", "close", "volume", "trade_count", "quality", *extra]
        with self.engine.begin() as conn:
            old = conn.execute(
                text(
                    f"SELECT {', '.join(cols)}, revision FROM candles_{tf}"
                    " WHERE symbol = :s AND open_time = :ot FOR UPDATE"
                ),
                {"s": self.symbol, "ot": c.open_time},
            ).first()
            new = {
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "trade_count": c.trade_count,
                "quality": quality.value,
                **extra,
            }
            if old is None:
                ecols = ", ".join(extra)
                evals = ", ".join(f":{k}" for k in extra)
                conn.execute(
                    text(
                        f"INSERT INTO candles_{tf} (symbol, open_time, close_time, open, high,"
                        f" low, close, volume, trade_count, finalized_at, source_hash, quality,"
                        f" reconciled_at{', ' + ecols if ecols else ''}) VALUES (:s, :ot, :ct,"
                        f" :open, :high, :low, :close, :volume, :trade_count, now(), :hash,"
                        f" :quality, now(){', ' + evals if evals else ''})"
                    ),
                    {
                        "s": self.symbol,
                        "ot": c.open_time,
                        "ct": c.open_time + STEP[tf],
                        "hash": candle_hash(c),
                        **new,
                    },
                )
                self._ids.pop((tf, c.open_time), None)
                return "INSERTED"
            prev = dict(zip([*cols, "revision"], old, strict=True))
            if all(_same(prev[k], new[k]) for k in cols):
                return "UNCHANGED"
            rev = int(prev["revision"]) + 1
            conn.execute(
                text(
                    "INSERT INTO candle_revisions (symbol, timeframe, open_time, revision, old,"
                    " new, old_quality, new_quality, source, reason) VALUES (:s, :tf, :ot, :r,"
                    " CAST(:old AS jsonb), CAST(:new AS jsonb), :oq, :nq, :src, :why)"
                ),
                {
                    "s": self.symbol,
                    "tf": tf,
                    "ot": c.open_time,
                    "r": rev,
                    "old": json.dumps({k: prev[k] for k in cols}, default=str),
                    "new": json.dumps(new, default=str),
                    "oq": prev["quality"],
                    "nq": quality.value,
                    "src": source,
                    "why": reason,
                },
            )
            sets = ", ".join(f"{k} = :{k}" for k in new)
            conn.execute(
                text(
                    f"UPDATE candles_{tf} SET {sets}, source_hash = :hash, revision = :r,"
                    " reconciled_at = now() WHERE symbol = :s AND open_time = :ot"
                ),
                {"s": self.symbol, "ot": c.open_time, "r": rev, "hash": candle_hash(c), **new},
            )
        return "REVISED"

    def resume_point(
        self, now: datetime, max_age: timedelta
    ) -> tuple[datetime | None, list[M1Result]]:
        """V5.7: where a restarted collector continues the canonical series: right after the
        last canonical minute (if recent), with the final minutes of its unfinished M5 bucket."""
        with self.engine.connect() as c:
            last = c.execute(
                text("SELECT MAX(open_time) FROM candles_1m WHERE symbol = :s"), {"s": self.symbol}
            ).scalar()
            if last is None:
                return None, []
            nxt = last.astimezone(UTC) + STEP["1m"]
            if now - nxt > max_age:
                return None, []
            rows = c.execute(
                text(
                    "SELECT open_time, open, high, low, close, volume, trade_count,"
                    " synthetic_no_trade, quality FROM candles_1m WHERE symbol = :s AND"
                    " open_time >= :a AND open_time < :b ORDER BY open_time"
                ),
                {"s": self.symbol, "a": m5_bucket(nxt), "b": nxt},
            ).all()
        bucket = [
            M1Result(
                r[0].astimezone(UTC),
                M1Status.SYNTHETIC_NO_TRADE if r[7] else M1Status.OK,
                Candle(
                    r[0].astimezone(UTC),
                    r[1],
                    r[2],
                    r[3],
                    r[4],
                    r[5],
                    None if r[6] is None else int(r[6]),
                    synthetic=bool(r[7]),
                ),
                Quality(r[8]),
            )
            for r in rows
        ]
        expected = [m5_bucket(nxt) + i * STEP["1m"] for i in range(len(bucket))]
        if [m.open_time for m in bucket] != expected or len(bucket) != (
            nxt - m5_bucket(nxt)
        ) // STEP["1m"]:
            bucket = []  # the bucket already has a hole: M5 will be a gap anyway
        return nxt, bucket

    def recent_m1(self, before: datetime, n: int) -> list[M1Result]:
        """V5.8: the last `n` canonical minutes before `before` (tier-3 overlap reference)."""
        with self.engine.connect() as c:
            rows = c.execute(
                text(
                    "SELECT open_time, open, high, low, close, volume, trade_count,"
                    " synthetic_no_trade, quality FROM candles_1m WHERE symbol = :s AND"
                    " open_time < :b ORDER BY open_time DESC LIMIT :n"
                ),
                {"s": self.symbol, "b": before, "n": n},
            ).all()
        return [
            M1Result(
                r[0].astimezone(UTC),
                M1Status.SYNTHETIC_NO_TRADE if r[7] else M1Status.OK,
                Candle(
                    r[0].astimezone(UTC),
                    r[1],
                    r[2],
                    r[3],
                    r[4],
                    r[5],
                    None if r[6] is None else int(r[6]),
                    synthetic=bool(r[7]),
                ),
                Quality(r[8]),
            )
            for r in reversed(rows)
        ]

    def stored_trades_since(self, since: datetime) -> list[Trade]:
        """Canonical trades already stored for the minute a restarted collector resumes."""
        with self.engine.connect() as c:
            rows = c.execute(
                text(
                    "SELECT trade_id, exch_ts, recv_ts, price, qty, taker_side, source"
                    " FROM raw_trades WHERE symbol = :s AND exch_ts >= :a AND NOT late"
                    " AND source IN ('WS', 'REST')"
                    " AND (sources IS NULL OR sources NOT LIKE 'DUPLICATE_OF:%')"
                    " ORDER BY exch_ts, recv_ts, trade_id"
                ),
                {"s": self.symbol, "a": since},
            ).all()
        return [
            Trade(r[0], r[1].astimezone(UTC), r[2].astimezone(UTC), r[3], r[4], r[5], source=r[6])
            for r in rows
        ]

    def revise_m1_from_trades(self, ts: datetime, *, reason: str, source: str) -> str:
        """Recompute one final minute from its canonical trades (WS on time + REST) and promote
        it through an auditable revision, then rebuild its M5. Past decisions are untouched:
        the Shadow runner never consumes revisions (V5.7 L)."""
        minute = ts.astimezone(UTC).replace(second=0, microsecond=0)
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT price, qty FROM raw_trades WHERE symbol = :s AND exch_ts >= :a AND"
                    " exch_ts < :b AND (NOT late OR source = 'REST')"
                    " AND (sources IS NULL OR sources NOT LIKE 'DUPLICATE_OF:%')"
                    " ORDER BY exch_ts, recv_ts, trade_id"
                ),
                {"s": self.symbol, "a": minute, "b": minute + STEP["1m"]},
            ).all()
            cur = conn.execute(
                text("SELECT quality FROM candles_1m WHERE symbol = :s AND open_time = :t"),
                {"s": self.symbol, "t": minute},
            ).first()
        if not rows or cur is None:
            return "NO_CANONICAL_CANDLE"  # a DATA_GAP minute stays a gap (no fabrication)
        px = [r[0] for r in rows]
        c = Candle(
            minute,
            px[0],
            max(px),
            min(px),
            px[-1],
            sum((r[1] for r in rows), Decimal(0)),
            len(rows),
        )
        out = self.reconcile(
            "1m",
            c,
            Quality(cur[0]) if cur[0] != "LIVE_WS_ONLY" else Quality.LIVE_RECONCILED,
            source=source,
            reason=reason,
        )
        if out == "REVISED":
            self.rebuild_m5(minute, source=source, reason=reason)
        return out

    def rebuild_m5(self, bucket: datetime, *, source: str, reason: str) -> str:
        """Re-derive one M5 bar from the canonical M1 bars (never from anything else)."""
        bucket = m5_bucket(bucket)
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT open_time, open, high, low, close, volume, trade_count,"
                    " synthetic_no_trade, quality FROM candles_1m WHERE symbol = :s AND"
                    " open_time >= :a AND open_time < :b ORDER BY open_time"
                ),
                {"s": self.symbol, "a": bucket, "b": bucket + STEP["5m"]},
            ).all()
        if len(rows) < 5:
            return "INCOMPLETE"  # a bucket with a missing minute has no M5 (DATA_GAP)
        agg = M5Aggregator()
        m5: M5Result | None = None
        for r in rows:
            t = r[0].astimezone(UTC)
            q = Quality(r[8])
            m5 = agg.add(
                M1Result(
                    t,
                    M1Status.SYNTHETIC_NO_TRADE if r[7] else M1Status.OK,
                    Candle(
                        t,
                        r[1],
                        r[2],
                        r[3],
                        r[4],
                        r[5],
                        None if r[6] is None else int(r[6]),
                        synthetic=bool(r[7]),
                    ),
                    q,
                )
            )
        assert m5 is not None and m5.candle is not None
        worst = {Quality(r[8]) for r in rows}
        quality = next(
            (
                q
                for q in (
                    Quality.CONFLICTED,
                    Quality.TABDEAL_HISTORY_REPAIRED,
                    Quality.REPAIRED_TABDEAL,
                )
                if q in worst
            ),
            m5.quality or Quality.LIVE_PROVEN_RAW,
        )
        return self.reconcile(
            "5m",
            m5.candle,
            quality,
            source=source,
            reason=reason,
            extra={
                "synthetic_m1_count": m5.synthetic_m1_count,
                "repaired_m1_count": m5.repaired_m1_count,
                "history_m1_count": m5.history_m1_count,
            },
        )

    def insert_gap(self, start: datetime, end: datetime, reason: str, tf: str = "trades") -> None:
        with self.engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO data_gaps (symbol, timeframe, kind, gap_start, gap_end, reason)"
                    " VALUES (:s, :tf, 'DATA_GAP', :a, :b, :r)"
                ),
                {"s": self.symbol, "tf": tf, "a": start, "b": end, "r": reason},
            )
