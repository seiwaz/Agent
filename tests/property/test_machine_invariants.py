"""Randomized interleavings: the E1 guard and single-order invariants always hold."""

from __future__ import annotations

from decimal import Decimal as D

from hypothesis import given, settings
from hypothesis import strategies as st

from sp2l.engine.model import OrderStatus
from tests.race.fakes import FakeGates, at, gate, m1, machine

actions = st.lists(
    st.one_of(
        st.tuples(st.just("extend")),
        st.tuples(st.just("flat")),
        st.tuples(st.just("touch")),
        st.tuples(st.just("fill"), st.sampled_from(["0.3", "1"])),
        st.tuples(st.just("cancel_mode"), st.sampled_from(["ok", "pending", "fill", "position"])),
    ),
    max_size=25,
)


@settings(max_examples=300, deadline=None)
@given(actions, st.lists(st.booleans(), max_size=40))
def test_guard_and_single_order_invariants(script, gate_outcomes):
    outcomes = list(gate_outcomes)
    gates = FakeGates(decide=lambda i, lv, r: gate(ok=outcomes[i] if i < len(outcomes) else True))
    m, port, _ = machine(gates=gates)
    m.start(at(3), last_trade=D("102.5"))
    low = D("101")
    minute = 3
    first_fill_submits: int | None = None
    for act in script:
        if m.terminal:
            break
        kind = act[0]
        if kind == "extend":
            low += 1
            m.on_m1_close(
                m1(minute, str(low + D("0.5")), str(low + 2), str(low), str(low + 1)),
                at(minute + 1),
                low + 1,
            )
            minute += 1
        elif kind == "flat":
            m.on_m1_close(
                m1(minute, str(low), str(low + 1), str(low - 5), str(low)), at(minute + 1), low
            )
            minute += 1
        elif kind == "touch" and m.e1_id:
            m.on_trade(m.levels.e1, at(minute, 30))
        elif kind == "fill" and m.e1_id:
            o = port.orders[m.e1_id]
            if o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                q = min(D(act[1]), o.qty - o.executed)
                m.on_fill(port.fill(m.e1_id, q))
        elif kind == "cancel_mode":
            mode = act[1]
            port.cancel_script = [
                ("fill", D("0.2"))
                if mode == "fill"
                else ("position", D("0.5"))
                if mode == "position"
                else mode
            ]
        # --- invariants -------------------------------------------------------------
        assert len(port.active_e1()) <= 1, "more than one live E1"
        e1_executed = sum(o.executed for c, o in port.orders.items() if "-E1-" in c)
        if (e1_executed > 0 or port.pos != 0) and first_fill_submits is None:
            first_fill_submits = len([s for s in port.submits if "-E1-" in s[0]])
        if first_fill_submits is not None:
            assert len([s for s in port.submits if "-E1-" in s[0]]) == first_fill_submits, (
                "E1 re-submitted after a fill or setup position"
            )
        e2 = [s for s in port.submits if "-E2-" in s[0]]
        if e2:
            e1 = port.orders[m.e1_id] if m.e1_id else None
            assert e1 is not None and e1.executed >= e1.qty, "E2 before full E1"
            assert port.protections, "E2 before verified protection"
            assert e2[0][2] == e1.qty, "E2 quantity differs from E1"
