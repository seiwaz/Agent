"""Deterministic synthetic trade tape with trending bursts (so Spikes/P-Gaps occur)."""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from sp2l.marketdata.m1_builder import Trade
from tests.conftest import T0


def tape(minutes: int, seed: int, start: float = 60000.0) -> list[Trade]:
    rng = random.Random(seed)
    out: list[Trade] = []
    px = start
    drift = 0.0
    tid = 0
    t = 0.0
    end = minutes * 60.0
    while t < end:
        if rng.random() < 0.004:  # regime switch: trend burst or calm
            drift = rng.choice([0.0, 0.0, 6.0, -6.0])
        px += drift + rng.gauss(0, 8)
        px = max(px, 1000.0)
        tid += 1
        ts = T0 + timedelta(seconds=t)
        recv = ts + timedelta(milliseconds=rng.randint(20, 400))
        out.append(Trade(str(tid), ts, recv, Decimal(f"{px:.1f}"), Decimal("0.01")))
        t += rng.uniform(2, 12)
    return out
