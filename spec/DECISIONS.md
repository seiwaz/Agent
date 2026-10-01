# V5 Frozen Decisions

## Core SP2L
- P-Gap strict, closed-candle only.
- Long directional continuation: `Low[n] >= Low[n-1]`.
- Short directional continuation: `High[n] <= High[n-1]`.
- First valid directional P-Gap confirms Spike.
- Spike Origin determined by deterministic backtracking.
- E1 exists before Pullback.
- Long E1 = Low(latest closed Spike candle).
- Short E1 = High(latest closed Spike candle).
- Safe cancel/reconcile/reprice on a valid extension is allowed only when `order_status=NEW`, `executedQty=0`, and no position for this setup exists.
- Any partial/full E1 fill freezes E1 permanently for that setup; no later E1 reprice is allowed.
- For partial E1, the unfilled remainder stays at the original E1 price until full fill, FillWindow expiry, or a terminal position event.
- Any cancel/reprice race that discovers a fill or setup position abandons replacement and enters protection flow.
- No retroactive fills.
- PullbackStart = first live touch/cross of active E1.
- 4-candle fill window starts with PullbackStart candle.
- Long SL = OriginLow - 1 tick; Short SL = OriginHigh + 1 tick.
- Exactly one TP = 1R.
- E2 midpoint, equal Qty, same SL and same single TP.
- E2 is a same-direction add-to-position order; it must never reduce/close the position. Any Tabdeal `reduceOnly` behavior must be runtime-validated before Live E2.

## Context hard gates
- M5 pivots = 2-left / 2-right, confirmed only after both right candles close.
- Pivot ties are NOT pivots: center must be strictly greater/less than all four neighbors.
- M5 trend uses last two confirmed swing highs and last two confirmed swing lows.
- RANGE: `CHOP14 >= 61.8 AND ADX14 < 20`.
- TREND: `CHOP14 <= 38.2 OR ADX14 >= 25`.
- Else TRANSITION.
- Range bounds = HH/LL of last 14 finalized M5 bars.
- Range middle inclusive: `1/3 <= RangePosition <= 2/3` => reject when Regime=RANGE.
- Require at least one: BreakoutContext OR HTFAlignment OR RangeEdgeOrigin.
- HTF-opposite direction without BreakoutContext => reject.
- RoomToTP must be >=1R against nearest confirmed, still-valid M5 swing obstacle.
- LowLiquidity reject if last finalized M5 Volume < 0.5×median(prev20) AND TradeCount < 0.5×median(prev20).
- Context re-evaluates before every E1 placement/replacement. If pending E1 has zero fill and context becomes invalid, cancel/reconcile E1 and reject candidate.

## Exhaustion hard gate
- `LateTrend = TrendAgeBars >=20 OR MicrochannelLen >=8`.
- `ExtremeStretch = StretchATR >=2.0`.
- `ClimacticSpike = SpikeATR >=1.5`.
- `AtOuterEdge = RangePosition20 >=0.90 Long / <=0.10 Short OR OpposingSwingDistance <=0.5*ATR14_M5`.
- Reject when `LateTrend AND ExtremeStretch AND (ClimacticSpike OR AtOuterEdge)` unless FreshBreakoutException.
- FreshBreakoutException requires a confirmed M5 swing break, previous regime RANGE/TRANSITION, and breakout first began within last 2 finalized M5 bars.

## Shadow/UI transparency
Every candidate, including rejected candidates, is stored with all Context and Exhaustion values, gate results and reason codes. Counterfactual rejected-candidate outcomes are simulated offline/shadow-only and never affect runtime decisions.

## V5.1 — BLOCKER decisions (2026-09-26)
- **B01:** the files as found on 2026-09-26 are authoritative. The manifest is regenerated and the rules hash pinned.
- **B02:** every finalized M1 that continues the directional sequence before PullbackStart becomes the new LastSpikeCandle.
- **B03:** no new candidate from the same ongoing sequence. A new setup requires a sequence break first.
- **B04:** the P-Gap triple itself must satisfy the directional sequence. There is no separate minimum-R filter.
- **B05:** market-data construction:
  - exchange timestamps, UTC-epoch alignment;
  - a zero-trade minute is missing;
  - 2 s finalization grace;
  - late trades are logged but never mutate finalized candles;
  - TradeCount = raw trade count.
