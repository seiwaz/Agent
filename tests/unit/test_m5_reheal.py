"""A break healed later from validated history is picked up without a new 150-bar warmup.

The engine rebuilds its M5 state from the stored contiguous bars ending at the last bar it
processed - only while it is still warming up and no setup is in progress; the 150-bar
requirement itself is unchanged.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

from sp2l.core.types import Candle
from sp2l.engine.symbol_engine import ShadowSymbolEngine
from sp2l.marketdata.m5_aggregator import M5_STEP, M5Result, M5Status
from sp2l.strategy.risk.engine import CostModel, ExchangeFilters
from tests.conftest import T0

COSTS = CostModel(D("0.0008"), D("0.00095"), D("0.000198"))
FILTERS = ExchangeFilters(D("0.1"), D("0.00001"), None, None, False)


def bar(i: int) -> M5Result:
    px = D(100) + D(i % 7)
    c = Candle(T0 + M5_STEP * i, px, px + 2, px - 2, px + 1, D(10), 10)
    return M5Result(c.open_time, M5Status.OK, c)


def engine(warmup: int = 20) -> ShadowSymbolEngine:
    return ShadowSymbolEngine("BTCUSDT", tick=D("0.1"), costs=COSTS, filters=FILTERS,
                              warmup_bars=warmup, probes=False)  # fmt: skip


def broken(eng: ShadowSymbolEngine, total: int = 40, gap_at: int = 30) -> list[M5Result]:
    """Bars 0..total-1 stored; the engine saw a DATA_GAP at `gap_at` (healed later)."""
    stored = [bar(i) for i in range(total)]
    for r in stored[:gap_at]:
        eng.m5.add(r)
    eng.m5.add(M5Result(T0 + M5_STEP * gap_at, M5Status.DATA_GAP, None))
    for r in stored[gap_at + 1 :]:
        eng.m5.add(r)
    return stored


def test_healed_break_restores_warm_state_from_stored_history():
    eng = engine()
    stored = broken(eng)
    assert not eng.m5.warm and eng.m5.segment is not None and len(eng.m5.segment.bars) == 9
    assert eng.reheal(stored)
    seg = eng.m5.segment
    assert eng.m5.warm and seg is not None
    assert seg.anchor_open_time == T0 and len(seg.bars) == 40
    fresh = engine()
    for r in stored:
        fresh.m5.add(r)
    assert fresh.m5.last is not None and eng.m5.last is not None
    assert eng.m5.last.adx == fresh.m5.last.adx and eng.m5.last.atr14 == fresh.m5.last.atr14


def test_reheal_refuses_when_it_would_not_be_exact_or_safe():
    eng = engine()
    stored = broken(eng)
    assert not eng.reheal(stored[:-1])  # must end at the engine's own last bar
    assert not eng.reheal(stored[-15:])  # shorter than the unchanged warmup requirement
    assert not eng.reheal(stored[-9:])  # nothing longer than what the engine already has
    assert not eng.m5.warm

    eng.active = object()  # type: ignore[assignment]  # a setup in progress: never touched
    assert not eng.reheal(stored)
    eng.active = None

    warm = engine()
    for r in stored:
        warm.m5.add(r)
    assert warm.m5.warm and not warm.reheal(stored)  # nothing to heal


def test_unhealed_hole_keeps_the_warmup():
    eng = engine()
    stored = broken(eng)
    still_missing = stored[:30] + stored[31:]  # the gap bar was never repaired
    tail: list[M5Result] = []
    t = stored[-1].open_time
    by_t = {r.open_time: r for r in still_missing}
    while t in by_t:
        tail.append(by_t[t])
        t -= timedelta(minutes=5)
    assert not eng.reheal(list(reversed(tail)))
    assert not eng.m5.warm


def test_collector_heal_loop_retries_and_never_stops_on_errors():
    import asyncio

    from sp2l.runtime.service import history_heal_loop

    calls: list[int] = []

    def heal() -> dict[str, object]:
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("server offline")  # an outage: logged, retried next pass
        return {"holes": 1, "repaired": [{"from": "a", "to": "b"}], "failed": []}

    async def main() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(history_heal_loop(heal, stop, interval=0.01))
        while len(calls) < 3:
            await asyncio.sleep(0.005)
        stop.set()
        await task

    asyncio.run(main())
    assert len(calls) >= 3
