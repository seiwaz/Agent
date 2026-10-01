from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sp2l.core.types import Candle

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def candle(
    i: int,
    o: str,
    h: str,
    lo: str,
    c: str,
    *,
    step_min: int = 1,
    volume: str = "0",
    trades: int = 0,
) -> Candle:
    return Candle(
        T0 + timedelta(minutes=step_min * i),
        Decimal(o),
        Decimal(h),
        Decimal(lo),
        Decimal(c),
        Decimal(volume),
        trades,
    )


def hl_series(rows: Sequence[tuple[str, str]], step_min: int = 5) -> list[Candle]:
    """Candles from (high, low) pairs; open/close set to the low/high midpoint."""
    out = []
    for i, (h, lo) in enumerate(rows):
        mid = str((Decimal(h) + Decimal(lo)) / 2)
        out.append(candle(i, mid, h, lo, mid, step_min=step_min))
    return out


def random_walk(n: int, seed: int, start: float = 60000.0) -> list[Candle]:
    rng = random.Random(seed)
    out = []
    px = start
    for i in range(n):
        o = px
        c = o + rng.gauss(0, 40)
        h = max(o, c) + abs(rng.gauss(0, 20))
        lo = min(o, c) - abs(rng.gauss(0, 20))
        px = c
        out.append(candle(i, f"{o:.2f}", f"{h:.2f}", f"{lo:.2f}", f"{c:.2f}", step_min=5))
    return out


@pytest.fixture(scope="session")
def walk600() -> list[Candle]:
    return random_walk(600, seed=7)