- **B06:** indicator anchoring:
  - TA-Lib is the canonical ADX implementation;
  - fixed anchor per symbol;
  - after a data gap, re-anchor and require 150 finalized M5 bars before Context is valid;
  - zero denominators are UNKNOWN (fail closed).
- **B06+:** TA-Lib ADX seeding. Implementations are never switched at runtime.
- **B07:** logic uses the raw E1 RangePosition. Clamping is display-only.
- **B08:** exact rationals 1/3 and 2/3 are used internally.
- **B09:** freeze the nearest confirmed unbroken M5 level at Spike confirmation for BreakoutContext.
- **B10:** once a finalized M1 closes strictly beyond the frozen BreakoutContext level in the trade direction, that level is broken for this setup's RoomToTP and OpposingSwing. There is no wait for an M5 close. The global M5 pivot record keeps its M5-based `broken_at`. *(Reworded by V5.4 B38: there is no setup-local removal; obstacles are path-relative to the current E1 and TP.)*
- **B11:** a dual pivot fails closed only if it participates in the SH1/SH2/SL1/SL2 of the current evaluation.
- **B12:** RP20 includes the current finalized context M5 bar.
- **B13:** map by M1 open_time. The current forming M5 bucket has breakout age 0.
- **B14:** after PullbackStart, with exchange-confirmed executedQty == 0 and setup_position == 0, Context and Exhaustion are rechecked on every M1 close. Repricing stays frozen. A gate failure cancels the still-unfilled E1.
- **B15:** never submit a marketable E1.
  - Long only when last trade > E1; Short only when last trade < E1. Equality is not sufficient.
  - If unsafe, no order: wait for the next finalized M1 close and fully re-evaluate.
  - Never convert to a market or taker entry. No retroactive fill.
- **B16:** wallet and costs:
  - Live wallet_balance = wallet balance excluding unrealized PnL.
  - Shadow = initial + realized PnL − fees.
  - Fees and the SL slippage allowance are runtime configuration with validation evidence, never hardcoded.
- **B17:** margin-capped quantity:
  - `Q_final = min(Q_risk_limit, Q_margin_limit)`, keeping E1/E2 quantities equal.
  - Round down to the step, then recheck minQty, minNotional, cost-adjusted risk and liquidation safety.
  - Expose `MARGIN_CAPPED_QTY`.
  - Reject only if below exchange minimums or unsafe.
  - Never silently resize after E1 has filled.
- **B18:** Shadow records LIQ_UNVERIFIED and continues. Live requires verified exchange liquidation data.
- **B19:** Shadow fill model:
  - a touch starts PullbackStart but is not a fill;
  - a resting limit fills fully once a trade prints strictly through it;
  - SL fills at the worse of the SL price and the trigger trade;
  - no fabricated partial fills.
- **B20:** the counterfactual continues canonical base-SP2L causally, reports in R, and stays isolated.
- **B21:** after any execution, protection is mandatory. Retries are bounded, explicit, configured and tested before Live. If protection is still unverified:
  1. block E2 and new orders;
  2. emergency-close via the verified position-close mechanism;
  3. reconcile until flat;
  4. ERROR_HOLD and alert.
- **B22:** UNRESOLVED 1–5 block Live only and are closed by runtime probes.
- **B23:** round E2 toward SL, then recompute the actual E2 risk and total setup risk. Never assume 0.5R.
- **B24:** strategy SL/TP use `CONTRACT_PRICE`, runtime-validated before Live.
- **B25:** an unrecoverable WS gap marks the data incomplete. No synthesized candles. Candidate creation stops, origin continuity breaks, and indicators re-anchor with a fresh warmup.
- **B26:** precision-derived tick/step are provisional for Shadow and development only. Live needs probe-established increments and minimums. Unknown minQty/minNotional means Live is disabled.

