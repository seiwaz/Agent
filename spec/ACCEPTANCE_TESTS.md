# Acceptance Tests V5

## Ambiguity-proof tests
- strict P-Gap equality rejected
- pivot equality rejected
- pivot unavailable until two right M5 bars close
- forming M5 never used
- ADX uses Wilder smoothing
- CHOP zero-range fails closed
- Regime precedence RANGE before TREND
- RangeMiddle boundaries 1/3 and 2/3 (V6.0: informational, never reject)
- Breakout requires M1 close, not wick/equality
- HTF opposite + no breakout (V6.0: informational; opposite trend alone -> NO_VALID_CONTEXT)
- no context passes => rejects
- nearest obstacle ignores already-invalidated levels
- no obstacle => infinite RoomToTP (V6.0: informational)
- RoomToTP exactly 1R (V6.0: informational, never rejects)
- liquidity median excludes current M5 and uses average middle pair for N=20
- liquidity equality at 0.5 passes
- missing volume/tradecount (V6.0: informational, never rejects)
- context rechecked before every zero-fill E1 replacement
- context invalidation with zero fill/zero setup position cancels pending E1
- E1 reprice allowed only when status=NEW, executedQty=0, setup_position_qty=0
- partial E1 fill permanently disables repricing
- full E1 fill permanently disables repricing
- partial E1 remainder stays at original E1 price
- partial E1 remainder is not cancel/replaced because Spike extends
- fill discovered during cancel race enters protection, not reject/replacement
- setup position discovered during cancel race enters protection, not replacement
- replacement requires old E1 inactive + executedQty=0 + setup_position_qty=0

## Exhaustion
- TrendAgeBars exact change point
- Microchannel equality counts
- EMA20 initialization stable across restart
- SpikeATR uses M1 Origin..LastSpike range
- RangePosition20 zero range fails closed
- opposing swing +INF when none
- threshold equalities trigger exhaustion components
- FreshBreakout uses regime strictly before breakout M1 close
- FreshBreakout age only current/prior M5 bucket
- rejection expression exact (V6.0: advisory, recorded as ADVISORY_WOULD_REJECT, never gates)

## Execution
- no retroactive fills
- only one active E1 revision
- any E1 fill freezes the E1 price for the setup
- partial E1 protects executed quantity immediately
- partial E1 keeps remainder at same original price through FillWindow
- E2 remains disabled until E1 fully fills
- exactly one TP everywhere
- no TP2 DTO/database/UI execution field
- E2 only after full E1+protection
- E2 is same-direction add-to-position, never reducing/closing
- if Tabdeal reduceOnly semantics are unverified, Live E2 is disabled
- 1% complete risk cap

## Shadow/UI
- rejected candidates persisted
- counterfactual does not affect capital/runtime
- WebUI displays every context/exhaustion raw value and reason
- frontend cannot derive gate state

## V5.1 decisions
- the P-Gap triple must satisfy the directional sequence (B04)
- no second candidate from the same directional run; a sequence break re-arms (B03)
- every sequence-continuing M1 before PullbackStart becomes LastSpikeCandle (B02)
- zero-trade minute is missing; late trades never mutate finalized candles (B05)
- TA-Lib ADX seeding; warmup after re-anchor (B06; V6.0: 30 bars)
- RangeMiddle uses raw E1 RP with exact 1/3, 2/3 (B07, B08)
- frozen breakout level (B09); obstacles are path-relative to E1/TP (B10 as reworded by B38)
- a dual pivot fails closed only inside SH1/SH2/SL1/SL2 (B11)
- RP20 includes the context bar (B12); FreshBreakout maps by M1 open_time, forming bucket age 0 (B13)
- gates are rechecked after PullbackStart with zero fill; failure cancels E1 (B14)
- a marketable E1 is never submitted; equality is not safe (B15)
- Q_final = min(risk, margin) with MARGIN_CAPPED_QTY; reject below minimums (B17)
- shadow LIQ_UNVERIFIED continues; Live requires exchange liquidation (B18)
- shadow fills need a trade strictly through; SL fills at the worse price (B19)
- unverifiable protection -> block, emergency close, flat, ERROR_HOLD (B21)
- E2 rounded toward SL; risk from the rounded E2 (B23)

## V5.2 decisions
- a violating candle seeds the next run; the P-Gap must lie fully inside the run (B29)
- a run that ends before arming → EXPIRED_UNARMED / E1_NEVER_ARMED, never EXPIRED_NO_FILL (B30)
- healthy zero-trade minute → synthetic no-trade M1, ineligible for P-Gap, Spike, Origin and sequence (B31)
- any coverage gap → DATA_GAP, no synthesis, re-anchor (B31)
- M5 inherits data-quality flags (B31)
- Q_margin_limit = available*leverage/(E1+E2) (B28)
- a dual pivot is usable as an obstacle and a breakout level; trend still fails closed (B32)

## V5.3 decisions
- BreakoutContext: highest crossed pre-Spike Swing High (Long) / lowest crossed Swing Low (Short); never selected by distance (B27)
- the level must be crossed by a strict Spike M1 close; a level confirmed after the Spike Origin is not eligible (B27, B35)
- an all-synthetic M5 bar never becomes a pivot; M5 stores synthetic_m1_count, synthetic_fraction, real_trade_count (B33)
- no synthetic candle before the first genuine post-gap trade; UNANCHORED otherwise (B34)

