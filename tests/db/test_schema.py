"""DATA-01..03, SLTP-03, E1-06, E2-04, SHD-06, LIVE-01 enforced at the schema level."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = pytest.mark.db


def _seed_candidate(c) -> uuid.UUID:
    c.execute(text("INSERT INTO spec_versions (sha256, version) VALUES ('abc', '5.0')"))
    ids = []
    for m in range(3):
        ids.append(
            c.execute(
                text(
                    "INSERT INTO candles_1m (symbol, open_time, close_time, open, high, low, close,"
                    " volume, trade_count, finalized_at, source_hash) VALUES ('BTCUSDT',"
                    " '2026-01-01T00:00:00Z'::timestamptz + make_interval(mins => :m),"
                    " '2026-01-01T00:01:00Z'::timestamptz + make_interval(mins => :m),"
                    " 100, 101, 99, 100, 1, 1, now(), 'h') RETURNING id"
                ),
                {"m": m},
            ).scalar_one()
        )
    pgap = c.execute(
        text(
            "INSERT INTO pgaps (symbol, side, left_candle_id, middle_candle_id, right_candle_id,"
            " confirmed_at, promoted) VALUES ('BTCUSDT','LONG',:a,:b,:c, now(), true) RETURNING id"
        ),
        {"a": ids[0], "b": ids[1], "c": ids[2]},
    ).scalar_one()
    spike = c.execute(
        text(
            "INSERT INTO spikes (symbol, side, pgap_id, origin_candle_id, origin_low, origin_high,"
            " confirmed_at) VALUES ('BTCUSDT','LONG',:p,:o,99,101, now()) RETURNING id"
        ),
        {"p": pgap, "o": ids[0]},
    ).scalar_one()
    cid = uuid.uuid4()
    c.execute(
        text(
            "INSERT INTO candidates (id, symbol, side, mode, spike_id, spec_version, spec_sha256,"
            " created_at) VALUES (:id,'BTCUSDT','LONG','SHADOW',:s,'5.0','abc', now())"
        ),
        {"id": cid, "s": spike},
    )
    return cid


def _order(c, cid, leg, rev=0, status="NEW", reduce_only=None):
    c.execute(
        text(
            "INSERT INTO orders (candidate_id, mode, leg, leg_id, revision_id, client_order_id,"
            " side, order_type, price, qty, reduce_only, status, created_at, updated_at)"
            " VALUES (:c,'SHADOW',:leg,:leg,:rev,:coid,'BUY','LIMIT',100,1,:ro,:st, now(), now())"
        ),
        {
            "c": cid,
            "leg": leg,
            "rev": rev,
            "coid": f"{cid}-{leg}-{rev}",
            "ro": reduce_only,
            "st": status,
        },
    )


def test_no_second_target_columns_anywhere(conn):
    rows = (
        conn.execute(
            text(
                "SELECT table_schema||'.'||table_name||'.'||column_name FROM information_schema.columns"
                " WHERE table_schema IN ('public','cf') AND column_name ~* '(tp2|tp_2|partial_tp|trail|abcd)'"
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.parametrize("stmt", ["DELETE FROM candidates", "UPDATE candidates SET symbol='X'"])
def test_candidates_are_append_only(conn, stmt):
    _seed_candidate(conn)
    with pytest.raises(DBAPIError, match="append-only"):
        conn.execute(text(stmt))


def test_only_one_active_tp_and_e1_per_candidate(conn):
    cid = _seed_candidate(conn)
    _order(conn, cid, "TP")
    with pytest.raises(IntegrityError), conn.begin_nested():
        _order(conn, cid, "TP", rev=1)
    _order(conn, cid, "E1", rev=0, status="CANCELED")
    _order(conn, cid, "E1", rev=1)
    with pytest.raises(IntegrityError):
        _order(conn, cid, "E1", rev=2)


def test_entry_legs_can_never_be_reduce_only(conn):
    cid = _seed_candidate(conn)
    with pytest.raises(IntegrityError):
        _order(conn, cid, "E2", reduce_only=True)


def test_counterfactual_is_always_labelled(conn):
    cid = _seed_candidate(conn)
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO cf.counterfactual_outcomes (candidate_id, label, rejection_stage,"
                " simulator_version, spec_sha256, outcome) VALUES (:c,'TRADED','CONTEXT','v','abc','TP')"
            ),
            {"c": cid},
        )


def test_live_gate_requires_latest_run_of_every_item_to_pass(conn):
    def status():
        return conn.execute(text("SELECT status FROM live_automation_status")).scalar_one()

    assert status() == "LIVE_AUTOMATION_DISABLED"
    conn.execute(
        text(
            "INSERT INTO runtime_validation_runs (item, started_at, finished_at, passed,"
            " api_version, evidence) SELECT item, now(), now(), true, 't', '{}'"
            " FROM runtime_validation_items"
        )
    )
    assert status() == "LIVE_AUTOMATION_ELIGIBLE"
    conn.execute(
        text(
            "INSERT INTO runtime_validation_runs (item, started_at, finished_at, passed,"
            " api_version, evidence) VALUES ('CROSS_10X', now(), now() + interval '1 s', false,"
            " 't', '{}')"
        )
    )
    assert status() == "LIVE_AUTOMATION_DISABLED"


def test_shadow_initial_wallet_is_100(conn):
    conn.execute(text("INSERT INTO spec_versions (sha256, version) VALUES ('abc', '5.0')"))
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO shadow_sessions (id, symbol, spec_sha256, initial_wallet_usdt,"
                " started_at) VALUES (gen_random_uuid(), 'BTCUSDT', 'abc', 1000, now())"
            )
        )