## V5.2 — decisions B27–B32 (2026-09-26)
- **B27:** RoomToTP's nearest level is measured from the prospective/current E1 (Long: nearest confirmed valid resistance above E1; Short: nearest support below E1). Exhaustion OpposingSwingDistanceATR is a separate metric, measured from the current SpikeExtreme to the nearest opposing confirmed M5 swing. *(The reference point for freezing the BreakoutContext level stays open.)*
- **B28:** Shadow/provisional linear-USDT margin:
  - `required_margin(Q) = Q*E1/leverage + Q*E2/leverage`
  - `Q_margin_limit = available_margin*leverage/(E1+E2)`
  - `Q_final = min(Q_risk_limit, Q_margin_limit)`, rounded down

  Margin is reserved for both legs. Live does not rely on this until runtime-validated.
- **B29:** a Long run starts after the previous Low-decrease violation, continues while `Low[n] >= Low[n-1]` and ends at the first finalized M1 with `Low[n] < Low[n-1]`. Short is symmetric. The violating candle seeds the next run. The qualifying P-Gap must lie fully inside the run.
- **B30:** a run that ends before E1 was ever armed → `EXPIRED_UNARMED`, reason `E1_NEVER_ARMED`. `EXPIRED_NO_FILL` is only for an active E1 with zero fill.
- **B31:** zero-trade intervals are separated from transport gaps.
  - **Healthy full-minute coverage with zero trades:** a synthetic no-trade M1 (`O=H=L=C=previous close`, volume 0, trade_count 0, `synthetic_no_trade=true`, `data_gap=false`). It may maintain indicator/time continuity but is ineligible for P-Gap, Spike confirmation, SpikeOrigin and sequence continuation.
  - **Any incomplete coverage:** `DATA_GAP`. Nothing is synthesized; continuity breaks, indicators re-anchor, warmup is required and candidates are blocked.
  - M5 inherits the M1 data-quality flags.
- **B32:** a confirmed dual pivot may supply its High (resistance / Long breakout level) and its Low (support / Short breakout level) for obstacles and BreakoutContext. B11 still applies to trend classification.

## V5.3 — decisions B27 (revised), B33, B34 (2026-09-26)
- **B27:** the "nearest measured from X" rule is removed. At Spike confirmation, BreakoutContext is determined from confirmed M5 swing levels that existed before the Spike.
  - Long: the confirmed, setup-eligible Swing Highs with at least one finalized Spike M1 close strictly above them. If any exist, BreakoutContext is true and the level is the **highest** crossed one; otherwise false.
  - Short: symmetric, with the **lowest** crossed Swing Low.
  - The level is frozen per setup. It is never selected by distance from Origin, E1 or last trade.
- **B33:** M5 bars with some synthetic minutes stay valid for indicators, range, liquidity and pivots, provided the bar has at least one real trade. A bar whose five minutes are all synthetic (healthy coverage) is allowed for indicators, range and liquidity, but pivot generation and structural Swing High/Low creation are forbidden. Every M5 bar stores `synthetic_m1_count`, `synthetic_fraction` and `real_trade_count`.
- **B34:** a pre-gap close is never carried across a DATA_GAP.
  - After coverage returns, the first genuine post-gap trade is the new anchor.
  - Later fully covered zero-trade minutes are synthesized from the last valid post-gap price.
  - Until then, zero-trade minutes stay DATA_GAP/UNANCHORED.
  - The gap still breaks continuity and forces re-anchoring and warmup.

- **B27 clarification:**
  - **Setup-eligible level:** a confirmed M5 pivot level (Long: Swing High; Short: Swing Low; dual-pivot High/Low per B32), confirmed no later than `SpikeOriginCandle.open_time` and unbroken at that time. Pivots confirmed after the SpikeOrigin are never eligible.
  - **Freezing the set:** `EligibleBreakoutLevels` is frozen at setup creation.
  - **Updating the level:** while there is no PullbackStart, E1 has zero executed quantity and the same Spike is extending, each new finalized M1 is checked against the set. The level may become true and may advance to a farther crossed level (Long: highest crossed Swing High; Short: lowest crossed Swing Low).
  - **Permanent freeze:** at PullbackStart or the first E1 fill, whichever comes first.

