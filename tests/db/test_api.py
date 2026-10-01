"""Read-only API over a recorded Shadow replay (UI-02: backend truth only)."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from sp2l.api.app import create_app
from sp2l.config import RuntimeConfig
from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.persistence.market_store import MarketStore
from sp2l.persistence.shadow_recorder import PostgresShadowRecorder
from sp2l.runtime.replay import replay
from sp2l.strategy.risk.engine import CostModel, ExchangeFilters
from tests.conftest import T0
from tests.db.conftest import URL
from tests.replay.tape import tape

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def client(engine):
    store = MarketStore(engine, "BTCUSDT")
    session = uuid.uuid4()
    rec = PostgresShadowRecorder(
        engine,
        store,
        symbol="BTCUSDT",
        spec_version="5.4",
        spec_sha256="api-test",
        session_id=session,
        started_at=T0,
    )
    eng = ShadowSymbolEngine(
        "BTCUSDT",
        tick=D("0.1"),
        costs=CostModel(D("0.0001"), D("0.0001"), D("0.0005")),
        filters=ExchangeFilters(D("0.1"), D("0.001"), None, None, False),
        warmup_bars=30,
        recorder=rec,
        session_id=str(session),
    )
    end = T0 + timedelta(minutes=600)
    replay(eng, tape(600, 3), start=T0, end=end + timedelta(minutes=1), healthy_until=end)
    eng.close()
    app = create_app(RuntimeConfig({"database_url": URL, "symbol": "BTCUSDT"}))
    return TestClient(app), eng


def test_overview_reports_spec_live_and_wallet(client):
    c, eng = client
    o = c.get("/api/overview").json()
    assert o["live"]["status"] == "LIVE_AUTOMATION_DISABLED"
    assert o["spec"]["version"] == "6.0" and len(o["spec"]["rules_sha256"]) == 64
    assert D(o["wallet"]["balance"]) == eng.wallet_balance().quantize(D("1e-18"))


def test_setup_detail_has_flow_gates_and_levels(client):
    c, eng = client
    closed = next(m for m in eng.finished if m.state.value == "CLOSED")
    d = c.get(f"/api/setups/{closed.cfg.setup_id}").json()
    assert [f["stage"] for f in d["flow"]] == [
        "SPIKE",
        "CONTEXT",
        "EXHAUSTION",
        "E1",
        "PULLBACK",
        "E1_FILL",
        "E2",
        "POSITION",
    ]
    assert d["flow"][-1]["status"] == "done"
    assert d["context_gates"][-1]["gate"] == "CONTEXT RESULT"
    assert d["exhaustion_gates"][-1]["gate"] == "EXHAUSTION RESULT"
    assert d["levels"]["tp"] and d["e1_revisions"]


def test_rejected_bucket_carries_reason_codes_and_counterfactuals(client):
    c, _ = client
    items = c.get("/api/setups", params={"bucket": "REJECTED_CONTEXT"}).json()["items"]
    assert items and all(i["primary_reason"] for i in items)
    cf = c.get("/api/counterfactuals").json()
    assert cf["label"] == "COUNTERFACTUAL" and cf["items"]


def test_market_quality_and_collector_endpoints(client):
    c, _ = client
    assert c.get("/api/market/candles", params={"tf": "5m"}).status_code == 200
    q = c.get("/api/market/quality", params={"minutes": 60}).json()
    assert set(q["counts"]) == {"OK", "SYNTHETIC", "DATA_GAP"} and len(q["timeline"]) == 60
    assert c.get("/api/collector").json()["status"] in (
        "NO_DATA",
        "STALE",
        "CONNECTED",
        "DISCONNECTED",
    )
    assert c.get("/api/validation").status_code == 200


def test_api_is_read_only_and_serves_ui(client):
    c, _ = client
    for method in ("post", "put", "patch", "delete"):
        assert getattr(c, method)("/api/overview").status_code == 405
    r = c.get("/")
    assert r.status_code == 200 and "SP2L Dashboard" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert c.get("/static/app.js").status_code == 200


def test_no_credentials_in_any_response(client):
    c, _ = client
    blob = json.dumps(
        [
            c.get(p).json()
            for p in ("/api/overview", "/api/setups", "/api/validation", "/api/collector")
        ]
    )
    assert "SP2L_TABDEAL" not in blob and "api_secret" not in blob.lower()


def test_overview_has_four_separate_status_concepts_and_a_blocker(client):
    c, _ = client
    sys_ = c.get("/api/overview").json()["system"]
    for k in ("data", "strategy", "shadow", "live"):
        assert {"state", "label", "tone"} <= set(sys_[k])
        assert sys_[k]["tone"] in ("ok", "warn", "bad", "info", "neutral")
    assert sys_["live"]["code"] == "LIVE_AUTOMATION_DISABLED"
    assert sys_["live"]["label"] == "Disabled"  # never the raw enum as the primary label
    assert sys_["blocker"]["text"] and sys_["blocker"]["code"]
    # CONNECTED is never a synonym for strategy readiness
    assert sys_["strategy"]["label"] != "CONNECTED"


def test_candidate_rows_are_humanized_but_keep_exact_codes(client):
    c, _ = client
    d = c.get("/api/setups").json()
    assert set(d["primary"]) == {"ACTIVE", "TRADED", "REJECTED"}
    counts = d["counts"]
    assert counts["REJECTED"] == sum(
        counts[k] for k in ("REJECTED_CONTEXT", "REJECTED_EXHAUSTION", "REJECTED_RISK")
    )
    rej = c.get("/api/setups", params={"bucket": "REJECTED"}).json()["items"]
    assert rej and all(i["stage_label"].startswith("Rejected at") for i in rej)
    for i in rej:
        assert i["reason_human"]["code"] == i["primary_reason"]  # exact code still there
        assert i["reason_human"]["label"] != i["primary_reason"]  # humanized label


def test_setup_detail_has_human_flow_summaries_and_overlays(client):
    c, eng = client
    closed = next(m for m in eng.finished if m.state.value == "CLOSED")
    d = c.get(f"/api/setups/{closed.cfg.setup_id}").json()
    assert [f["name"] for f in d["flow_human"]][:3] == ["Spike", "Context", "Exhaustion"]
    assert d["flow_human"][-1]["label"] == "Completed"
    assert d["context_summary"]["result"]["label"] == "Passed"
    assert d["context_summary"]["question"].startswith("Is this a good location")
    assert d["exhaustion_summary"]["result"]["code"] == "PASS"
    kinds = {line["kind"] for line in d["overlays"]["lines"]}
    assert {"E1", "SL", "TP"} <= kinds and "TP2" not in kinds
    assert any(m["kind"] == "EXIT" for m in d["overlays"]["markers"])
    assert d["context_gates"] and d["exhaustion_gates"]  # technical detail kept


def test_confirmed_performance_is_separate_from_counterfactual_and_ambiguous(client):
    c, _ = client
    p = c.get("/api/performance").json()
    st = p["stats"]
    assert st["trades"] == p["confirmed"]["closed"]
    assert st["trades"] == len(p["recent_trades"])
    assert "counterfactual" not in json.dumps(st).lower()
    assert isinstance(p["ambiguous"], list)


def test_api_chart_candles_equal_db_canonical_and_holes_are_explicit(client, engine):
    from datetime import datetime

    c, _ = client
    items = c.get("/api/market/candles", params={"tf": "1m", "limit": 500}).json()["items"]
    with engine.connect() as x:
        from sqlalchemy import text

        db = {
            r[0].isoformat(): r[1:]
            for r in x.execute(
                text(
                    "SELECT open_time AT TIME ZONE 'UTC', open, high, low, close, volume, quality"
                    " FROM candles_1m WHERE symbol = 'BTCUSDT' ORDER BY open_time DESC LIMIT 500"
                )
            )
        }
    real = [i for i in items if not i.get("missing")]
    assert len(real) == len(db)
    for i in real:
        row = db[datetime.fromisoformat(i["open_time"]).replace(tzinfo=None).isoformat()]
        assert [D(i[k]) for k in ("open", "high", "low", "close", "volume")] == list(row[:5])
        assert i["quality"] == row[5]
    times = [datetime.fromisoformat(i["open_time"]) for i in items]
    assert all(b - a == timedelta(minutes=1) for a, b in zip(times, times[1:], strict=False))


def test_integrity_endpoint_exposes_repairs_revisions_and_conflict_audit(client):
    c, _ = client
    ig = c.get("/api/integrity").json()
    assert set(ig) == {
        "repairs",
        "revisions",
        "revision_counts",
        "conflict_audit",
        "quality_24h",
        "latest_canonical_minute",
        "revision_total",
        "latency_1h",
        "continuity",
    }


def test_indicators_endpoint_serves_the_engines_finalized_m5_values(client):
    """Live indicators: one exact snapshot per finalized M5 bar, written by the engine."""
    c, eng = client
    d = c.get("/api/indicators?limit=48").json()
    assert d["available"] and len(d["history_times"]) == 48
    last = eng.m5.last
    assert d["bar_open"] == last.open_time.isoformat()
    by = {x["key"]: x for x in d["items"]}
    assert by["atr14"]["exact"] == str(last.atr14)
    assert by["ema20"]["exact"] == str(last.ema20)
    assert by["adx14"]["exact"] == (str(last.adx.adx) if last.adx.adx is not None else None)
    assert {s["key"]: s["value"] for s in d["states"]}["regime"] == last.regime.value
    assert all(x["change"] in (None, "up", "down", "same") for x in d["items"])
