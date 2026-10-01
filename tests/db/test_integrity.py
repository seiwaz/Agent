"""V5.6 candle reconciliation, lineage and historical multi-fill recovery (Postgres)."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from sp2l.core.types import Candle
from sp2l.marketdata.m1_builder import M1Result, M1Status, Quality
from sp2l.persistence.market_store import MarketStore
from sp2l.runtime.reconcile import reconcile_history
from tests.conftest import T0

pytestmark = pytest.mark.db


def c(t, o, h, lo, cl, v="1", n=3):
    return Candle(t, D(o), D(h), D(lo), D(cl), D(v), n)


@pytest.fixture()
def store(engine):
    return MarketStore(engine, "BTCUSDT")  # each test owns a disjoint time range


DAY = timedelta(days=1)


def minutes(store, n=5, start=T0):
    for i in range(n):
        t = start + timedelta(minutes=i)
        store.upsert_m1(
            M1Result(t, M1Status.OK, c(t, "100", "101", "99", "100"), Quality.LIVE_PROVEN_RAW)
        )


def test_revision_preserves_the_original_and_promotes_the_correction(store, engine):
    t0 = T0 + DAY
    minutes(store, start=t0)
    new = c(t0, "100", "103", "99", "102", v="1.5", n=4)
    assert (
        store.reconcile("1m", new, Quality.REPAIRED_TABDEAL, source="test", reason="R") == "REVISED"
    )
    with engine.connect() as x:
        row = x.execute(
            text("SELECT high, close, quality, revision FROM candles_1m WHERE open_time = :t"),
            {"t": t0},
        ).one()
        rev = x.execute(
            text(
                "SELECT old, new, old_quality, new_quality, reason FROM candle_revisions WHERE open_time = :t"
            ),
            {"t": t0},
        ).one()
    assert (row[0], row[1], row[2], row[3]) == (D("103"), D("102"), "REPAIRED_TABDEAL", 1)
    assert D(rev[0]["high"]) == D("101") and D(rev[1]["high"]) == D("103")  # old version kept
    assert (rev[2], rev[3], rev[4]) == ("LIVE_PROVEN_RAW", "REPAIRED_TABDEAL", "R")
    assert (
        store.reconcile("1m", new, Quality.REPAIRED_TABDEAL, source="test", reason="R")
        == "UNCHANGED"
    )


def test_canonical_candles_cannot_change_silently_or_be_deleted(store, engine):
    t0 = T0 + 2 * DAY
    minutes(store, 1, t0)
    for sql in (
        "UPDATE candles_1m SET high = 999 WHERE open_time = :t",
        "DELETE FROM candles_1m WHERE open_time = :t",
    ):
        with pytest.raises(DBAPIError), engine.begin() as x:
            x.execute(text(sql), {"t": t0})


def test_m1_repair_rebuilds_the_dependent_m5(store, engine):
    t0 = T0 + 3 * DAY
    minutes(store, 5, t0)
    assert store.rebuild_m5(t0, source="test", reason="init") == "INSERTED"
    t2 = t0 + timedelta(minutes=2)
    store.reconcile(
        "1m",
        c(t2, "100", "108", "95", "100", v="2"),
        Quality.REPAIRED_TABDEAL,
        source="test",
        reason="fix",
    )
    assert store.rebuild_m5(t0, source="test", reason="M1 repaired") == "REVISED"
    with engine.connect() as x:
        m5 = x.execute(
            text(
                "SELECT high, low, volume, quality, repaired_m1_count, revision FROM candles_5m WHERE open_time = :t"
            ),
            {"t": t0},
        ).one()
    assert (m5[0], m5[1], m5[2], m5[3], m5[4], m5[5]) == (
        D("108"),
        D("95"),
        D("6"),
        "REPAIRED_TABDEAL",
        1,
        1,
    )


def test_incomplete_bucket_is_never_fabricated(store):
    minutes(store, 4, T0 + 4 * DAY)
    assert store.rebuild_m5(T0 + 4 * DAY, source="test", reason="x") == "INCOMPLETE"


def test_reconcile_history_recovers_logged_multi_fills_and_marks_pre_fix_data(store, engine):
    with engine.begin() as x:
        r_old = x.execute(
            text(
                "INSERT INTO collector_runs (started_at, pid, host, symbol, mode, spec_version, spec_sha256) VALUES (:t, 1, 'h', 'BTCUSDT', 'COLLECT', '5.6', 'x') RETURNING id"
            ),
            {"t": T0},
        ).scalar()
        r_fix = x.execute(
            text(
                "INSERT INTO collector_runs (started_at, pid, host, symbol, mode, spec_version, spec_sha256) VALUES (:t, 2, 'h', 'BTCUSDT', 'COLLECT', '5.6', 'x') RETURNING id"
            ),
            {"t": T0 + timedelta(minutes=10)},
        ).scalar()
        ts = T0 + timedelta(seconds=30)
        # pre-fix: the first fill of sequence 7 was stored, the second dropped but logged
        x.execute(
            text(
                "INSERT INTO raw_trades (symbol, trade_id, exch_ts, recv_ts, price, qty) VALUES ('BTCUSDT', '7', :e, :e, 100, 1)"
            ),
            {"e": ts},
        )
        x.execute(
            text(
                "INSERT INTO feed_conflicts (run_id, symbol, ts, trade_id, first, other) VALUES (:r, 'BTCUSDT', :t, '7', CAST(:a AS jsonb), CAST(:b AS jsonb))"
            ),
            {
                "r": r_old,
                "t": ts,
                "a": json.dumps({"fingerprint": ["7", ts.isoformat(), "100", "1"]}),
                "b": json.dumps({"conn": "A", "fingerprint": ["7", ts.isoformat(), "105", "2"]}),
            },
        )
        x.execute(
            text(
                "INSERT INTO feed_conflicts (run_id, symbol, ts, trade_id, first, other) VALUES (:r, 'BTCUSDT', :t, '9', CAST(:a AS jsonb), CAST(:b AS jsonb))"
            ),
            {
                "r": r_fix,
                "t": T0 + timedelta(minutes=11),
                "a": json.dumps(
                    {
                        "fingerprint": ["9", "x", "1", "1"],
                        "classification": "MULTI_FILL_SAME_SEQUENCE",
                    }
                ),
                "b": json.dumps({"fingerprint": ["9", "x", "2", "1"]}),
            },
        )
    store.upsert_m1(
        M1Result(
            T0, M1Status.OK, c(T0, "100", "100", "100", "100", v="1", n=1), Quality.LIVE_PROVEN_RAW
        )
    )
    minutes(store, 4, T0 + timedelta(minutes=1))
    dry = reconcile_history(engine, "BTCUSDT")
    assert dry["fills_to_restore"] == 1 and dry["m1"]["value_changes"] == 1 and not dry["applied"]
    out = reconcile_history(engine, "BTCUSDT", apply=True)
    assert out["m1"]["REVISED"] == 5 and out["m5"]["REVISED"] + out["m5"]["INSERTED"] == 1
    with engine.connect() as x:
        k = x.execute(
            text("SELECT high, volume, trade_count, quality FROM candles_1m WHERE open_time = :t"),
            {"t": T0},
        ).one()
        src = x.execute(text("SELECT source FROM raw_trades WHERE trade_id LIKE '7:%'")).scalar()
        q = {
            r[0]
            for r in x.execute(
                text("SELECT DISTINCT quality FROM candles_1m WHERE open_time < :f"),
                {"f": T0 + timedelta(minutes=10)},
            )
        }
    assert (k[0], k[1], k[2], k[3]) == (D("105"), D("3"), 2, "CONFLICTED")
    assert src == "RECOVERED_CONFLICT_LOG" and q == {"CONFLICTED"}
    again = reconcile_history(engine, "BTCUSDT", apply=True)  # idempotent
    assert again["fills_to_restore"] == 0 and again["m1"]["REVISED"] == 0


def _m5_history(store, start, n):
    for i in range(n * 5):
        t = start + timedelta(minutes=i)
        px = D(100) + D(i % 11) - D(i % 7)
        store.upsert_m1(
            M1Result(
                t,
                M1Status.OK,
                c(t, str(px), str(px + 2), str(px - 2), str(px + 1)),
                Quality.LIVE_RECONCILED,
            )
        )
    for b in range(n):
        store.rebuild_m5(start + timedelta(minutes=5 * b), source="test", reason="history")


def test_restart_with_trusted_history_in_the_db_is_immediately_context_ready(store, engine):
    """Warmup is a property of the stored history, not of process uptime (V5.7 I/N)."""
    from datetime import UTC, datetime

    from sp2l.engine.symbol_engine import ShadowSymbolEngine
    from sp2l.runtime.feed_report import m5_segments
    from sp2l.runtime.shadow_service import ShadowRunner
    from sp2l.strategy.risk.engine import CostModel, ExchangeFilters

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=5 * 160)
    start -= timedelta(minutes=start.minute % 5)
    _m5_history(store, start, 160)
    seg = m5_segments(engine, "BTCUSDT", now - timedelta(hours=24))
    assert seg["current_bars"] >= 150 and seg["target_reached"]  # the UI's warmup reads this
    with engine.begin() as x:  # the journal holds the next M1 (a new process after a restart)
        x.execute(
            text(
                "INSERT INTO market_events (symbol, kind, ts, payload) VALUES ('BTCUSDT', 'M1', now(), CAST(:p AS jsonb))"
            ),
            {
                "p": json.dumps(
                    {
                        "t": (start + timedelta(minutes=5 * 160)).isoformat(),
                        "status": "OK",
                        "candle": None,
                    }
                )
            },
        )
    eng = ShadowSymbolEngine(
        "BTCUSDT",
        tick=D("0.1"),
        costs=CostModel(D("0.0004"), D("0.0006"), D("0.0005")),
        filters=ExchangeFilters(D("0.1"), D("0.001"), None, None, False),
        warmup_bars=150,
    )
    ShadowRunner._prime_new(engine, eng, "BTCUSDT")
    assert eng.m5.warm and len(eng.m5.segment.bars) >= 150  # READY at once, no 0/150


def test_collector_resume_point_continues_the_series_and_its_m5_bucket(store):
    from datetime import UTC, datetime

    now = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(days=30)
    b = now - timedelta(minutes=now.minute % 5) - timedelta(minutes=10)
    minutes(store, 7, b)  # one full bucket + 2 minutes of the next
    nxt, bucket = store.resume_point(b + timedelta(minutes=8), timedelta(minutes=30))
    assert nxt == b + timedelta(minutes=7)
    assert [m.open_time for m in bucket] == [b + timedelta(minutes=5), b + timedelta(minutes=6)]
    assert store.resume_point(b + timedelta(hours=2), timedelta(minutes=30)) == (None, [])


def test_reconciliation_never_rewrites_past_trading_records(store, engine):
    tables = (
        "candidates",
        "candidate_transitions",
        "strategy_events",
        "orders",
        "fills",
        "shadow_wallet_ledger",
        "context_snapshots",
        "exhaustion_snapshots",
    )
    with engine.connect() as x:
        before = {t: x.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in tables}
    t0 = T0 + 5 * DAY
    minutes(store, 5, t0)
    with engine.begin() as x:
        x.execute(
            text(
                "INSERT INTO raw_trades (symbol, trade_id, exch_ts, recv_ts, price, qty, source) VALUES ('BTCUSDT', 'R:x', :e, :e, 130, 1, 'REST')"
            ),
            {"e": t0 + timedelta(seconds=20)},
        )
    assert (
        store.revise_m1_from_trades(
            t0, reason="REST_RECONCILIATION_AFTER_FINALIZATION", source="REST"
        )
        == "REVISED"
    )
    with engine.connect() as x:
        after = {t: x.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in tables}
        hi = x.execute(text("SELECT high FROM candles_1m WHERE open_time = :t"), {"t": t0}).scalar()
    assert before == after and hi == D("130")  # history corrected, decisions untouched


def test_rest_duplicates_from_the_first_identity_rule_are_marked_and_revised(store, engine):
    from sp2l.runtime.reconcile import dedupe_rest_history

    t0 = T0 + 6 * DAY
    e = t0 + timedelta(seconds=11.99)
    with engine.begin() as x:
        x.execute(
            text(
                "INSERT INTO raw_trades (symbol, trade_id, exch_ts, recv_ts, price, qty, source, sources) VALUES"
                " ('BTCUSDT', 'W1', :e, :e, 100, 1, 'WS', 'A'),"
                " ('BTCUSDT', 'R:dup', :r, :e, 100, 1, 'REST', 'REST')"
            ),
            {"e": e, "r": e + timedelta(milliseconds=620)},
        )
    store.upsert_m1(
        M1Result(
            t0, M1Status.OK, c(t0, "100", "100", "100", "100", v="2", n=2), Quality.LIVE_RECONCILED
        )
    )
    out = dedupe_rest_history(engine, "BTCUSDT", t0, apply=True)
    assert out["duplicates"] == 1 and out["revisions"][t0.isoformat()] == "REVISED"
    with engine.connect() as x:
        k = x.execute(
            text("SELECT volume, trade_count, revision FROM candles_1m WHERE open_time = :t"),
            {"t": t0},
        ).one()
        src = dict(
            x.execute(
                text("SELECT trade_id, sources FROM raw_trades WHERE trade_id IN ('W1', 'R:dup')")
            ).all()
        )
    assert (k[0], k[1], k[2]) == (D("1"), 1, 1)  # one trade, audited revision
    assert src["R:dup"] == "DUPLICATE_OF:W1" and "REST" in src["W1"]  # kept, never deleted