## V5.4 — decisions B37, B38 (2026-09-26)
- **B37:** a fully synthetic, healthy/anchored M5 bar cannot become a pivot but may be a 2L/2R neighbour. A DATA_GAP or UNANCHORED M5 bar can be neither a pivot nor a neighbour, and any pivot window containing one is UNKNOWN / not confirmed.
- **B38:** the BreakoutLevel is not specially removed, and levels crossed by the Spike are not globally removed. BreakoutContext stays: Long = highest crossed eligible Swing High; Short = lowest crossed eligible Swing Low.
  - **RoomToTP:** obstacles are path-relative to the current prospective E1. Long: `E1 < level <= TP`; Short: `TP <= level < E1`. A level crossed earlier but back in the E1→TP path counts again; a level behind the current E1 does not.
  - **OpposingSwingDistance:** Long considers only confirmed opposing levels strictly above the current SpikeExtreme; Short only those strictly below. Crossed levels behind the SpikeExtreme are not candidates.
  - **B10** is reworded to match this rule.

## V5.5 — decisions B39–B45 (2026-09-26)
- **B39:** a Shadow restart never auto-closes a simulated position.
  - It restores the last engine/setup state and cursor, then replays stored raw market events after the cursor in exact causal order. With complete coverage the result is identical to an uninterrupted run.
  - A DATA_GAP overlapping any period where an E1/E2/order/position was active finalizes the setup as `AMBIGUOUS_DATA_GAP`, with no fabricated close or PnL, excluded from confirmed performance statistics. The symbol then returns to normal warmup/scanning.
  - ERROR_HOLD is for runtime/system uncertainty needing intervention; missing Shadow market data is `AMBIGUOUS_DATA_GAP`.
- **B40:** no API leverage write. The user sets BTC_USDT to 10x manually; the read-only check is rerun, and Live stays blocked unless the API reads exactly 10x.
- **B41:** Cross margin stays a Live blocker. It is never inferred while flat; it will be verified on an explicitly approved minimum-size position. Until then: `CROSS_MARGIN_UNVERIFIED`.
- **B42:** clock drift stays a Live blocker until corrected. The probe uses the request midpoint and at least 20 samples; the Live target is worst absolute offset <= 500 ms.
- **B44:** no validation order while available balance is 0. Shadow uses its own virtual wallet. The user provides dedicated collateral before any order test; minQty/minNotional are never guessed; no order test without explicit approval.
- **B45:** development keeps the current Mac behavior (sleep/wake produces DATA_GAP, nothing synthesized). Production needs an always-on host or no system sleep. Host-sleep and long-gap events are exposed in diagnostics and the WebUI.

## V5.6 — market-data integrity (2026-09-27). Data contract only; no strategy rule changed.
Owner brief: a WebSocket interruption must not needlessly invalidate 12.5 h of indicator history; repair the data, never weaken the rules.
- **Transport vs coverage:** a socket reconnect while merged A/B coverage stays proven is no gap at all.
- **GAP_PENDING_REPAIR:** a real merged gap no longer than `max_gap_s` holds its minutes. After reconnect, missing trades are fetched from Tabdeal's public `special-margin/recent-trades` (last 50 trades) and accepted only if EXACT:
  - the window reaches back before the gap;
  - the measured time model (record time − WS time in [-100, +500] ms) holds for every live trade in the window;
  - no recovered trade can fall in two minutes;
  - every affected minute's open and close is unambiguous.
- **Repaired trades** go through the same M1/M5 code as live trades (`REPAIRED_TABDEAL` lineage). Continuity is kept, with no re-anchor and no warmup.
- **Repaired history is never a decision point:**
  - no P-Gap is promoted on a repaired minute;
  - a setup or counterfactual still alive when repaired history arrives becomes `AMBIGUOUS_DATA_GAP`;
  - exposure during the gap stays B39.
