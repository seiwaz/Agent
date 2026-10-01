# Exhaustion Gate Specification V5 (rev 5.5)

> **V6.0 (2026-10-01): ADVISORY ONLY — not a gate.** Everything below is still evaluated and recorded (exhaustion_snapshots), but a REJECT or UNKNOWN result never rejects a setup: the snapshot is stored as PASS with sub-reason `ADVISORY_WOULD_REJECT` (or `ADVISORY_UNKNOWN`) plus the original trigger sub-reasons, so analytics can still ask "would exhaustion have rejected?". `SetupState.REJECTED_EXHAUSTION` remains only for historical rows.

Purpose (V5): reject late-trend/climactic Spike entries without reducing every large breakout to a simple ATR cutoff.

Evaluate after Context Engine and before every initial/replacement E1 submit. Use only finalized data available at evaluation time.

## 1. ATR14_M5
Wilder ATR14 on finalized M5. Fully initialized only.

## 2. EMA20_M5
Standard EMA alpha=`2/(20+1)` on M5 closes. Initialize from SMA20 of the first 20 bars in the loaded continuous history, then recurse. Require sufficient warmup; no ad-hoc restart at each query.

## 3. TrendAgeBars
A `trend state change` is the confirmation time when M5Trend (per Context spec) changes from not-current-direction to current direction.
Count finalized M5 bars from that confirmation bar through current context bar inclusive.
If current M5Trend is not aligned with setup direction, TrendAgeBars=0.
No inferred trend before enough confirmed pivots.

## 4. MicrochannelLen
Using finalized M5 bars ending at current context bar:
- Long: count consecutive bars backward while `Low[n] >= Low[n-1]`.
- Short: count consecutive bars backward while `High[n] <= High[n-1]`.
Minimum value 1 when at least one context bar exists.
Stop at first violation or missing bar.

## 5. StretchATR
Use M1 SpikeExtreme known at evaluation time:
- Long SpikeExtreme = max High of active M1 Spike candles through LastSpikeCandle.
- Short = min Low.
`StretchATR = abs(SpikeExtreme - EMA20_M5) / ATR14_M5`.

## 6. SpikeATR
`SpikeRange_M1 = max(High)-min(Low)` across Origin..LastSpikeCandle finalized M1 candles.
`SpikeATR = SpikeRange_M1 / ATR14_M5`.

## 7. RangePosition20
On last 20 finalized M5 bars, inclusive of the current context bar (V5.1 B12):
`RP20=(SpikeExtreme-LL20)/(HH20-LL20)`.
If HH20==LL20 => exhaustion unknown => fail closed before E1.
Do not clamp for logic.

## 8. OpposingSwingDistance
Long: nearest confirmed, still-unbroken M5 Swing High strictly above SpikeExtreme. Distance = level-SpikeExtreme.
Short: nearest still-unbroken Swing Low below SpikeExtreme. Distance = SpikeExtreme-level.
If none => +INF.
V5.4 (B38): candidates are confirmed, M5-valid swing levels strictly beyond the current SpikeExtreme in the trade direction (Long: above; Short: below). Levels crossed earlier and now behind the SpikeExtreme are not candidates. There is no setup-local removal.
Normalize: `OpposingSwingDistanceATR=Distance/ATR14_M5`.

## 9. Conditions
`LateTrend = TrendAgeBars>=20 OR MicrochannelLen>=8`.
`ExtremeStretch = StretchATR>=2.0`.
`ClimacticSpike = SpikeATR>=1.5`.
`AtOuterEdge = (Long and RP20>=0.90) OR (Short and RP20<=0.10) OR OpposingSwingDistanceATR<=0.5`.
Boundary equality triggers the condition.

## 10. FreshBreakoutException
True only when ALL:
1. BreakoutContext=true against a confirmed M5 swing level;
2. `PreviousRegimeAtBreakoutStart` was RANGE or TRANSITION;
3. `BreakoutStartM5Index` is within current context bar or one immediately prior finalized M5 bar (age 0 or 1; equivalently within last 2 finalized M5 bars).

BreakoutStart definition:
- Long: first finalized M1 Spike candle that closed strictly above the specific M5 swing-high breakout level.
- Short symmetric.
Map that M1 candle to its containing M5 bucket by M1 **open_time** (V5.1 B13). A breakout inside the currently forming M5 bucket has age 0. The regime used is the regime of the most recent finalized M5 bar strictly BEFORE that breakout M1 close; never use future M5 data.

If this prior regime cannot be computed, FreshBreakoutException=false.

## 11. Reject expression
Reject `EXHAUSTION_RISK` iff:
`LateTrend AND ExtremeStretch AND (ClimacticSpike OR AtOuterEdge) AND NOT FreshBreakoutException`.

Record sub-reasons:
- LATE_TREND_AGE
- LONG_MICROCHANNEL / SHORT_MICROCHANNEL
- EXTREME_STRETCH
- CLIMACTIC_SPIKE
- OUTER_EDGE_20
- OPPOSING_SWING_NEAR
- FRESH_BREAKOUT_EXCEPTION

These thresholds are frozen V5 project parameters, not universal market laws.

## V5.2
OpposingSwingDistanceATR is measured from the current SpikeExtreme to the nearest opposing confirmed, still-valid M5 swing (including dual-pivot levels, B32). It is separate from RoomToTP, which is measured from E1 (B27).
