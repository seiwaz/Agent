"""Hand-built zone-timeframe candles, expanded to consistent M1 data.

Every 1h row (open, high, low, close) becomes 60 one-minute candles that walk open -> low ->
high -> close (bullish) or open -> high -> low -> close (bearish), so aggregating the minutes
gives back exactly the hourly row and every timeframe agrees.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from sp2l.core.types import Candle
from sp2l.smc.model import SmcParams

T0 = datetime(2026, 1, 5, 0, 30, tzinfo=UTC)  # a Monday, on the 1h / 4h Tehran grid
H = timedelta(hours=1)
MIN = timedelta(minutes=1)
Row = tuple[float, float, float, float]

# Long setup, bars of 1h (swing_len 2):
#  4: swing low 95 (confirmed at 6), 8: swing high 104 (confirmed at 10)
# 12: sweep - wick 94 below 95, close 96.5 back inside; bearish: the last opposite candle
# 13: displacement up; 14: its low 99 is above bar 12's high 97.5 -> FVG 97.5-99 right after
# 15: close 105.5 above 104 -> BOS; the setup (OB 94-97.5, FVG 97.5-99) is known at its close
# 19: first trade into the FVG (arming); 20: reaches the OB edge 97.5 (fill)
SETUP: list[Row] = [
    (100, 100.5, 99, 99.5),
    (99.5, 99.8, 97, 97.5),
    (97.5, 98, 96, 96.5),
    (96.5, 97, 95.5, 96),
    (96, 96.5, 95, 95.8),
    (95.8, 98, 95.6, 97.8),
    (97.8, 100, 97.5, 99.8),
    (99.8, 102, 99.5, 101.8),
    (101.8, 104, 101.5, 103),
    (103, 103.5, 100, 100.5),
    (100.5, 101, 98, 98.5),
    (98.5, 99, 96.5, 97),
    (97, 97.5, 94, 96.5),
    (96.5, 101, 96.4, 100.8),
    (100.8, 103.5, 99, 103.2),
    (103.2, 106, 103, 105.5),
    (105.5, 107, 105, 106.5),
    (106.5, 107.5, 104, 104.5),
    (104.5, 105, 100, 100.5),
    (100.5, 101, 98.5, 99),
    (99, 99.5, 97, 98),
]
ARMING_BAR, FILL_BAR, BREAK_BAR, SWEEP_BAR = 19, 20, 15, 12

# zone timeframe = 1h, bias read on 1h as well (the fixture is too short for 4h structure)
P = SmcParams(
    swing_len=2,
    atr_len=3,
    fvg_min_atr=D(0),
    bias_tf="1h",
    lookback_15m=1400,
)


def expand(rows: list[Row], t0: datetime = T0) -> list[Candle]:
    out: list[Candle] = []
    for h, (o, hi, lo, c) in enumerate(rows):
        pts = [o, hi, lo, c] if c < o else [o, lo, hi, c]
        path = []
        for seg in range(3):  # 20 minutes per leg
            a, b = pts[seg], pts[seg + 1]
            path += [a + (b - a) * k / 20 for k in range(20)]
        path.append(c)
        for m in range(60):
            a, b = path[m], path[m + 1]
            if m == 59:
                b = c
            t = t0 + h * H + m * MIN
            out.append(
                Candle(
                    t,
                    D(f"{a:.4f}"),
                    D(f"{max(a, b):.4f}"),
                    D(f"{min(a, b):.4f}"),
                    D(f"{b:.4f}"),
                    D(1),
                )
            )
    return out


def hourly(rows: list[Row], t0: datetime = T0) -> list[Candle]:
    return [
        Candle(t0 + i * H, D(str(o)), D(str(h)), D(str(lo)), D(str(c)), D(1))
        for i, (o, h, lo, c) in enumerate(rows)
    ]
