# Context Engine Specification V5 (rev 5.5)

## 1. Input timing
Evaluate using the latest finalized M5 candle whose close_time <= evaluation_time. Never use the forming M5 candle. Evaluate before initial E1 submit and before every zero-fill E1 replacement.

If required warmup is unavailable => `CONTEXT_UNKNOWN_WARMUP` => no E1.

## 2. Confirmed M5 pivots
A pivot at index `i` is confirmable only when bars `i+1` and `i+2` are finalized.

Swing High:
`High[i] > High[i-1], High[i-2], High[i+1], High[i+2]`

Swing Low:
`Low[i] < Low[i-1], Low[i-2], Low[i+1], Low[i+2]`

Strict inequalities. Any equality => not a pivot. A single candle may not be both a Swing High and Swing Low. If pathological data mathematically satisfies both after preprocessing, mark data invalid and fail closed.

## 3. M5 Trend
Let last two confirmed highs be `SH1` older, `SH2` newer; lows `SL1` older, `SL2` newer.
- BULL if `SH2.price > SH1.price AND SL2.price > SL1.price`.
- BEAR if `SH2.price < SH1.price AND SL2.price < SL1.price`.
- Otherwise NEUTRAL.
Missing 2 highs or 2 lows => NEUTRAL_INSUFFICIENT_STRUCTURE, which does not satisfy alignment.

## 4. ADX14
Use Wilder True Range, +DM, -DM, Wilder smoothing, then DX and Wilder-smoothed ADX. Use finalized M5 only. Require enough history to produce a fully initialized ADX14; implementation must test against a reference library. Do not approximate with SMA.

## 5. CHOP14
For 14 finalized M5 bars ending at context bar:
`CHOP = 100 * log10(sum(TR14)/(HH14-LL14)) / log10(14)`.
If `HH14==LL14`, context is invalid/unknown => fail closed.

## 6. Regime precedence
Evaluate RANGE first, then TREND, else TRANSITION:
- RANGE iff `CHOP14>=61.8 AND ADX14<20`.
- TREND iff not RANGE and (`CHOP14<=38.2 OR ADX14>=25`).
- otherwise TRANSITION.
This precedence prevents an overlapping condition from being guessed.

## 7. Range Position
Bounds use last 14 finalized M5 bars, inclusive of context bar:
- RangeHigh = max High
- RangeLow = min Low
- `RangePosition=(price-RangeLow)/(RangeHigh-RangeLow)`
Use `price = M1 SpikeOrigin price reference`: Long uses OriginLow, Short uses OriginHigh for RangeEdgeOrigin. RangeMiddle uses the raw RangePosition of the current (prospective or active) E1 (V5.1 B07).
Clamp display value to [0,1] (display only). Do not clamp for logic: if outside, keep raw value and classify as breakout/outside range.

RangeMiddle hard reject only if Regime=RANGE and E1 raw RangePosition is within inclusive [1/3,2/3]. Bounds are exact rationals 1/3 and 2/3 (V5.1 B08).

RangeEdgeOrigin:
- Long true iff Regime=RANGE and Origin raw RangePosition <=1/3.
- Short true iff Regime=RANGE and Origin raw RangePosition >=2/3.

## 8. BreakoutContext
Use latest confirmed M5 swing obstacle known strictly before/equal evaluation time.
Long BreakoutContext true iff any finalized M1 candle belonging to the active Spike closes strictly above the latest confirmed M5 Swing High obstacle.
Short symmetric below latest confirmed Swing Low.
Wick-only breaks do not count. Equality does not count.

A breakout level is considered broken only by such a qualifying close. Once broken by the active Spike, BreakoutContext remains true for this setup.

## 9. HTF Alignment
- Long true iff M5Trend=BULL and Regime!=RANGE.
- Short true iff M5Trend=BEAR and Regime!=RANGE.

If M5Trend is opposite and BreakoutContext=false => hard reject `HTF_OPPOSITE_NO_BREAKOUT`.