- **UNRECOVERED:** anything not exact follows the V5.5 DATA_GAP rules unchanged (re-anchor, 150-bar warmup). The warmup length is unchanged.
- **Candles:** changed only through recorded revisions (previous version kept), never deleted. M5 is always rebuilt from canonical M1.
- **Trade identity:** `sequence` is not a trade id; one sequence carried up to 23 fills. Dedup key = (sequence, exchange time, price, amount) + occurrence. Conflicting payloads are logged with raw payload and never dropped.
- **Pre-fix data:** runs before the fingerprint fix are marked `CONFLICTED` via revisions. The multi-fills those runs dropped but logged were restored.
- **Open (see BLOCKERS B47/B48):**
  - Tabdeal's chart-history bars are not canonical: continuous bars, record-time bucketing, no trade count.
  - Tabdeal's WS broadcast omits ~0.6 % of trades even under proven coverage.

## V5.7 — continuous REST reconciliation and history-based warmup (2026-09-27). Data contract only; no strategy rule changed.
- **B48 becomes mandatory.** Over 2.5 h, trades that only REST listed were 0.52 % of canonical trades, and they changed 9.5 % of M1 bars (all volume/TradeCount, 1 High). The Liquidity gate uses both.
- **Sources:** WS A, WS B and Tabdeal's public `special-margin/recent-trades` (newest 50 trades, no paging, no time range; measured span 42.6 s min, 50 s p1, 105 s p50). It is polled continuously at a third of the last window span (3–15 s), so windows always overlap.
- **Identity (measured):** a REST trade equals a WS trade iff price and amount are equal and record time − stream time ∈ [−100, +500] ms, taking the nearest unmatched WS trade. Otherwise it is a canonical REST-only trade. Every trade keeps its sources (A,B,REST) and raw payloads.
- **M1 lifecycle:** a minute becomes FINAL_CANONICAL only after REST coverage spans it (LIVE_RECONCILED / REST_REPAIRED). If REST is unavailable it waits up to the reconcile deadline, then finalizes as LIVE_WS_ONLY. A zero-trade minute is synthetic only under REST coverage.
- **Gaps:** a WS coverage gap spanned by REST coverage is REST_REPAIRED (no reset); otherwise it is UNRECOVERED and the V5.5 rules apply. The maximum exactly repairable host outage is bounded by the shortest recent REST window (exposed in Diagnostics).
- **Warmup = trusted canonical history in the DB, not process uptime.**
  - A restarted collector resumes the series at the last canonical minute (the restart is an ordinary, REST-repairable gap) and completes the open M5 bucket from stored minutes.
  - Shadow primes from stored contiguous M5.
  - API, collector and process restarts never reset warmup by themselves.
- **Late REST trades** for an already-final minute become an audited candle revision (with M5 rebuild). Past Shadow/Live decisions are never rewritten.

## V5.8 — three-tier recovery, execution certainty vs indicator continuity, exact 10x leverage (2026-09-28). No SP2L, Context, Exhaustion, E1/E2 or risk threshold changed; the 150-M5 warmup and DATA_GAP safety are unchanged.
- **Forensics of the reset that led here:** 2026-09-28 03:32:39.8–03:33:36.2 UTC (56.4 s). Both WS A and WS B lost frames at the same instant; reconnect handshakes and REST polls timed out (a host-network outage). After recovery the first 50-trade recent-trades window no longer reached 03:32:39 (REST_WINDOW_DID_NOT_REACH_GAP_START), so M1 03:32 became DATA_GAP, M5 03:30 was never formed and the Context segment re-anchored at 03:35.
- **Three tiers:**
  1. dual WS;
  2. recent-trades exact repair (EXACT_RAW_REPAIR → RECENT_TRADES_REPAIRED, the V5.8 name of REST_REPAIRED);
  3. validated Tabdeal chart history (`special-margin/plots/history`, resolution 1) for what tier 2 cannot reach: long outages, restarts and startup bootstrap (CANDLE_HISTORY_REPAIR → TABDEAL_HISTORY_REPAIRED).
  - UNRECOVERED only if all three fail. No other exchange is ever used.
- **History acceptance, per repair, in the same response:**
  - every gap minute is present and final;
  - at least 5 overlapping canonical minutes are compared under the TradingView continuity model, with ≥ 80 % exact on H/L/C/V and no H/L relative difference > 0.1 %;
  - otherwise the repair fails closed.
  - Offline validation: 762 reconciled minutes, 96.85 % exact; 20 of the 24 differences are close order within the minute (same trades); 4 are small unexplained differences (max 0.4 USDT on L, volume ≤ 0.0047 BTC).
