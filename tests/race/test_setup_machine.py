"""State machine and cancel/replace race tests (E1-03..10, PB, FW, E2, B14, B15, B21, B30)."""

from __future__ import annotations

from decimal import Decimal as D

from sp2l.engine.model import SetupState as S
from tests.race.fakes import FakeGates, at, gate, m1, machine


def armed(**kw):
    m, port, gates = machine(**kw)
    m.start(at(3), last_trade=D("102.5"))
    assert m.state is S.E1_PENDING and port.submits == [("s1-E1-0", D("101"), D("1"))]
    return m, port, gates


def test_b15_unsafe_start_sends_nothing_then_arms_on_safe_extension():
    m, port, _ = machine()
    m.start(at(3), last_trade=D("101"))  # equality is not safe
    assert m.state is S.E1_PREPARING and port.submits == []
    m.on_m1_close(m1(3, "102.5", "104", "102", "103.5"), at(4), D("103.5"))
    assert m.state is S.E1_PENDING and port.submits == [("s1-E1-0", D("102"), D("1"))]


def test_b30_run_ends_before_arming_is_expired_unarmed():
    m, port, _ = machine()
    m.start(at(3), last_trade=D("100"))
    m.on_m1_close(m1(3, "101", "101.5", "100.5", "100.8"), at(4), D("100.8"))  # lower low
    assert m.state is S.EXPIRED_UNARMED and m.primary_reason == "E1_NEVER_ARMED"


def test_reprice_on_extension_cancels_then_replaces_one_order():
    m, port, _ = armed()
    m.on_m1_close(m1(3, "102.5", "104", "102", "103.5"), at(4), D("103.5"))
    assert m.state is S.E1_PENDING
    assert [s[:2] for s in port.submits] == [("s1-E1-0", D("101")), ("s1-E1-1", D("102"))]
    assert port.active_e1() == ["s1-E1-1"]


def test_fill_discovered_during_cancel_aborts_replacement_and_protects():
    m, port, _ = armed()
    port.cancel_script = [("fill", D("0.4"))]
    m.on_m1_close(m1(3, "102.5", "104", "102", "103.5"), at(4), D("103.5"))
    assert m.state is S.E1_PARTIAL
    assert len(port.submits) == 1  # no replacement
    assert port.protections == [(D("98.9"), D("103.1"))]
    assert m.levels.e1 == D("101")  # price frozen at the original E1


def test_setup_position_discovered_during_cancel_aborts_replacement():
    m, port, _ = armed()
    port.cancel_script = [("position", D("1"))]
    m.on_m1_close(m1(3, "102.5", "104", "102", "103.5"), at(4), D("103.5"))
    assert m.state in (S.E1_FILLED, S.E2_PENDING, S.POSITION_ACTIVE)
    assert [s[0] for s in port.submits if "-E1-" in s[0]] == ["s1-E1-0"]


def test_cancel_in_flight_waits_then_replaces_or_holds():
    m, port, _ = armed()
    port.cancel_script = ["pending"]
    m.on_m1_close(m1(3, "102.5", "104", "102", "103.5"), at(4), D("103.5"))
    assert m.state is S.E1_REPRICING and len(port.submits) == 1
    port.orders["s1-E1-0"].status = port.orders["s1-E1-0"].status.CANCELED
    m.on_m1_close(m1(4, "103.5", "105", "103", "104"), at(5), D("104"))
    assert m.state is S.E1_PENDING and port.submits[-1][0] == "s1-E1-1"

    m2, port2, _ = armed()
    port2.cancel_script = ["pending"]
    m2.on_m1_close(m1(3, "102.5", "104", "102", "103.5"), at(4), D("103.5"))
    for i in range(4, 8):
        m2.on_m1_close(m1(i, "103.5", "105", "103", "104"), at(i + 1), D("104"))
    assert m2.state is S.ERROR_HOLD and m2.primary_reason == "CANCEL_UNCONFIRMED"


