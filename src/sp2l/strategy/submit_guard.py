"""E1 submit guard (V5.1 B15): never submit a marketable E1.

Long: submit only if last_trade > E1. Short: only if last_trade < E1. Equality is not
safe. If unsafe, nothing is sent; the setup waits for the next finalized M1 close and is
fully re-evaluated. E1 is never converted into a market/taker entry.
"""

from __future__ import annotations

from decimal import Decimal

from sp2l.core.types import Side


def e1_submit_safe(side: Side, e1: Decimal, last_trade: Decimal | None) -> bool:
    if last_trade is None:
        return False
    return last_trade > e1 if side is Side.LONG else last_trade < e1
