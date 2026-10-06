"""Smart Money Concepts value types and parameters.

Direction uses `Side`: LONG = bullish, SHORT = bearish. All prices are Decimal; every
index refers to the position in the bar list that was analysed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sp2l.core.types import Candle, Side

VERSION = "SMC-2.2"


class ZoneStatus(StrEnum):
    ACTIVE = "ACTIVE"  # never retested
    TESTED = "TESTED"  # price traded into it, never closed through it
    MITIGATED = "MITIGATED"  # a bar closed through the far side


@dataclass(frozen=True, slots=True)
class Swing:
    kind: str  # HIGH / LOW
    idx: int
    time: datetime  # open time of the pivot bar
    price: Decimal
    confirmed_idx: int  # the bar on whose close the pivot became known


@dataclass(frozen=True, slots=True)
class StructureEvent:
    id: str
    kind: str  # BOS (continuation) / CHOCH (reversal)
    direction: Side
    level: Decimal  # the broken swing price
    level_idx: int
    level_time: datetime
    break_idx: int
    break_time: datetime  # open time of the bar that closed beyond the level


@dataclass(frozen=True, slots=True)
class Sweep:
    """A bar whose wick ran beyond resting liquidity and closed back inside.

    direction LONG = sell-side liquidity (swing lows) taken: the bullish sweep; SHORT mirrored."""

    idx: int
    direction: Side
    level: Decimal  # the deepest swing taken (an equal-lows pool counts only when all taken)
    wick: Decimal  # the sweep bar's low (LONG) / high (SHORT)
    time: datetime  # open time of the sweep bar


@dataclass(slots=True)
class Zone:
    """Order block or fair value gap."""

    id: str
    kind: str  # OB / FVG
    direction: Side
    top: Decimal
    bottom: Decimal
    idx: int  # OB: the candle; FVG: the middle candle
    time: datetime
    created_idx: int  # bar on whose close the zone became known (OB: the break; FVG: 3rd bar)
    event_id: str | None = None  # OB: the structure event that created it
    expires_idx: int = 1 << 62  # zones older than the timeframe's lookback are dropped
    status: ZoneStatus = ZoneStatus.ACTIVE
    tested_idx: int | None = None
    mitigated_idx: int | None = None
    gap: tuple[Decimal, Decimal] | None = None  # OB: (bottom, top) of the FVG right after it
    gap_idx: int | None = None  # OB: the FVG's middle bar

    @property
    def mid(self) -> Decimal:
        return (self.top + self.bottom) / 2

    def valid_at(self, k: int) -> bool:
        """Known, not mitigated and not expired as of the close of bar k."""
        return (
            self.created_idx <= k
            and k <= self.expires_idx
            and (self.mitigated_idx is None or self.mitigated_idx > k)
        )

    def overlaps(self, other: Zone) -> bool:
        return self.bottom <= other.top and other.bottom <= self.top


@dataclass(slots=True)
class Analysis:
    tf: str
    bars: list[Candle]
    atr: list[Decimal | None]
    swings: list[Swing] = field(default_factory=list)
    events: list[StructureEvent] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    trend: list[int] = field(default_factory=list)  # +1 / -1 / 0 after each bar closed
    event_ob: dict[str, Zone] = field(default_factory=dict)
    sweeps: list[Sweep] = field(default_factory=list)

    @property
    def order_blocks(self) -> list[Zone]:
        return [z for z in self.zones if z.kind == "OB"]

    @property
    def fvgs(self) -> list[Zone]:
        return [z for z in self.zones if z.kind == "FVG"]


@dataclass(frozen=True, slots=True)
class Costs:
    maker_fee: Decimal = Decimal(0)
    taker_fee: Decimal = Decimal(0)
    slippage: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class SmcParams:
    # structure (every timeframe)
    swing_len: int = 5  # bars on each side of a pivot
    atr_len: int = 14
    ob_lookback: int = 60  # bars before a break searched for the order-block candle
    ob_rule: str = "last_opposite"  # last_opposite: last opposite-colour candle before the
    # break / extreme: the extreme candle between the broken swing and the break (SMC-1.0)
    fvg_min_atr: Decimal = Decimal("0.5")  # smaller gaps are not drawn as zones
    ob_min_atr: Decimal = Decimal("0.5")  # order blocks below this x ATR are not kept
    ob_require_fvg: bool = True  # an OB needs an FVG (any size) in the move leaving it
    fvg_adjacent: bool = True  # ... and that FVG must start at the OB candle (bars j..j+2)
    fvg_fill: str = "wick"  # an FVG is gone once fully filled (wick) / only on a close (close)
    eq_tol_atr: Decimal = Decimal("0.1")  # swings this close (x ATR) form one equal-highs/lows pool
    require_sweep: bool = True  # a setup needs a liquidity sweep before the break
    sweep_max_bars: int = 12  # zone-TF bars from the sweep to the break at most
    # top-down model: bias -> zone (sweep, displacement, OB + FVG) -> execution
    htf_grid: str = "tehran"  # 1h / 4h bars: tehran (hh:30 UTC, Tabdeal's chart) / utc
    bias_tf: str = "4h"
    zone_tf: str = "1h"
    exec_tf: str = "15m"  # confirmation, trailing swings and time units
    confirm_exec: bool = False  # after arming wait for an exec-TF BOS/CHoCH with the setup
    confirm_entry: str = "market"  # (confirm_exec) market: at the confirming close / limit: a
    # limit at the entry price from that close on
    confirm_window_min: int = 0  # (confirm_exec) the break must close within this after
    # arming; 0 = pending_expiry_min
    entry_ref: str = "ob_edge"  # limit price: ob_edge (the OB edge touching the FVG) /
    # fvg_mid (50 % of the FVG after the OB)
    sl_ref: str = "wick"  # wick: one tick beyond the OB's wick / ob_height: beyond the OB by
    # its own height (long: OB low - OB height)
    tp_rr: Decimal = Decimal(0)  # > 0: TP = entry + tp_rr x stop distance (price R:R), in
    # place of the previous HH / LL; 0 = the HH / LL target
    # stop: one tick beyond the order block's wick; one target, independent of the stop: the
    # edge of the previous HH candle (long: its high) / LL candle (short: its low)
    tick: Decimal = Decimal("0.01")
    tp_ref: str = "leg"  # leg: the HH / LL after the OB candle, before price came back /
    # swing: the last confirmed zone-TF swing high / low beyond the entry
    tp_front_run_atr: Decimal = Decimal("0.05")  # TP this x ATR(zone TF) before the HH / LL
    # filters: they may reject a trade, they never move the stop or the target
    require_discount: bool = True  # long entry in the lower half of sweep -> HH (short mirrored)
    min_net_rr: Decimal = Decimal(2)  # net R at the TP after fees and slippage; 0 = off
    max_cost_frac: Decimal = Decimal(0)  # entry + exit fees + slippage vs the stop; 0 = off
    exit_on_choch: bool = False  # close at a zone-TF CHoCH against the open trade
    # data: closed bars analysed per timeframe, and the exchange history kept for them
    lookback_1m: int = 720
    lookback_5m: int = 600
    lookback_15m: int = 500
    lookback_1h: int = 400
    lookback_4h: int = 180
    lookback_1d: int = 120
    history_days: int = 35
    # lifecycle
    max_active: int = 1  # simultaneous active signals per symbol
    max_positions: int = 2  # simultaneous active signals across all symbols
    pending_expiry_min: int = 120  # an unfilled limit is cancelled 8 x 15m after arming
    time_stop_min: int = 0  # market exit when neither TP nor SL within this; 0 = off
    max_hold_min: int = 1440
    # chart (display only)
    chart_near_atr: Decimal = Decimal(3)  # setups within this x ATR(zone TF) of price
    chart_top_n: int = 3
    # shared simulated wallet: initial balance, risk per position, cross leverage cap
    account_usdt: Decimal = Decimal(100)
    risk_pct: Decimal = Decimal("0.01")
    max_leverage: Decimal = Decimal(10)

    def lookback(self, tf: str) -> int:
        return int(getattr(self, f"lookback_{tf}"))

    def as_dict(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}

    def digest(self) -> str:
        import hashlib

        return hashlib.sha256(repr(sorted(self.as_dict().items())).encode()).hexdigest()[:16]

    @classmethod
    def from_mapping(cls, m: dict[str, Any]) -> SmcParams:
        base = cls()
        kw: dict[str, Any] = {}
        for f in cls.__dataclass_fields__:
            if f not in m:
                continue
            cur = getattr(base, f)
            v = m[f]
            if isinstance(cur, Decimal):
                kw[f] = Decimal(str(v))
            elif isinstance(cur, tuple):
                items = v.split(",") if isinstance(v, str) else v
                kw[f] = tuple(str(x).strip() for x in items if str(x).strip())
            elif isinstance(cur, bool):  # before int: a bool is an int in Python
                if isinstance(v, str):
                    kw[f] = v.strip().lower() in ("1", "true", "yes", "on")
                else:
                    kw[f] = bool(v)
            elif isinstance(cur, int):
                kw[f] = int(v)
            else:
                kw[f] = str(v)
        return cls(**kw)


@dataclass(frozen=True, slots=True)
class ZoneSetup:
    """A complete zone-timeframe sequence: [sweep ->] displacement (BOS/CHoCH by close) -> order
    block (last opposite candle) with the FVG right after it. Known at `confirmed_at`."""

    key: str  # the order block's id
    tf: str
    direction: Side
    ob: Zone
    gap: tuple[Decimal, Decimal]  # (bottom, top) of the FVG
    sweep: Sweep | None  # None: no sweep before the break (require_sweep off)
    event: StructureEvent
    confirmed_at: datetime  # close of the zone bar on which the OB and its FVG were known

    @property
    def edge(self) -> Decimal:
        """The OB edge touching the FVG: the limit price."""
        return self.ob.top if self.direction is Side.LONG else self.ob.bottom


@dataclass(frozen=True, slots=True)
class Target:
    price: Decimal
    source: str  # what the level is (e.g. "1h HH")
    net_r: Decimal  # R after fees if the whole position closed there
    level: Decimal | None = None  # the HH / LL the target refers to (price = front-run of it)


@dataclass(frozen=True, slots=True)
class Setup:
    """A zone setup evaluated when it was armed (accepted -> a tradable signal)."""

    key: str
    direction: Side
    accepted: bool
    reasons: tuple[str, ...]
    created_at: datetime  # when the order exists (limit: open of the arming M1 bar)
    zone: ZoneSetup
    bias: int
    market: bool = False  # confirm_exec: entered at the exec-TF close (taker)
    entry: Decimal | None = None
    sl: Decimal | None = None
    tp: Target | None = None
    range_mid: Decimal | None = None  # 50 % of the dealing range (sweep wick -> HH / LL)
    cost_frac: Decimal | None = None  # entry + exit fees + slippage per unit / stop distance
    qty: Decimal | None = None
    notional: Decimal | None = None
    leverage: Decimal | None = None

    @property
    def net_rr(self) -> Decimal | None:
        return None if self.tp is None else self.tp.net_r