def test_gate_failure_with_zero_fill_cancels_and_rejects_after_arm():
    gates = FakeGates(decide=lambda i, lv, r: gate(ok=i == 0))
    m, port, _ = armed(gates=gates)
    m.on_m1_close(m1(3, "102.5", "104", "102", "103.5"), at(4), D("103.5"))
    assert m.state is S.REJECTED_CONTEXT
    assert m.reasons[0] == "CONTEXT_INVALIDATED_BEFORE_FILL"
    assert port.active_e1() == []


def test_pullback_freezes_repricing_and_b14_recheck_cancels():
    gates = FakeGates(decide=lambda i, lv, r: gate(ok=i < 2, stage="exhaustion"))
    m, port, _ = armed(gates=gates)
    m.on_trade(D("101"), at(3, 10))  # touch == E1 -> PullbackStart
    assert m.state is S.PULLBACK_DETECTED
    m.on_m1_close(m1(3, "102", "104", "101", "103.5"), at(4), D("103.5"))  # passes (call 1)
    assert m.state is S.PULLBACK_DETECTED and len(port.submits) == 1  # no reprice
    assert gates.calls[-1][1] is False  # recheck without risk revision
    m.on_m1_close(m1(4, "103.5", "105", "103", "104"), at(5), D("104"))  # fails (call 2)
    assert m.state is S.REJECTED_EXHAUSTION
    assert m.reasons[0] == "EXHAUSTION_INVALIDATED_BEFORE_FILL"


def test_fill_window_expiry_zero_fill():
    m, port, _ = armed()
    m.on_trade(D("101"), at(3, 30))
    for i in range(3, 6):
        m.on_m1_close(m1(i, "102", "103", "101.2", "102"), at(i + 1), D("102"))
        assert m.state is S.PULLBACK_DETECTED
    m.on_m1_close(m1(6, "102", "103", "101.2", "102"), at(7), D("102"))  # close of #4
    assert m.state is S.EXPIRED_NO_FILL and port.active_e1() == []


def test_partial_fill_keeps_remainder_at_original_price_and_disables_e2():
    m, port, _ = armed()
    m.on_trade(D("101"), at(3, 5))
    m.on_fill(port.fill("s1-E1-0", D("0.3")))
    assert m.state is S.E1_PARTIAL and port.protections
    for i in range(3, 6):  # extension candles never move the remainder
        m.on_m1_close(m1(i, "103", "106", "102.5", "105"), at(i + 1), D("105"))
    assert port.orders["s1-E1-0"].price == D("101") and len(port.submits) == 1
    m.on_m1_close(m1(6, "105", "106", "104", "105"), at(7), D("105"))  # close of #4
    assert m.state is S.POSITION_ACTIVE and m.e2_enabled is False
    assert port.orders["s1-E1-0"].status.name == "CANCELED"
    assert all("-E2-" not in s[0] for s in port.submits)


def test_full_fill_protects_then_submits_equal_size_e2_at_rounded_midpoint():
    m, port, _ = armed()
    m.on_fill(port.fill("s1-E1-0", D("1")))
    assert port.protections == [(D("98.9"), D("103.1"))]
    assert m.state is S.E2_PENDING
    assert port.submits[-1] == ("s1-E2-0", D("99.9"), D("1"))  # (101+98.9)/2=99.95 -> 99.9


def test_e2_not_submitted_when_semantics_unvalidated():
    m, port, _ = armed(gates=FakeGates(e2_ok=False))
    m.on_fill(port.fill("s1-E1-0", D("1")))
    assert m.state is S.POSITION_ACTIVE and all("-E2-" not in s[0] for s in port.submits)


def test_b21_protection_failure_emergency_closes_and_holds():
    m, port, _ = armed()
    port.protection_script = [False, False, False]
    m.on_fill(port.fill("s1-E1-0", D("1")))
    assert m.state is S.ERROR_HOLD
    assert m.reasons == ["PROTECTION_UNVERIFIED", "EMERGENCY_CLOSED"]
    assert port.emergency_closes == 1 and port.pos == 0
    assert all("-E2-" not in s[0] for s in port.submits)


