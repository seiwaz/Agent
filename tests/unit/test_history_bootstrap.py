"""History bootstrap planning (pure): parsing, forming-bar drop, missing-minute policy, and
stitching to the stored/live series. Decided 2026-09-30: chart holes of 1-2 minutes become
synthetic no-trade minutes; longer holes stay DATA_GAP."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

from sp2l.marketdata.bootstrap import plan_bootstrap
from tests.conftest import T0

MIN = timedelta(minutes=1)


def bar(i: int, close: str = "100", volume: str = "2") -> dict[str, str | int]:
    t = T0 + i * MIN
    c = D(close)
    return {"time": int(t.timestamp()), "open": str(c), "high": str(c + 1), "low": str(c - 1),
            "close": str(c), "volume": volume}  # fmt: skip


def at(i: int, sec: int = 0):  # type: ignore[no-untyped-def]
    return T0 + i * MIN + timedelta(seconds=sec)


def test_chart_minutes_are_planned_and_the_forming_bar_is_dropped():
    raw = [bar(i, str(100 + i)) for i in range(10)]
    # requested at minute 9 + 6 s: bar 8 is final (60 s + 5 s settle), bar 9 still forming
    plan = plan_bootstrap(raw, T0, at(20), {}, at(9, 6))
    assert plan is not None
    assert sorted(plan.candles) == [at(i) for i in range(9)]
    assert plan.dropped_forming == 1
    assert plan.candles[at(4)].close == D(104) and plan.candles[at(4)].trade_count is None


def test_one_and_two_minute_holes_become_synthetic_no_trade_minutes():
    raw = [bar(0, "100"), bar(2, "101"), bar(3, "102"), bar(6, "103"), bar(7, "104")]
    plan = plan_bootstrap(raw, T0, at(8), {}, at(30))
    assert plan is not None
    assert sorted(plan.synthetic) == [at(1), at(4), at(5)]
    s = plan.synthetic[at(4)]
    assert s.synthetic and s.volume == 0 and s.trade_count == 0
    assert s.open == s.high == s.low == s.close == D(102)  # previous close
    assert plan.gaps == []


def test_longer_holes_stay_data_gaps():
    raw = [bar(0), bar(1), bar(5), bar(6)]  # minutes 2, 3, 4 missing: 3 > 2
    plan = plan_bootstrap(raw, T0, at(7), {}, at(30))
    assert plan is not None
    assert plan.gaps == [(at(2), at(4))] and plan.synthetic == {}
    assert at(3) not in plan.minutes


def test_stored_minutes_are_never_touched_and_stitching_fills_the_partial_start_minute():
    raw = [bar(i, str(100 + i)) for i in range(12)]
    # the live collector stored minutes 9..11; minute 8 was its partial COLLECTOR_START minute
    stored = {at(9): D(109), at(10): D(110), at(11): D(111)}
    plan = plan_bootstrap(raw, T0, at(11), stored, at(30))
    assert plan is not None
    assert max(plan.minutes) == at(8)  # joins the live series with no hole
    assert not set(plan.minutes) & set(stored)


def test_hole_next_to_a_stored_minute_uses_the_stored_close():
    raw = [bar(0, "100"), bar(1, "101")]  # chart omits minute 2; minute 3 is stored (live)
    plan = plan_bootstrap(raw, T0, at(4), {at(3): D(105)}, at(30))
    assert plan is not None
    assert plan.synthetic[at(2)].close == D(101)


def test_nothing_before_the_first_chart_bar_and_malformed_responses_are_rejected():
    plan = plan_bootstrap([bar(5), bar(6)], T0, at(8), {}, at(30))
    assert plan is not None and min(plan.minutes) == at(5) and plan.gaps == [(at(7), at(7))]
    assert plan_bootstrap([{"time": "x"}], T0, at(8), {}, at(30)) is None


def _run_task(outcomes, ready_after=2):  # type: ignore[no-untyped-def]
    import asyncio

    from sp2l.runtime.service import history_bootstrap_task

    polls, calls = [], []

    def ready() -> bool:
        polls.append(1)
        return len(polls) > ready_after

    def boot():  # type: ignore[no-untyped-def]
        calls.append(1)
        return outcomes[min(len(calls), len(outcomes)) - 1]

    async def main() -> None:
        await asyncio.wait_for(
            history_bootstrap_task(boot, ready, asyncio.Event(), poll=0.001, retry=0.001), 5
        )

    asyncio.run(main())
    return len(polls), len(calls)


def test_task_waits_for_live_minutes_then_runs_once():
    polls, calls = _run_task([{"minutes_from_chart": 10}])
    assert polls == 3 and calls == 1


def test_task_retries_fetch_failures_but_stops_on_validation_failure():
    _, calls = _run_task([{"failure": "FETCH_FAILED: x"}, {"failure": "FETCH_FAILED: y"}, {}])
    assert calls == 3
    _, calls = _run_task([{"failure": "HISTORY_VALIDATION_FAILED"}, {}])
    assert calls == 1  # fail closed: the normal warmup continues
