"""Per-bar indicator snapshot for display (UI-02: the backend computes, the WebUI renders).

Built from the engine's own M5 state right after each finalized M5 bar, so the values shown
are exactly the ones the Context / Exhaustion gates read (never a forming bar, MKT-02).
Every number is exact text; rounding happens only in the API presentation layer.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any

from sp2l.indicators.m5_state import M5State
from sp2l.indicators.regime import (
    ADX_RANGE_BELOW,
    ADX_TREND_MIN,
    CHOP_RANGE_MIN,
    CHOP_TREND_MAX,
    RANGE_EXPR,
    TREND_EXPR,
    Regime,
)
from sp2l.indicators.trend import Trend

RANGE_BARS = 14


def _s(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, Fraction):
        return str(Decimal(v.numerator) / Decimal(v.denominator))
    return str(v) if isinstance(v, Decimal | int | str) else None  # Unknown -> None


def indicator_snapshot(state: M5State) -> dict[str, Any] | None:
    from sp2l.strategy.context.engine import ContextSnapshot, _liquidity

    seg, bar = state.segment, state.last
    if seg is None or bar is None:
        return None
    k = bar.index
    c = bar.candle
    window = seg.bars[max(0, k - RANGE_BARS + 1) : k + 1]
    rng_hi = max(b.candle.high for b in window) if len(window) == RANGE_BARS else None
    rng_lo = min(b.candle.low for b in window) if len(window) == RANGE_BARS else None
    rp = (
        Fraction(c.close - rng_lo) / Fraction(rng_hi - rng_lo)
        if rng_hi is not None and rng_lo is not None and rng_hi != rng_lo
        else None
    )
    highs = [p for p in seg.pivots if p.confirmed_index <= k and p.has_high]
    lows = [p for p in seg.pivots if p.confirmed_index <= k and p.has_low]
    liq = ContextSnapshot()
    _liquidity(liq, seg.bars, k)
    chop, adx = bar.chop14, bar.adx.adx
    if bar.regime is Regime.RANGE:
        rule = f"RANGE: {RANGE_EXPR}"
    elif bar.regime is Regime.TREND:
        rule = f"TREND: {TREND_EXPR}"
    elif bar.regime is Regime.TRANSITION:
        rule = "TRANSITION: neither RANGE nor TREND"
    else:
        rule = "UNKNOWN: CHOP14 or ADX14 not initialized"
    trend_age = state.trend_age(bar.trend, k) if bar.trend in (Trend.BULL, Trend.BEAR) else 0
    return {
        "open_time": bar.open_time.isoformat(),
        "close_time": bar.close_time.isoformat(),
        "bar_index": k,
        "anchor": seg.anchor_open_time.isoformat(),
        "candle": {
            "open": _s(c.open),
            "high": _s(c.high),
            "low": _s(c.low),
            "close": _s(c.close),
            "volume": _s(c.volume),
            "trade_count": c.trade_count,
        },
        "atr14": _s(bar.atr14),
        "ema20": _s(bar.ema20),
        "adx14": _s(adx),
        "plus_di": _s(bar.adx.plus_di),
        "minus_di": _s(bar.adx.minus_di),
        "dx": _s(bar.adx.dx),
        "chop14": _s(chop),
        "regime": bar.regime.value,
        "regime_rule": rule,
        "regime_thresholds": {
            "chop_range_min": str(CHOP_RANGE_MIN),
            "adx_range_below": str(ADX_RANGE_BELOW),
            "chop_trend_max": str(CHOP_TREND_MAX),
            "adx_trend_min": str(ADX_TREND_MIN),
        },
        "trend": bar.trend.value,
        "trend_age_bars": trend_age,
        "range14_high": _s(rng_hi),
        "range14_low": _s(rng_lo),
        "range_position_close": _s(rp),
        "swing_high": _s(highs[-1].high) if highs else None,
        "swing_low": _s(lows[-1].low) if lows else None,
        "pivots_confirmed": len(seg.pivots),
        "volume_ratio": _s(liq.volume_ratio),
        "tradecount_ratio": _s(liq.tradecount_ratio),
        "liquidity_status": liq.liquidity_status,
        "liquidity_basis": liq.liquidity_basis,
        "price_context_ready": state.price_ready,
        "liquidity_context_ready": state.liquidity_ready,
        "liquidity_bars_missing": state.liquidity_bars_missing(),
        "segment_bars": len(seg.bars),
        "warmup_target": state.warmup_bars,
    }
