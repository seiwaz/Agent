# Context Engine Specification V6.0 (2026-10-01)

Source: the SP2L strategy description (poursamadi.com/sp2l-strategy) names three "best conditions" for a Spike: it **breaks an important level**, it **starts from the edge of a channel**, or it **agrees with the higher-timeframe trend**. The owner decided (DECISIONS.md, V6.0): a setup's Context passes iff **at least one** of them holds **and** a win at TP is profitable after costs. There are no other Context gates.

## 1. Inputs and timing
Side, evaluation time, prospective or current E1, R, TP (= E1 ± R), the Origin candle, the Spike M1 candles (Origin..LastSpikeCandle), the frozen BreakoutLevel (§8 resolutions below, unchanged) and the cost rates `entry_fee_rate` / `exit_fee_rate` from runtime configuration (never hardcoded).

M5 data: the latest finalized M5 bar whose close_time <= evaluation time and which is the head of the current segment (§1 of V5; a stale or broken series has no context bar). The forming M5 bar is never used. Context is evaluated before the initial E1 submit and before every zero-fill E1 replacement; an invalidation with zero fill cancels the pending E1 (`CONTEXT_INVALIDATED_BEFORE_FILL`). After any fill Context never closes or moves the position (unchanged).

## 2. The rule
`PASS iff D AND (A OR B OR C)`. Every condition is evaluated every time and recorded.

**D — NetTP** (evaluated FIRST, needs no M5 data, also during warmup):
`net_tp_per_unit = |TP − E1| − E1·entry_fee_rate − TP·exit_fee_rate`; require `> 0` (zero rejects). E1 only (E2 is never assumed). Same formula as Risk V5.11, which stays as a second layer. Costs missing → `CONTEXT_NET_TP_UNKNOWN` (fail closed). Not positive → `CONTEXT_NET_TP_NOT_POSITIVE`.

**A — LevelBreak** (= V5 BreakoutContext, machinery unchanged): a finalized M1 candle of the active Spike closes strictly beyond the frozen BreakoutLevel — a confirmed M5 swing level that existed (confirmed and unbroken) at Spike origin; Long uses Swing Highs, Short Swing Lows (selection, freezing and advancement: resolutions below). Wick-only and equality do not count.

**B — ChannelEdge** (= V5 RangeEdgeOrigin without the regime condition): range = the last 14 finalized M5 bars including the context bar; `RP = (ref − RangeLow) / (RangeHigh − RangeLow)` raw (never clamped), ref = OriginLow (Long) / OriginHigh (Short). Long true iff `RP ≤ 1/3`, Short true iff `RP ≥ 2/3` (exact rationals, inclusive). Any regime. RangeHigh == RangeLow → B false (not a global reject).

**C — HTFAligned** (= V5 HTFAlignment without the regime condition): the M5 trend (§3 below) is BULL for Long / BEAR for Short. Any regime. NEUTRAL, NEUTRAL_INSUFFICIENT_STRUCTURE or INVALID_DUAL_PIVOT → C false (not a global reject).

**Warmup:** A, B and C need a warm M5 segment of at least `shadow.warmup_m5_bars` finalized bars (default **30**; was 150). Unwarm → `CONTEXT_UNKNOWN_WARMUP` (fail closed); D is still computed and recorded.

**Reasons** (in this order): `CONTEXT_NET_TP_NOT_POSITIVE` or `CONTEXT_NET_TP_UNKNOWN`, `CONTEXT_UNKNOWN_WARMUP`, `NO_VALID_CONTEXT`. Status: PASS with no reasons; UNKNOWN if the only reasons are NET_TP_UNKNOWN / WARMUP; otherwise REJECT.

## 3. M5 pivots and trend (unchanged)
Swing High at i: `High[i] > High[i−1], High[i−2], High[i+1], High[i+2]`; Swing Low symmetric; strict inequalities; confirmable only when bars i+1 and i+2 are finalized. Trend from the last two confirmed highs SH1 < SH2 (older, newer) and lows SL1, SL2: BULL iff `SH2 > SH1 AND SL2 > SL1`; BEAR iff `SH2 < SH1 AND SL2 < SL1`; otherwise NEUTRAL; fewer than two of either → NEUTRAL_INSUFFICIENT_STRUCTURE. A dual pivot among SH1/SH2/SL1/SL2 → INVALID_DUAL_PIVOT (C false).

## 4. Informational measures (recorded, never a reason, never affect the result)
Kept so the database, UI and analytics stay comparable with V5:
- Regime from Wilder ADX14 and CHOP14 (RANGE iff CHOP ≥ 61.8 AND ADX < 20; TREND iff CHOP ≤ 38.2 OR ADX ≥ 25; else TRANSITION).
- RangeMiddle: regime RANGE and E1's raw RangePosition in [1/3, 2/3].
- HTF opposite without breakout.
- RoomToTP: nearest confirmed, M5-unbroken opposing swing level in the E1→TP path, as R (none = +INF).
- Liquidity: context-bar volume and trade count vs the median of the previous 20 bars (V5.10 basis).

Snapshot fields: V5 names are kept (`breakout_context` = A, `range_edge_origin` = B, `htf_alignment` = C) with the aliases `level_break`, `channel_edge`, `htf_aligned`; new fields `tp`, `net_tp_per_unit`, `net_tp_positive` are stored in the snapshot's exact JSON.

## History
V5 (rev 5.5) required, in addition, no RangeMiddle reject, no HTF-opposite-without-breakout, RoomToTP ≥ 1R, Liquidity, valid data and a 150-bar warmup, and treated Exhaustion as a gate. The resolutions below define the breakout levels used by A and stay in force.

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