- **Execution certainty is separate from indicator continuity:**
  - A GAP event no longer decides anything by itself. Trades are buffered from the GAP on and released in exchange-time order at each minute close.
  - EXACT_RAW_REPAIR → causal replay.
  - CANDLE_HISTORY_REPAIR → the exposed setup is AMBIGUOUS_DATA_GAP. Counterfactuals with exposure become AMBIGUOUS. Indicators continue with no re-anchor.
  - DATA_GAP → V5.5 rules.
  - No retroactive setups on any repaired minute.
- **Liquidity:**
  - History bars have no trade count. It is UNKNOWN (NULL), never invented.
  - PRICE_CONTEXT_READY (150 trusted price bars; history bars count) is separate from LIQUIDITY_CONTEXT_READY (the context bar and the 20 before it have a known trade count).
  - While unknown, the gate is LIQUIDITY_UNKNOWN (CTX-18). Formula and threshold are unchanged.
  - Liquidity is valid again 21 M5 bars (105 min) after the last history-repaired bucket.
- **Bootstrap / restart invariant:**
  - The collector resumes after the last canonical minute, however old (up to 7 days). The recent part is repaired from recent-trades, the rest from validated history.
  - With ≥ 150 trusted M5 bars, Context is ready immediately.
  - Warmup never depends on process id, run id, restart count, socket generation, WS session or boot time.
- **Leverage:**
  - The strategy leverage is exactly 10x everywhere (Shadow, risk, margin sizing `Q_margin_limit = available_margin*10/(E1+E2)`, reports, liquidation modelling, UI, counterfactuals). The engine refuses any other value.
  - The account leverage is only read (read-only GET). Live requires exchange leverage == 10, otherwise the Live blocker is LEVERAGE_MISMATCH.
  - There is no automatic leverage change and no authenticated write.

## V5.9 — Core P-Gap qualification + live forming candle (2026-09-28)
- **Two new Core filters on every geometric P-Gap:**
  1. The impulse candle C2 is directional (bullish for Long, bearish for Short) with body/range ≥ 0.60.
  2. The gap is strong: gap ≥ 0.15 × body(C2) and gap ≥ 2 ticks.
  - Exact rational comparison; equality passes. The thresholds are frozen: no tuning, no ML, and analytics never adjust them.
- **Run and colour semantics:**
  - A geometric P-Gap that fails either filter is logged with all its measurements and a reason code. It is not a qualifying P-Gap and does not spend the run: the first QUALIFYING P-Gap of the run may still create the candidate (B03 otherwise unchanged).
  - The colour requirement applies to C2 only (SEQ-03 stays true for runs and all other Spike candles).
- **Unchanged:** sequence, run start/end, Origin, LastSpikeCandle extension, PullbackStart, E1/E2/SL/TP, FillWindow, Context, Exhaustion, Risk, Shadow fills, counterfactuals, warmup, reconciliation, repair, costs, leverage and Live blockers.
- **History is never reinterpreted:**
  - Candidates and P-Gaps recorded before V5.9 keep their spec_version, and their P-Gap quality stays "not measured".
  - A new Shadow session starts under 5.9.
  - The comparison in docs/pgap_quality_report.json is labelled as a replay under the new spec.
- **Live forming M1 (UI and observability only, zero trading authority):**
  - The collector builds the current minute from canonical trades as they arrive and publishes it (Postgres NOTIFY). The API streams it over Server-Sent Events.
  - The final canonical M1, and any later audited revision, replaces it (identity = symbol + open_time).
  - No strategy, engine, indicator, counterfactual or execution code reads it.

