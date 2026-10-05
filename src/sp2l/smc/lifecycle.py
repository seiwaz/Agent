"""Signal lifecycle on M1 bars: one stop, one target.

PENDING (limit resting) -> OPEN -> TP / SL, or INVALIDATED (`exit_on_choch`: a zone-TF CHoCH
against the trade closed; market exit at that close), TIME_STOP (neither TP nor SL within
`time_stop_min` of the fill, market exit; 0 = off), TIMEOUT (`max_hold_min`). Without a fill:
EXPIRED (`pending_expiry_min` after creation), MISSED (the target traded before the fill).

All path decisions use final M1 candles. Where one minute could have touched both sides the
conservative reading applies: a stop beats the target in the same minute, and no target is
credited in the minute the entry filled.
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
    TP = "TP"
    SL = "SL"
    INVALIDATED = "INVALIDATED"  # a zone-TF CHoCH against the trade (exit_on_choch)
    TIME_STOP = "TIME_STOP"  # neither TP nor SL within time_stop_min
    EXPIRED = "EXPIRED"  # never filled within the allowed wait
    MISSED = "MISSED"  # target traded before the entry filled
    TIMEOUT = "TIMEOUT"  # closed at market after max_hold
    CANCELLED = "CANCELLED"


ACTIVE = (State.PENDING, State.OPEN)
ACTIVE_SQL = "('PENDING', 'OPEN')"


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
    """The exit: `frac` of the position closed at `price` (taker fee)."""

    kind: str  # TP / SL / INVALIDATED / TIME_STOP / TIMEOUT
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
    tp: Decimal
    created_at: datetime
    risk: Decimal  # risk_unit() at creation: the denominator of R
    state: State = State.PENDING
    filled_at: datetime | None = None
    closed_at: datetime | None = None
    exit_price: Decimal | None = None
    result_r: Decimal | None = None
    last_m1: datetime | None = None  # open time of the last M1 applied
    market: bool = False  # entered at market (OPEN from creation)
    sl0: Decimal | None = None  # the initial stop
    time_stop_min: int = 0  # 0 = no time stop
    parts: list[Part] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.market and self.state is State.PENDING:
            self.state, self.filled_at = State.OPEN, self.created_at
        if self.sl0 is None:
            self.sl0 = self.sl

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


def _slipped(t: Tracked, price: Decimal, costs: Costs) -> Decimal:
    """A market exit of the position at `price` fills one slippage allowance worse."""
    slip = price * costs.slippage
    return price - slip if t.side is Side.LONG else price + slip


def _stop(t: Tracked, at: datetime, costs: Costs) -> str:
    return _close(t, State.SL, _slipped(t, t.sl, costs), at, costs)


def advance(
    t: Tracked, bar: Candle, p: SmcParams, costs: Costs, *, invalidate: bool = False
) -> list[str]:
    """Apply one final M1 bar. `invalidate`: this minute closes a zone-TF CHoCH against the
    open trade (checked after the stop and the target). Returns the events in order."""
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
        if _hit(t, bar, t.tp):
            return [_end(t, State.MISSED, end)]
        if bar.open_time >= t.created_at + timedelta(minutes=p.pending_expiry_min):
            return [_end(t, State.EXPIRED, end)]
        return []
    if _stopped(t, bar):
        return [_stop(t, end, costs)]
    if _hit(t, bar, t.tp):  # a market take-profit: taker fee + the slippage allowance
        return [_close(t, State.TP, _slipped(t, t.tp, costs), end, costs)]
    if invalidate:
        return [_close(t, State.INVALIDATED, bar.close, end, costs)]
    assert t.filled_at is not None
    held = bar.open_time - t.filled_at
    if t.time_stop_min > 0 and held >= timedelta(minutes=t.time_stop_min):
        return [_close(t, State.TIME_STOP, bar.close, end, costs)]
    if held >= timedelta(minutes=p.max_hold_min):
        return [_close(t, State.TIMEOUT, bar.close, end, costs)]
    return []
