"""Signal lifecycle on M1 bars: PENDING (limit waiting) -> OPEN -> TP / SL (+ exits without fill).

Break-even (`be_at_r` > 0): once a bar's favourable extreme reaches entry +- be_at_r x the
initial stop distance, the stop moves to the entry price shifted by the entry and exit fees,
so a later stop-out costs about nothing. It moves at the END of that bar and only counts from
the next bar (the order of the extremes inside one minute is unknown); never in the fill bar.

All path decisions use final M1 candles. Where one minute could have touched both sides the
conservative reading applies: a stop beats a target, and a target is not credited in the very
minute the entry filled (the order inside the minute is unknown).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sp2l.core.types import Candle, Side
from sp2l.smc.model import Costs, SmcParams

MINUTE = timedelta(minutes=1)


class State(StrEnum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    TP = "TP"
    SL = "SL"
    EXPIRED = "EXPIRED"  # never filled within the allowed wait
    MISSED = "MISSED"  # target traded before the entry filled
    TIMEOUT = "TIMEOUT"  # filled, closed at market after max_hold
    CANCELLED = "CANCELLED"


ACTIVE = (State.PENDING, State.OPEN)


def risk_unit(
    side: Side, entry: Decimal, sl: Decimal, costs: Costs, *, market: bool = False
) -> Decimal:
    """Worst planned loss per unit: stop distance + entry fee + stop exit fee + slippage.
    A market entry pays the taker fee, a resting limit entry the maker fee."""
    return (
        abs(entry - sl)
        + entry * (costs.taker_fee if market else costs.maker_fee)
        + sl * costs.taker_fee
        + sl * costs.slippage
    )


@dataclass(slots=True)
class Tracked:
    key: str
    side: Side
    entry: Decimal
    sl: Decimal
    tp: Decimal
    created_at: datetime
    risk: Decimal  # risk_unit() at creation: the denominator of R
    state: State = State.PENDING
    filled_at: datetime | None = None
    closed_at: datetime | None = None
    exit_price: Decimal | None = None
    result_r: Decimal | None = None
    last_m1: datetime | None = None  # open time of the last M1 applied
    market: bool = False  # entered at the trigger close (OPEN from creation)
    sl0: Decimal | None = None  # the initial stop (sl moves to break-even)

    def __post_init__(self) -> None:
        if self.market and self.state is State.PENDING:
            self.state, self.filled_at = State.OPEN, self.created_at
        if self.sl0 is None:
            self.sl0 = self.sl

    @property
    def at_breakeven(self) -> bool:
        return self.sl0 is not None and self.sl != self.sl0

    @property
    def active(self) -> bool:
        return self.state in ACTIVE


def _close(t: Tracked, state: State, price: Decimal, at: datetime, costs: Costs) -> str:
    sgn = 1 if t.side is Side.LONG else -1
    gross = (price - t.entry) * sgn
    fee_in = costs.taker_fee if t.market else costs.maker_fee
    net = gross - t.entry * fee_in - price * costs.taker_fee
    t.state, t.exit_price, t.closed_at = state, price, at
    t.result_r = net / t.risk
    return state.value


def _end(t: Tracked, state: State, at: datetime) -> str:
    t.state, t.closed_at = state, at
    return state.value


def advance(t: Tracked, bar: Candle, p: SmcParams, costs: Costs) -> str | None:
    """Apply one final M1 bar. Returns the event name when the state changed."""
    if not t.active or (t.last_m1 is not None and bar.open_time <= t.last_m1):
        return None
    t.last_m1 = bar.open_time
    end = bar.open_time + MINUTE
    long = t.side is Side.LONG
    event: str | None = None
    if t.state is State.PENDING:
        filled = bar.low <= t.entry if long else bar.high >= t.entry
        if filled:
            t.state, t.filled_at = State.OPEN, bar.open_time
            event = "FILLED"
            stop = bar.low <= t.sl if long else bar.high >= t.sl
            if stop:
                return _stop(t, end, costs)
            return event
        reached = bar.high >= t.tp if long else bar.low <= t.tp
        if reached:
            return _end(t, State.MISSED, end)
        if bar.open_time >= t.created_at + timedelta(minutes=p.pending_expiry_min):
            return _end(t, State.EXPIRED, end)
        return None
    stop = bar.low <= t.sl if long else bar.high >= t.sl
    if stop:
        return _stop(t, end, costs)
    hit = bar.high >= t.tp if long else bar.low <= t.tp
    if hit:
        return _close(t, State.TP, t.tp, end, costs)
    assert t.filled_at is not None
    if bar.open_time - t.filled_at >= timedelta(minutes=p.max_hold_min):
        return _close(t, State.TIMEOUT, bar.close, end, costs)
    if p.be_at_r > 0 and not t.at_breakeven and t.sl0 is not None:
        reach = abs(t.entry - t.sl0) * p.be_at_r
        fav = (bar.high - t.entry) if long else (t.entry - bar.low)
        if fav >= reach:
            fee_in = costs.taker_fee if t.market else costs.maker_fee
            cost = t.entry * (fee_in + costs.taker_fee)
            t.sl = t.entry + cost if long else t.entry - cost
            return "BREAKEVEN"
    return event


def _stop(t: Tracked, at: datetime, costs: Costs) -> str:
    slip = t.sl * costs.slippage
    price = t.sl - slip if t.side is Side.LONG else t.sl + slip
    return _close(t, State.SL, price, at, costs)
