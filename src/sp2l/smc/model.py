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

VERSION = "SMC-1.0"


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
    # structure
    swing_len: int = 5  # bars on each side of a pivot
    atr_len: int = 14
    ob_lookback: int = 60  # bars before a break searched for the order-block candle
    fvg_min_atr: Decimal = Decimal("0.1")  # smaller gaps are noise
    # top-down model: bias -> point of interest -> M1 trigger
    bias_tf: str = "4h"
    confirm_bias_tf: str = "1h"  # +1 score when it agrees
    poi_tfs: tuple[str, ...] = ("1h", "15m")  # priority order
    trigger_tf: str = "1m"
    # entry / stop / target
    entry_mode: str = "market"  # market (trigger close) / proximal / mid / distal of the zone
    sl_mode: str = "poi"  # poi: beyond the HTF zone and the M1 block / m1: the M1 block only
    sl_buffer_atr: Decimal = Decimal("0.25")  # in ATR of the timeframe the stop sits on
    min_sl_atr: Decimal = Decimal("0")  # stop at least this many POI-timeframe ATRs away
    max_risk_pct: Decimal = Decimal("0.01")  # entry-to-SL distance cap (fraction of price)
    min_net_rr: Decimal = Decimal("1.5")  # reward/risk after fees and slippage
    tp_mode: str = "liquidity"  # liquidity: nearest pool paying min_net_rr / rr: exactly that
    entry_on: str = "m1"  # m1: the M1 order block / poi: the higher-timeframe zone
    min_score: int = 2
    require: tuple[str, ...] = ()  # confluence factors that are mandatory (e.g. sweep, fresh)
    tick: Decimal = Decimal("0.01")
    # data: closed bars analysed per timeframe, and the exchange history kept for them
    lookback_1m: int = 720
    lookback_5m: int = 600
    lookback_15m: int = 500
    lookback_1h: int = 400
    lookback_4h: int = 180
    history_days: int = 35
    # lifecycle
    max_active: int = 1  # simultaneous PENDING/OPEN signals
    pending_expiry_min: int = 45
    max_hold_min: int = 360
    # advisory sizing
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
            elif isinstance(cur, int):
                kw[f] = int(v)
            else:
                kw[f] = str(v)
        return cls(**kw)


@dataclass(frozen=True, slots=True)
class Setup:
    """A trigger evaluated against the top-down model (accepted -> a tradable signal)."""

    key: str
    direction: Side
    accepted: bool
    reasons: tuple[str, ...]
    created_at: datetime  # close time of the M1 bar that confirmed the trigger
    trigger_kind: str  # BOS / CHOCH on M1
    trigger_event_id: str
    bias: int
    poi: Zone | None = None
    poi_tf: str | None = None
    entry_zone: Zone | None = None  # the M1 order block
    entry: Decimal | None = None
    sl: Decimal | None = None
    tp: Decimal | None = None
    tp_source: str | None = None
    rr: Decimal | None = None  # gross
    net_rr: Decimal | None = None
    score: int = 0
    factors: dict[str, bool] = field(default_factory=dict)
    qty: Decimal | None = None
    notional: Decimal | None = None
    leverage: Decimal | None = None