- EligibleBreakoutLevels frozen at setup creation; a pivot confirmed after SpikeOrigin is never used (B27 clarification)
- BreakoutLevel may become true or advance during zero-fill pre-Pullback extension; it freezes at PullbackStart or the first E1 fill (B27 clarification)

## V5.4 decisions
- all-synthetic M5 bar: never a pivot, may be a neighbour; a DATA_GAP/UNANCHORED bar is never in a pivot window (B37)
- RoomToTP obstacle iff E1 < level <= TP (Long) / TP <= level < E1 (Short); a crossed level back in the path counts again (B38)
- OpposingSwing candidates are strictly beyond the current SpikeExtreme (B38)

## V5.5 decisions
- Shadow restart with complete coverage (journal replay from the cursor) == uninterrupted run (B39)
- DATA_GAP while a position is active → AMBIGUOUS_DATA_GAP; no close or PnL fabricated; excluded from confirmed stats; scanning resumes (B39)
- no automatic close on restart (B39)
- drift probe: midpoint offset, >= 20 samples, bound 500 ms (B42)
- host sleep is detected and reported; it still produces DATA_GAP (B45)

## V5.9
- P-Gap impulse body/range = 0.60 → passes; just below → PGAP_IMPULSE_BODY_TOO_WEAK
- Doji C2 → fails; Long with bearish C2 and Short with bullish C2 → PGAP_IMPULSE_WRONG_DIRECTION
- gap/body = 0.15 → passes; just below → PGAP_GAP_TOO_SMALL_RELATIVE_TO_BODY
- gap = 2 ticks → passes; < 2 ticks → PGAP_GAP_TOO_SMALL_IN_TICKS
- a weak geometric P-Gap followed by a strong one in the same run: the strong one is the first qualifying P-Gap
- non-C2 Spike candles may have any colour
- the live forming candle follows each trade (O/H/L/C/V/N), is replaced by the final canonical candle without duplication, is corrected by revisions, is restored on reconnect, never reaches strategy code, and updates the chart in place at 1440/768/390 px

## V5.10
- trade count unknown (context bar or any reference bar), volume ≥ 0.5 × median → PASS, basis VOLUME_ONLY, no trade-count ratio invented
- trade count unknown, volume exactly 0.5 × median → PASS (equality passes)
- trade count unknown, volume < 0.5 × median → LIQUIDITY_UNKNOWN (never LOW_LIQUIDITY)
- all trade counts known → V5.8 result unchanged (LOW_LIQUIDITY only when both are low)
- fewer than 20 reference bars → LIQUIDITY_UNKNOWN

## V5.11
- net at TP computed per unit from E1, TP and the configured entry/exit rates (E1 only)
- net at TP > 0 → risk passes (other checks unchanged); net at TP = 0 → RISK_NET_TP_NOT_POSITIVE; net at TP < 0 → RISK_NET_TP_NOT_POSITIVE
- the result does not depend on wallet size or quantity rounding
- zero costs never trigger it (R > 0)
- a failing setup is rejected before any order is sent; the rule is re-evaluated on every E1 revision (a failure there cancels the zero-fill E1 as RISK_INVALIDATED_BEFORE_FILL; an extension only widens R)

## V5.12
- a strict geometric P-Gap with a weak impulse (body/range < 0.60, doji or opposite colour) or a small gap (< 15 % of body or < 2 ticks) qualifies and creates the candidate like any other
- the first P-Gap of a run spends the run whatever its measured quality; later P-Gaps of the run are log-only (NOT_FIRST_IN_RUN)
- the measured quality (values, verdicts, reason codes) is still recorded with `enforced: false` and never appears as a rejection
- equality (`Low[C3] == High[C1]`) is still not a P-Gap

## V6.0 Context (website-based)
- PASS iff NetTP > 0 AND (LevelBreak OR ChannelEdge OR HTFAligned); each of A, B, C alone passes; none -> NO_VALID_CONTEXT
- NetTP exactly 0 rejects (CONTEXT_NET_TP_NOT_POSITIVE); tiny positive passes; costs missing -> CONTEXT_NET_TP_UNKNOWN
- NetTP is evaluated and recorded while the M5 segment is still warming up
- LevelBreak: wick-only and equality do not count (strict M1 close)
- ChannelEdge: 1/3 and 2/3 inclusive on the raw (unclamped) origin RangePosition, both sides; works in TREND and RANGE regimes; flat range -> false
- HTFAligned works in any regime; opposite trend + breakout passes; opposite trend alone -> NO_VALID_CONTEXT; dual-pivot trend -> C false, A/B can still pass
- warm boundary at 30 finalized M5 bars (29 -> CONTEXT_UNKNOWN_WARMUP, 30 -> evaluated)
- former hard rejects (middle of range, RoomToTP < 1R, low/unknown liquidity, regime) never reject but are recorded
- Exhaustion never rejects at the gates level and records ADVISORY_WOULD_REJECT / ADVISORY_UNKNOWN