## 10. Require one Context
At least one must be true:
- BreakoutContext
- HTFAlignment
- RangeEdgeOrigin
Else reject `NO_VALID_CONTEXT`.

## 11. Nearest unbroken obstacle / RoomToTP
At evaluation time build confirmed M5 swing levels known at that time.
Long obstacle candidates = confirmed Swing Highs with price > E1 that have NOT been invalidated by any finalized M5 close strictly above that level after its confirmation and before evaluation.
Short = Swing Lows < E1 not invalidated by finalized M5 close strictly below.
Nearest obstacle = candidate with minimum absolute distance from E1.
V5.4 (B10 reworded by B38): there is no setup-local removal of levels. Obstacle candidacy is path-relative to the current (prospective) E1: Long iff `E1 < level <= TP`; Short iff `TP <= level < E1`. Levels must be confirmed and not invalidated by a finalized M5 close. A level the Spike crossed earlier is again an obstacle if the current E1 is back on the other side of it; a level already behind the current E1 is not an obstacle. The global M5 pivot record keeps its M5-based `broken_at`.

If no obstacle candidate exists, `RoomToTP = +INF` and this gate passes.
Otherwise:
- Long Room = obstacle-E1
- Short Room = E1-obstacle
- `RoomToTP_R = Room / R`
Pass iff `RoomToTP_R >= 1.0` (equality passes).

## 12. Liquidity gate
Last finalized M5 bar = L.
Reference set = exactly 20 finalized M5 bars immediately preceding L, excluding L.
Median for even N=20 = arithmetic mean of sorted values #10 and #11 (1-indexed).
Require both Volume and TradeCount available and nonnegative.
Reject `LOW_LIQUIDITY` iff:
`L.Volume < 0.5*MedianVolume AND L.TradeCount < 0.5*MedianTradeCount`.
Equality passes.
Missing/invalid fields => `LIQUIDITY_UNKNOWN` => fail closed for Live; Shadow records candidate but no simulated execution unless explicitly running a research mode.

V5.10: the reject is a conjunction, so a missing TradeCount (L or any reference bar; e.g. Tabdeal chart-history repair) is decided by Volume when Volume is known:
- `L.Volume >= 0.5*MedianVolume` => the conjunction is false whatever TradeCount is => PASS (basis `VOLUME_ONLY`).
- `L.Volume < 0.5*MedianVolume` => the result depends on the unknown TradeCount => `LIQUIDITY_UNKNOWN` (fail closed, unchanged).
- Fewer than 20 reference bars => `LIQUIDITY_UNKNOWN` (unchanged). No TradeCount is ever invented; formula and thresholds are unchanged. `LOW_LIQUIDITY` is only ever decided with both values known.

## 13. Re-evaluation and pending E1
Before each E1 replacement, recompute Context from data available at that candle close.
If E1 has zero executedQty and Context turns invalid:
- stop repricing;
- safe cancel/reconcile active E1;
- if no fill => finalize `CONTEXT_INVALIDATED_BEFORE_FILL` with reason codes;
- if a fill is discovered during cancel race => enter fill/protection flow; do NOT retroactively reject the executed fill.
After any E1 execution, Context no longer cancels the live position.

## V5.1 resolutions (B06, B09, B11, B25)
- **Warmup (B06, B25):**
  - Recursive indicators use a fixed anchor per symbol.
  - After any data gap or missing M5 bar, re-anchor. Context stays `CONTEXT_UNKNOWN_WARMUP` until 150 finalized M5 bars exist since the anchor.
  - Any zero denominator is UNKNOWN (fail closed).
- **ADX14 (B06+):** TA-Lib is the canonical implementation. Wilder sums are seeded with n−1 = 13 values and the recursion is applied from bar n. The implementation is never switched at runtime.
- **Dual pivot (B11):** fail closed only when a DUAL pivot is one of SH1/SH2/SL1/SL2 in the current evaluation.
- **BreakoutContext level (B09):**
  - Freeze the nearest confirmed, M5-unbroken swing level at Spike confirmation.
  - Long uses a Swing High; Short uses a Swing Low.
  - Superseded by V5.3 B27 (no distance-based selection).

