"""P-Gap detection on three finalized M1 candles (PG-01..03; V5.9 qualification).

`detect_pgap` is the raw strict geometric inequality only. Whether the triple must also
satisfy the directional sequence (B04) is decided by the Spike detector.

V5.9 introduced a quality filter (strong impulse C2, strong gap). V5.12 REMOVED it as a rule:
`assess_pgap` still measures the same values (body/range, gap/body, gap ticks) and records
them with every P-Gap, but nothing is rejected by them. Runs and Spike candles ignore candle
colour (SEQ-03).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from sp2l.core.types import Candle, Side

BULLISH_EXPR = "low[i+1] > high[i-1]"
BEARISH_EXPR = "high[i+1] < low[i-1]"


def detect_pgap(left: Candle, middle: Candle, right: Candle) -> Side | None:
    """Return LONG for a bullish P-Gap, SHORT for bearish, None otherwise. Equality is invalid."""
    del middle  # the middle candle does not enter the P-Gap inequality
    if right.low > left.high:
        return Side.LONG
    if right.high < left.low:
        return Side.SHORT
    return None


# ---- V5.9 Core P-Gap qualification: strong impulse C2 + strong gap ---------------------------
# Frozen thresholds (no tuning, no ML). Compared exactly with Fraction; equality passes.
BODY_RATIO_MIN = Fraction(3, 5)  # 0.60
GAP_BODY_RATIO_MIN = Fraction(3, 20)  # 0.15
GAP_TICKS_MIN = 2

WRONG_DIRECTION = "PGAP_IMPULSE_WRONG_DIRECTION"
BODY_TOO_WEAK = "PGAP_IMPULSE_BODY_TOO_WEAK"
GAP_SMALL_VS_BODY = "PGAP_GAP_TOO_SMALL_RELATIVE_TO_BODY"
GAP_SMALL_TICKS = "PGAP_GAP_TOO_SMALL_IN_TICKS"


@dataclass(frozen=True, slots=True)
class PGapQuality:
    """Every measured value of one geometric P-Gap (C1 = i-1, C2 = i impulse, C3 = i+1)."""

    side: Side
    impulse_body: Decimal
    impulse_range: Decimal
    impulse_upper_shadow: Decimal
    impulse_lower_shadow: Decimal
    impulse_body_ratio: Fraction | None  # None when range <= 0
    impulse_direction: str  # BULLISH / BEARISH / DOJI
    gap_size: Decimal
    gap_body_ratio: Fraction | None  # None when body == 0
    gap_size_ticks: Fraction
    strong_impulse_pass: bool
    strong_gap_pass: bool
    reasons: tuple[str, ...]

    @property
    def final_pgap_pass(self) -> bool:
        return self.strong_impulse_pass and self.strong_gap_pass

    @property
    def primary_reason(self) -> str | None:
        return self.reasons[0] if self.reasons else None

    def as_dict(self) -> dict[str, object]:
        def s(v: object) -> str | None:
            if v is None:
                return None
            if isinstance(v, Fraction):
                return str(Decimal(v.numerator) / Decimal(v.denominator))
            return str(v)

        return {
            "impulse_body": s(self.impulse_body),
            "impulse_range": s(self.impulse_range),
            "impulse_upper_shadow": s(self.impulse_upper_shadow),
            "impulse_lower_shadow": s(self.impulse_lower_shadow),
            "impulse_body_ratio": s(self.impulse_body_ratio),
            "impulse_direction": self.impulse_direction,
            "gap_size": s(self.gap_size),
            "gap_body_ratio": s(self.gap_body_ratio),
            "gap_size_ticks": s(self.gap_size_ticks),
            "strong_impulse_pass": self.strong_impulse_pass,
            "strong_gap_pass": self.strong_gap_pass,
            "final_pgap_pass": self.final_pgap_pass,
            "enforced": False,  # V5.12: measured for the record, not a rule
            "failure_reasons": list(self.reasons),
            "thresholds": {
                "body_ratio_min": "0.60",
                "gap_body_ratio_min": "0.15",
                "gap_ticks_min": GAP_TICKS_MIN,
            },
        }


def assess_pgap(
    side: Side, left: Candle, middle: Candle, right: Candle, tick: Decimal
) -> PGapQuality:
    """V5.9 qualification of a geometric P-Gap of `side` (all values kept even on failure)."""
    rng = middle.high - middle.low
    body = abs(middle.close - middle.open)
    upper = middle.high - max(middle.open, middle.close)
    lower = min(middle.open, middle.close) - middle.low
    ratio = Fraction(body) / Fraction(rng) if rng > 0 else None
    direction = (
        "BULLISH"
        if middle.close > middle.open
        else "BEARISH"
        if middle.close < middle.open
        else "DOJI"
    )
    gap = right.low - left.high if side is Side.LONG else left.low - right.high
    gap_ratio = Fraction(gap) / Fraction(body) if body > 0 else None
    ticks = Fraction(gap) / Fraction(tick)
    reasons = []
    want = "BULLISH" if side is Side.LONG else "BEARISH"
    if direction != want:
        reasons.append(WRONG_DIRECTION)
    if ratio is None or ratio < BODY_RATIO_MIN:
        reasons.append(BODY_TOO_WEAK)
    strong_impulse = not reasons
    gap_fail = []
    if Fraction(gap) < GAP_BODY_RATIO_MIN * Fraction(body):
        gap_fail.append(GAP_SMALL_VS_BODY)
    if ticks < GAP_TICKS_MIN:
        gap_fail.append(GAP_SMALL_TICKS)
    return PGapQuality(
        side,
        body,
        rng,
        upper,
        lower,
        ratio,
        direction,
        gap,
        gap_ratio,
        ticks,
        strong_impulse,
        not gap_fail,
        tuple(reasons + gap_fail),
    )
