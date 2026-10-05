"""No look-ahead: cutting off the future never changes what was known in the past."""

from __future__ import annotations

from decimal import Decimal as D

from hypothesis import given, settings
from hypothesis import strategies as st

from sp2l.smc.backtest import build_context, orders
from sp2l.smc.model import Costs
from sp2l.smc.strategy import zone_setups
from tests.smc.test_timeframes_strategy import DENSE, m1_walk

COSTS = Costs(D("0.0002"), D("0.0005"), D("0.0001"))
WALK = m1_walk(4 * 1440, 11)
FULL = build_context(WALK, DENSE)
FULL_ORDERS = orders(FULL, DENSE, COSTS)


def view(s):
    return (
        s.key,
        s.created_at,
        s.accepted,
        s.reasons,
        s.entry,
        s.sl,
        s.tp1,
        s.tp2,
        s.tp3,
    )


@settings(max_examples=12, deadline=None)
@given(cut=st.integers(600, 4 * 1440 - 1))
def test_truncating_future_bars_changes_no_past_zone_setup_or_order(cut):
    ctx = build_context(WALK[:cut], DENSE)
    end = WALK[cut - 1].open_time
    za, zf = ctx[DENSE.zone_tf], FULL[DENSE.zone_tf]
    # zones and their state as of the cut
    k = len(za.bars) - 1
    assert [(z.id, z.top, z.bottom, z.gap) for z in za.zones] == [
        (z.id, z.top, z.bottom, z.gap) for z in zf.zones if z.created_idx <= k
    ]
    assert [z.valid_at(k) for z in za.zones] == [
        z.valid_at(k) for z in zf.zones if z.created_idx <= k
    ]
    # setups known by then
    assert [s.key for s in zone_setups(za, DENSE)] == [
        s.key
        for s in zone_setups(zf, DENSE)
        if s.confirmed_at <= za.bars[-1].open_time + (za.bars[1].open_time - za.bars[0].open_time)
    ]
    # orders whose minute had closed by then are identical
    past = [view(s) for s in orders(ctx, DENSE, COSTS) if s.created_at < end]
    assert past == [view(s) for s in FULL_ORDERS if s.created_at < end]