## V5.2 resolutions (B27, B32)
- **RoomToTP (B27):** measured from the prospective or current E1. Long uses the nearest confirmed valid resistance above E1; Short uses the nearest confirmed valid support below E1. The Exhaustion OpposingSwingDistanceATR is a separate metric measured from the current SpikeExtreme.
- **Dual pivots (B32):** a confirmed dual-pivot bar may supply its High as resistance / Long breakout level and its Low as support / Short breakout level, for both obstacles and BreakoutContext. B11 still applies to trend classification: a dual pivot among SH1/SH2/SL1/SL2 makes the trend fail closed.
- **BreakoutContext level:** superseded by V5.3 B27 below.

## V5.3 resolution (B27): BreakoutContext level selection
At Spike confirmation, BreakoutContext is determined from confirmed M5 swing levels that existed before the Spike. No "nearest" rule is used, and no selection by distance from Origin, E1 or last trade.

- **Long:** collect the confirmed, setup-eligible M5 Swing High levels (including a dual pivot's High) that at least one finalized M1 candle of the current Spike closed strictly above.
  - If any exist: `BreakoutContext = true` and `BreakoutLevel` = the **highest** crossed qualifying Swing High.
  - Otherwise: `BreakoutContext = false`.
- **Short:** symmetric. A Swing Low qualifies if a finalized M1 close is strictly below it; freeze the **lowest** crossed qualifying Swing Low.
- The BreakoutLevel is frozen per setup once selected. For RoomToTP and OpposingSwing it has no special status: path-relative geometry decides (V5.4 B38).
- **Clarification (settles the former provisional B35/B36):**
  - **Setup-eligible level:** a confirmed M5 pivot level (Long: Swing High; Short: Swing Low; dual-pivot High/Low eligible per B32). Its pivot was confirmed no later than `SpikeOriginCandle.open_time`, and the level was still unbroken at that time. Pivots confirmed after the SpikeOrigin are never eligible for this setup.
  - **Freezing the set:** the complete set `EligibleBreakoutLevels` is frozen at setup creation; newly confirmed pivots are never added.
  - **Updating the level:** BreakoutContext may become true after the initial Spike confirmation. While PullbackStart has not occurred, E1 has zero executed quantity and the same Spike sequence is still extending, each newly finalized M1 candle is checked against the frozen set.
    - Long: `BreakoutLevel` = the highest crossed eligible Swing High (close strictly above).
    - Short: `BreakoutLevel` = the lowest crossed eligible Swing Low (close strictly below).
  - **Advancing and freezing:** the level may advance to a farther crossed eligible level during zero-fill extension. It freezes permanently at PullbackStart or the first E1 fill, whichever comes first.

## V5.4 (B37, B38)
- **Pivot data quality (B37):**
  - A fully synthetic but healthy/anchored M5 bar can never itself be a pivot, but it may be one of the 2L/2R neighbours of another pivot.
  - A DATA_GAP or UNANCHORED M5 bar can never be a pivot or a neighbour. Any pivot evaluation whose 2L/2R window contains such a bar is UNKNOWN / not confirmed. Because a DATA_GAP ends the M5 segment, no pivot window ever spans one.
- **RoomToTP path (B38):**
  - Long: a confirmed valid resistance is an obstacle only if `E1 < level <= TP`.
  - Short: a confirmed valid support is an obstacle only if `TP <= level < E1`.
  - Room = distance from E1 to the nearest obstacle; the gate passes iff there is no obstacle or `RoomToTP_R >= 1` (a level exactly at TP gives exactly 1R and passes).
  - BreakoutContext is unchanged: Long = highest crossed eligible Swing High; Short = lowest crossed eligible Swing Low.