## V5.10 — Liquidity decided by volume when the trade count is unknown (2026-09-29)
- **Problem (measured, docs: Context ablation study):** Tabdeal chart-history repair restores OHLCV but never a trade count, and Tabdeal offers no deeper trade history (recent-trades = last 50 trades, about 100 s; no paging). Under V5.8 a single repaired M5 bucket made the Liquidity gate LIQUIDITY_UNKNOWN for the next 21 M5 bars (105 min). Over 28 Sep 00:00 – 29 Sep 07:36 UTC, this rejected 81 of 220 candidates (37 %). Every one had volume ≥ 0.5 × median, and there was no LOW_LIQUIDITY at all.
- **Rule (three-valued evaluation of the unchanged conjunction):**
  - `LOW_LIQUIDITY` iff `Volume < 0.5×median AND TradeCount < 0.5×median` (unchanged).
  - If any TradeCount in L or its 20 reference bars is unknown:
    - `Volume ≥ 0.5×median` → the conjunction is false → PASS, basis `VOLUME_ONLY`;
    - `Volume < 0.5×median` → the result depends on the unknown value → `LIQUIDITY_UNKNOWN` (fail closed).
  - Fewer than 20 reference bars → `LIQUIDITY_UNKNOWN` (unchanged).
- **Unchanged:** lookback 20, median rule, both 0.5 thresholds, equality passes, no invented trade count, LOW_LIQUIDITY only with both values known, the 150-bar price warmup, DATA_GAP handling, repair tiers, every other Context gate, Exhaustion, Risk, E1/E2, costs, leverage and Live blockers.
- LIQUIDITY_CONTEXT_READY keeps its meaning (trade count known for L and the 20 bars before it). While it is false, Liquidity is decided by volume alone as above.
- **History is never reinterpreted:** candidates recorded before V5.10 keep their spec_version and their stored Liquidity result. A new Shadow session starts under 5.10.

## V5.11 — Net profit at TP must be positive (2026-09-29)
- **Problem (measured):** with a single TP at 1R, a win pays R while a round trip costs about 17.5 bps. The median R of fillable setups was 12.7 bps. All 4 Shadow trades that reached TP (5.8/5.9 sessions) closed with a negative net, and 37 of 45 wins in the 31.6-hour study lost money after fees.
- **Rule:** the Risk engine rejects a setup with `RISK_NET_TP_NOT_POSITIVE` unless `abs(TP − E1) − E1*entry_fee_rate − TP*exit_fee_rate > 0` (E1 alone, exited at TP, per unit; zero rejects). It is part of every Risk evaluation, so it applies before the first E1 submit and on every E1 revision.
- **Unchanged:** Spike, P-Gap, Origin, extension, E1/E2/SL/TP geometry, Context, Exhaustion, the 1 % risk budget and sizing, margin, liquidation, fees, leverage, counterfactuals (no-gate twins still simulate every candidate) and Live blockers.
- **Known limit:** a positive win does not make the expectancy positive. At R just above the floor a win nets little while a loss costs about 2R. The rule removes certain-loss wins; it does not make the strategy profitable.
- **History is never reinterpreted:** records before V5.11 keep their spec_version. A new Shadow session starts under 5.11.

## V5.12 — P-Gap quality filter removed (2026-09-30)
- **Change:** the V5.9 Core P-Gap qualification (strong directional impulse C2 with body/range ≥ 0.60; gap ≥ 0.15 × body and ≥ 2 ticks) is no longer a rule. Every strict geometric P-Gap inside a directional run qualifies (`Low[C3] > High[C1]` / `High[C3] < Low[C1]`, equality invalid), and the first one of a run confirms the Spike.
- **Still recorded:** the same values, verdicts and reason codes are measured and stored with every P-Gap (`enforced: false`) and shown in Diagnostics as "Measured only". They reject nothing and change nothing.
- **Unchanged:** the directional sequence, run semantics, B03/B04, Origin, extension, E1/E2/SL/TP, Context, Exhaustion, Risk (including V5.11 net-at-TP), the V5.10 Liquidity rule, Shadow fills, costs, leverage, warmup/repair and Live blockers. The colour of C2 is again not required (SEQ-03 applies to every candle).
- **Expected effect:** more Spike candidates than under V5.9 (weak-impulse and small-gap P-Gaps now qualify). Context, Exhaustion and the net-at-TP rule still filter them before any E1.
- **History is never reinterpreted:** P-Gaps and candidates recorded before V5.12 keep their spec_version and their stored verdicts. A new Shadow session starts under 5.12.
