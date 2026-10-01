"""Continuity checks for candle series."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from sp2l.core.types import Candle

M1 = timedelta(minutes=1)
M5 = timedelta(minutes=5)


class DiscontinuousSeriesError(ValueError):
    """A candle series has a missing or out-of-order bar."""


def require_contiguous(candles: Sequence[Candle], step: timedelta) -> None:
    for prev, cur in zip(candles, candles[1:], strict=False):
        if cur.open_time - prev.open_time != step:
            raise DiscontinuousSeriesError(
                f"gap/misorder between {prev.open_time.isoformat()} and "
                f"{cur.open_time.isoformat()} (expected step {step})"
            )
