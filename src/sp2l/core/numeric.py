"""Exact numerics for decision paths (plan §7).

Prices and quantities are Decimal parsed from exchange strings. Recursive indicators
run under a fixed Decimal context so replays are bit-identical. Floats are never used
for a strategy decision.
"""

from __future__ import annotations

import decimal
from collections.abc import Sequence
from decimal import Decimal

PRECISION = 50

DECISION_CONTEXT = decimal.Context(
    prec=PRECISION,
    rounding=decimal.ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
)


def D(value: str | int | Decimal) -> Decimal:
    """Build a Decimal from a string/int. Floats are rejected to keep inputs exact."""
    if isinstance(value, float):  # pragma: no cover - guarded by typing too
        raise TypeError("float inputs are forbidden in decision paths; pass a string")
    return Decimal(value)


def div(a: Decimal, b: Decimal) -> Decimal:
    with decimal.localcontext(DECISION_CONTEXT):
        return a / b


def mul(a: Decimal, b: Decimal) -> Decimal:
    with decimal.localcontext(DECISION_CONTEXT):
        return a * b


def add(a: Decimal, b: Decimal) -> Decimal:
    with decimal.localcontext(DECISION_CONTEXT):
        return a + b


def sub(a: Decimal, b: Decimal) -> Decimal:
    with decimal.localcontext(DECISION_CONTEXT):
        return a - b


def log10(x: Decimal) -> Decimal:
    with decimal.localcontext(DECISION_CONTEXT):
        return x.log10()


def mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("mean of empty sequence")
    with decimal.localcontext(DECISION_CONTEXT):
        total = Decimal(0)
        for v in values:
            total += v
        return total / len(values)


def median_even(values: Sequence[Decimal]) -> Decimal:
    """Median of an even-length sequence: mean of the two middle sorted values.

    CE§12: for N=20 this is the mean of sorted values #10 and #11 (1-indexed).
    """
    n = len(values)
    if n == 0 or n % 2:
        raise ValueError(f"median_even requires a non-empty even-length sequence, got {n}")
    s = sorted(values)
    with decimal.localcontext(DECISION_CONTEXT):
        return (s[n // 2 - 1] + s[n // 2]) / 2


def is_on_tick(price: Decimal, tick: Decimal) -> bool:
    if tick <= 0:
        raise ValueError("tick must be positive")
    with decimal.localcontext(DECISION_CONTEXT):
        return (price % tick) == 0