def test_b21_retry_within_limit_succeeds():
    m, port, _ = armed()
    port.protection_script = [False, True]
    m.on_fill(port.fill("s1-E1-0", D("1")))
    assert m.state is S.E2_PENDING and port.emergency_closes == 0


def test_reconcile_at_close_finds_unreported_fill():
    m, port, _ = armed()
    port.fill("s1-E1-0", D("1"))  # exchange filled; no fill event delivered yet
    m.on_m1_close(m1(3, "102.5", "104", "102", "103.5"), at(4), D("103.5"))
    assert m.state is S.E2_PENDING and len([s for s in port.submits if "-E1-" in s[0]]) == 1


def test_exit_cancels_open_entries_and_closes():
    from sp2l.engine.model import ExitEvent, ExitKind

    m, port, _ = armed()
    m.on_fill(port.fill("s1-E1-0", D("1")))
    assert m.state is S.E2_PENDING
    m.on_exit(ExitEvent(ExitKind.TP, at(5), D("103.1"), D("1"), D(0)))
    assert m.state is S.CLOSED and port.orders["s1-E2-0"].status.name == "CANCELED"


def test_b27_breakout_level_advances_during_zero_fill_extension_then_locks_at_pullback():
    from sp2l.strategy.context.levels import EligibleLevel
    from tests.conftest import T0

    gates = FakeGates()
    gates.eligible = (EligibleLevel(D("104"), T0), EligibleLevel(D("106"), T0))
    m, port, _ = armed(gates=gates)
    assert m.frozen is None  # spike closes <= 102.5 cross nothing yet
    m.on_m1_close(m1(3, "103", "105", "102", "104.5"), at(4), D("104.5"))
    assert m.frozen is not None and m.frozen.price == D("104")
    gates.eligible = (EligibleLevel(D("999"), T0),)  # the eligible set is frozen at creation
    m.on_m1_close(m1(4, "104.5", "107", "103", "106.5"), at(5), D("106.5"))
    assert m.frozen.price == D("106")  # advanced to the farther crossed level
    m.on_trade(m.levels.e1, at(5, 10))  # PullbackStart locks it
    assert m.breakout_locked


def test_b27_breakout_level_locks_at_first_fill():
    from sp2l.strategy.context.levels import EligibleLevel
    from tests.conftest import T0

    gates = FakeGates()
    gates.eligible = (EligibleLevel(D("104"), T0),)
    m, port, _ = armed(gates=gates)
    m.on_fill(port.fill("s1-E1-0", D("1")))
    assert m.breakout_locked and m.frozen is None


def test_b39_gap_leaves_unexposed_setup_to_normal_rules_and_marks_exposed_ambiguous():
    from decimal import Decimal as D2

    from sp2l.engine.symbol_engine import ShadowSymbolEngine
    from sp2l.strategy.risk.engine import CostModel, ExchangeFilters

    eng = ShadowSymbolEngine(
        "BTCUSDT",
        tick=D2("0.1"),
        costs=CostModel(D2(0), D2(0), D2(0)),
        filters=ExchangeFilters(D2("0.1"), D2("0.001"), None, None, False),
    )
    m, port, _ = machine()
    m.start(at(3), last_trade=D("101"))  # unsafe: E1_PREPARING, nothing resting
    eng.active = m
    eng.on_gap(at(3, 30), None, at(4), "DISCONNECTED")
    assert m.state is S.E1_PREPARING and eng.active is m  # normal run-end rules will apply

    m2, port2, _ = armed()
    eng.active = m2
    eng.on_gap(at(3, 30), None, at(4), "DISCONNECTED")
    # V5.8: the GAP alone decides nothing (an exact repair would replay causally) ...
    assert m2.state is S.E1_PENDING and eng.active is m2
    # ... the gap's minute arriving as DATA_GAP (every tier failed) does
    from sp2l.marketdata.m1_builder import M1Result, M1Status

    eng.on_m1(M1Result(at(3), M1Status.DATA_GAP, None))
    assert m2.state is S.AMBIGUOUS_DATA_GAP and m2.primary_reason == "DATA_GAP_WHILE_ACTIVE"
    assert eng.active is None and all(e["kind"] != "EXIT" for e in m2.events)
