"""V5.8 tier-3 recovery: validated Tabdeal chart history -> CANDLE_HISTORY_REPAIR minutes.

Source (read-only, public, the same data Tabdeal's own chart shows):
GET https://api-web.tabdeal.org/special-margin/plots/history/ (resolution 1). Bars are
TradingView-continuous: open = previous bar's close, high/low include that open; trades are
bucketed by Tabdeal record time; there is NO trade count. See docs/tabdeal_history_source.md.

A chart bar is accepted for a minute only if, in the SAME response:
- it is final (its minute ended at least `settle` before the request) and structurally valid;
- every minute that must be repaired is present (no partial repair of an interval);
- the response agrees with our own reconciled canonical minutes around the gap: at least
  `min_overlap` comparable minutes, of which at least `min_agreement` match exactly under the
  continuity model (H, L, C, V), and none differs in H or L by more than `max_rel_diff`.
Anything else fails closed (the minutes stay DATA_GAP). Pure and deterministic.

A repaired candle restores price history (M5, ATR, ADX, EMA, CHOP, pivots, trend, regime,
range). It never supplies a trade count (UNKNOWN), fills, PullbackStart timing, SL/TP order,
partial fills or intrabar chronology.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

from sp2l.core.types import Candle

MINUTE = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class HistoryPolicy:
    settle: timedelta = timedelta(seconds=5)
    min_overlap: int = 5
    min_agreement: Fraction = Fraction(4, 5)
    max_rel_diff: Fraction = Fraction(1, 1000)
    overlap: timedelta = timedelta(minutes=15)  # canonical minutes compared on each side


DEFAULT_POLICY = HistoryPolicy()


@dataclass(slots=True)
class HistoryOutcome:
    ok: bool
    reason: str | None
    candles: dict[datetime, Candle] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)


def _d(x: Any) -> Decimal:
    return Decimal(str(x))


def parse_bars(raw: Iterable[Mapping[str, Any]]) -> dict[datetime, Candle] | None:
    """Chart bars -> candles (trade count UNKNOWN). None if any bar is malformed."""
    out: dict[datetime, Candle] = {}
    try:
        for b in raw:
            t = datetime.fromtimestamp(int(b["time"]), UTC)
            if t.second or t.microsecond:
                return None
            o, h, lo, c, v = (_d(b[k]) for k in ("open", "high", "low", "close", "volume"))
            if v == 0:  # the chart records no trade in this minute: a proven quiet minute
                if not (o == h == lo == c):
                    return None
                out[t] = Candle(t, o, h, lo, c, Decimal(0), 0, synthetic=True)
            else:
                out[t] = Candle(t, o, h, lo, c, v, None)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
    return out


def validate(
    raw: Iterable[Mapping[str, Any]],
    needed: list[datetime],
    canonical: Mapping[datetime, Candle],
    requested_at: datetime,
    policy: HistoryPolicy = DEFAULT_POLICY,
) -> HistoryOutcome:
    bars = parse_bars(raw)
    if bars is None:
        return HistoryOutcome(False, "HISTORY_BAR_INVALID")
    missing = [m for m in needed if m not in bars]
    if missing:
        return HistoryOutcome(
            False, "HISTORY_INCOMPLETE", detail={"missing": [m.isoformat() for m in missing[:20]]}
        )
    if needed and max(needed) + MINUTE + policy.settle > requested_at:
        return HistoryOutcome(False, "HISTORY_NOT_FINAL")
    compared = exact = 0
    worst = Fraction(0)
    mismatches: list[dict[str, Any]] = []
    for m, c in sorted(canonical.items()):
        h, hp = bars.get(m), bars.get(m - MINUTE)
        if h is None or hp is None or c.synthetic or h.synthetic:
            continue
        compared += 1
        pc = hp.close
        exp = (max(pc, c.high), min(pc, c.low), c.close, c.volume)
        got = (h.high, h.low, h.close, h.volume)
        if exp == got:
            exact += 1
            continue
        rel = max(Fraction(abs(exp[0] - got[0])), Fraction(abs(exp[1] - got[1]))) / Fraction(
            c.close
        )
        worst = max(worst, rel)
        if len(mismatches) < 10:
            mismatches.append(
                {
                    "minute": m.isoformat(),
                    "expected": [str(x) for x in exp],
                    "chart": [str(x) for x in got],
                }
            )
    detail: dict[str, Any] = {
        "overlap_compared": compared,
        "overlap_exact": exact,
        "overlap_max_hl_rel_diff": str(round(float(worst), 8)),
        "overlap_mismatches": mismatches,
        "repaired_minutes": len(needed),
        "first": needed[0].isoformat() if needed else None,
        "last": needed[-1].isoformat() if needed else None,
    }
    if compared < policy.min_overlap:
        return HistoryOutcome(False, "HISTORY_OVERLAP_INSUFFICIENT", detail=detail)
    if Fraction(exact, compared) < policy.min_agreement or worst > policy.max_rel_diff:
        return HistoryOutcome(False, "HISTORY_VALIDATION_FAILED", detail=detail)
    return HistoryOutcome(True, None, {m: bars[m] for m in needed}, detail)
