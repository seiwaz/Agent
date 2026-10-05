"""Signal lifecycle on M1 bars with the SMC-2.0 take-profit ladder.

PENDING (limit resting) -> OPEN -> TP1 (tp1 share out, stop to break-even net of fees)
-> TP2 (tp2 share out, the stop trails behind execution-TF swings) -> TP (TP3, the rest out).
Without a TP2 level its share stays in the runner, which trails from TP1 on.
Exits of what is still open: SL (before TP1: exactly -1R), BE (after TP1, stop still at
break-even), TRAIL (the trailing stop),
TIME_STOP (neither TP1 nor SL within `time_stop_min` of the fill), TIMEOUT (`max_hold_min`).
Without a fill: EXPIRED (`pending_expiry_min` after creation), MISSED (the first target traded
before the fill). A signal without TP1/TP2 (SMC-1.0) runs its single TP.

All path decisions use final M1 candles. Where one minute could have touched both sides the
conservative reading applies: a stop beats any target in the same minute, no target is
credited in the minute the entry filled, and a moved stop (break-even, trailing) only counts
from the next minute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sp2l.core.types import Candle, Side
from sp2l.smc.model import Costs, SmcParams

MINUTE = timedelta(minutes=1)


class State(StrEnum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    TP1 = "TP1"  # first share out, stop at break-even
    TP2 = "TP2"  # second share out, trailing
    TP = "TP"  # final target
    SL = "SL"  # full stop before TP1
    BE = "BE"  # rest stopped at break-even after TP1
    TRAIL = "TRAIL"  # rest stopped by the trailing stop after TP2
    TIME_STOP = "TIME_STOP"  # neither TP1 nor SL within time_stop_min
    EXPIRED = "EXPIRED"  # never filled within the allowed wait
    MISSED = "MISSED"  # first target traded before the entry filled
    TIMEOUT = "TIMEOUT"  # closed at market after max_hold
    CANCELLED = "CANCELLED"


ACTIVE = (State.PENDING, State.OPEN, State.TP1, State.TP2)
ACTIVE_SQL = "('PENDING', 'OPEN', 'TP1', 'TP2')"


def risk_unit(
    side: Side, entry: Decimal, sl: Decimal, costs: Costs, *, market: bool = False
) -> Decimal:
    """Worst planned loss per unit: stop distance + entry fee + slippage + the taker fee on
    the slipped stop fill. A full stop-out is exactly -1R."""
    slip = sl * costs.slippage
    exit_px = sl - slip if side is Side.LONG else sl + slip
    return (
        abs(entry - sl)
        + entry * (costs.taker_fee if market else costs.maker_fee)
        + slip
        + exit_px * costs.taker_fee
    )


@dataclass(frozen=True, slots=True)
class Part:
    """One exit: `frac` of the position closed at `price` (taker fee)."""

    kind: str  # TP1 / TP2 / TP / SL / BE / TRAIL / TIME_STOP / TIMEOUT
    price: Decimal
    frac: Decimal
    at: datetime
    r: Decimal  # this part's contribution to the result in R


@dataclass(slots=True)
class Tracked:
    key: str
    side: Side
    entry: Decimal
    sl: Decimal
    tp: Decimal | None  # final target (TP3); None: the rest runs on the trailing stop
    created_at: datetime
    risk: Decimal  # risk_unit() at creation: the denominator of R
    state: State = State.PENDING
    filled_at: datetime | None = None
    closed_at: datetime | None = None
    exit_price: Decimal | None = None
    result_r: Decimal | None = None
    last_m1: datetime | None = None  # open time of the last M1 applied
    market: bool = False  # entered at market (OPEN from creation)
    sl0: Decimal | None = None  # the initial stop (sl moves to break-even / trails)
    tp1: Decimal | None = None
    tp2: Decimal | None = None
    frac1: Decimal = Decimal(0)  # share closed at TP1 (actual, after quantity rounding)
    frac2: Decimal = Decimal(0)
    time_stop_min: int = 0  # 0 = no time stop
    parts: list[Part] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.market and self.state is State.PENDING:
            self.state, self.filled_at = State.OPEN, self.created_at
        if self.sl0 is None:
            self.sl0 = self.sl

    @property
    def ladder(self) -> bool:
        return self.tp1 is not None

    @property
    def runner(self) -> bool:
        """Only the runner is left: it trails."""
        return self.state is State.TP2 or (self.state is State.TP1 and self.tp2 is None)

    @property
    def at_breakeven(self) -> bool:
        return self.sl0 is not None and self.sl != self.sl0

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    @property
    def open_frac(self) -> Decimal:
        return 1 - sum((x.frac for x in self.parts), start=Decimal(0))

    @property
    def realized_r(self) -> Decimal:
        return sum((x.r for x in self.parts), start=Decimal(0))

    def fee_in(self, costs: Costs) -> Decimal:
        return costs.taker_fee if self.market else costs.maker_fee

    def net_per_unit(self, price: Decimal, costs: Costs) -> Decimal:
        sgn = 1 if self.side is Side.LONG else -1
        fees = self.entry * self.fee_in(costs) + price * costs.taker_fee
        return (price - self.entry) * sgn - fees


def _part(t: Tracked, kind: str, price: Decimal, frac: Decimal, at: datetime, c: Costs) -> None:
    t.parts.append(Part(kind, price, frac, at, frac * t.net_per_unit(price, c) / t.risk))


def _close(t: Tracked, state: State, price: Decimal, at: datetime, costs: Costs) -> str:
    """Close what is still open."""
    _part(t, state.value, price, t.open_frac, at, costs)
    t.state, t.exit_price, t.closed_at = state, price, at
    t.result_r = t.realized_r
    return state.value


def _end(t: Tracked, state: State, at: datetime) -> str:
    t.state, t.closed_at = state, at
    return state.value


def _hit(t: Tracked, bar: Candle, px: Decimal) -> bool:
    return bar.high >= px if t.side is Side.LONG else bar.low <= px


def _stopped(t: Tracked, bar: Candle) -> bool:
    return bar.low <= t.sl if t.side is Side.LONG else bar.high >= t.sl


def _stop(t: Tracked, at: datetime, costs: Costs) -> str:
    slip = t.sl * costs.slippage
    price = t.sl - slip if t.side is Side.LONG else t.sl + slip
    if t.state is State.OPEN:
        state = State.SL
    elif t.state is State.TP1 and t.sl == breakeven(t, costs):
        state = State.BE
    else:
        state = State.TRAIL
    return _close(t, state, price, at, costs)


def breakeven(t: Tracked, costs: Costs) -> Decimal:
    """The stop that loses nothing after the entry and exit fees."""
    cost = t.entry * (t.fee_in(costs) + costs.taker_fee)
    return t.entry + cost if t.side is Side.LONG else t.entry - cost


def advance(
    t: Tracked, bar: Candle, p: SmcParams, costs: Costs, trail: Decimal | None = None
) -> list[str]:
    """Apply one final M1 bar; `trail` = the trailing stop known at the bar's open. Returns
    the events in the order they happened (empty when nothing changed)."""
    if not t.active or (t.last_m1 is not None and bar.open_time <= t.last_m1):
        return []
    t.last_m1 = bar.open_time
    end = bar.open_time + MINUTE
    long = t.side is Side.LONG
    if t.state is State.PENDING:
        if bar.low <= t.entry if long else bar.high >= t.entry:
            t.state, t.filled_at = State.OPEN, bar.open_time
            if _stopped(t, bar):
                return ["FILLED", _stop(t, end, costs)]
            return ["FILLED"]
        first = t.tp1 if t.ladder else t.tp
        if first is not None and _hit(t, bar, first):
            return [_end(t, State.MISSED, end)]
        if bar.open_time >= t.created_at + timedelta(minutes=p.pending_expiry_min):
            return [_end(t, State.EXPIRED, end)]
        return []
    if _stopped(t, bar):
        return [_stop(t, end, costs)]
    events: list[str] = []
    if t.ladder:
        assert t.tp1 is not None
        if t.state is State.OPEN and _hit(t, bar, t.tp1):
            _part(t, "TP1", t.tp1, t.frac1, end, costs)
            t.state, t.sl = State.TP1, breakeven(t, costs)
            events.append("TP1")
        if t.state is State.TP1 and t.tp2 is not None and _hit(t, bar, t.tp2):
            _part(t, "TP2", t.tp2, t.frac2, end, costs)
            t.state = State.TP2
            events.append("TP2")
        if t.runner and t.tp is not None and _hit(t, bar, t.tp):
            return [*events, _close(t, State.TP, t.tp, end, costs)]
    elif t.tp is not None and _hit(t, bar, t.tp):
        return [_close(t, State.TP, t.tp, end, costs)]
    assert t.filled_at is not None
    held = bar.open_time - t.filled_at
    if t.state is State.OPEN and t.time_stop_min > 0 and held >= timedelta(minutes=t.time_stop_min):
        return [*events, _close(t, State.TIME_STOP, bar.close, end, costs)]
    if held >= timedelta(minutes=p.max_hold_min):
        return [*events, _close(t, State.TIMEOUT, bar.close, end, costs)]
    if t.runner and trail is not None and (trail > t.sl if long else trail < t.sl):
        t.sl = trail  # only ever tightened; counts from the next minute
        events.append("STOP_MOVED")
    return events
