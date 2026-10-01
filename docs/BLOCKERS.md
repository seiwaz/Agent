# SP2L — BLOCKER register

## Resolved (V5.1, 2026-09-26)
B01–B26 are decided. The authoritative record is `spec/DECISIONS.md`, section V5.1, and the machine-readable
form is `spec/SP2L_RULES.yaml` (version 5.1).

| ID | Decision (short) | Where enforced |
|---|---|---|
| B01 | current files authoritative; manifest regenerated; rules hash pinned | `config/spec.lock`, `spec/MANIFEST_SHA256.json` |
| B02 | every sequence-continuing M1 before PullbackStart = new LastSpike | `strategy/spike.py` |
| B03 | one candidate per directional run; re-arm needs a sequence break | `strategy/spike.py` |
| B04 | the P-Gap triple must satisfy SEQ; no min-R filter | `strategy/spike.py` |
| B05 | exchange ts, UTC epoch, zero-trade M1 = missing, 2 s grace, late trades log-only, raw trade count | `marketdata/m1_builder.py` |
| B06/B06+ | TA-Lib ADX seeding (only implementation); fixed anchor; 150-bar warmup after re-anchor; zero denominators UNKNOWN | `indicators/wilder.py`, `indicators/m5_state.py` |
| B07/B08 | raw E1 RP for RangeMiddle; exact 1/3, 2/3 | `strategy/context/engine.py` |
| B09/B10 | frozen breakout level; an M1 close beyond it removes it as an obstacle for the setup | `strategy/context/levels.py` |
| B11 | dual pivot fails closed only within SH1/SH2/SL1/SL2 | `indicators/m5_state.py` |
| B12/B13 | RP20 includes the context bar; FreshBreakout maps by M1 open_time, forming bucket age 0 | `strategy/exhaustion/engine.py` |
| B14 | gate recheck after PullbackStart with zero fill; failure cancels E1 | `engine/setup_machine.py` (`_close_pullback`) |
| B15 | never submit a marketable E1; strict last-trade guard; else wait for the next close | `strategy/submit_guard.py` |
| B16/B17/B18/B23 | wallet definitions; configured costs; Q = min(risk, margin); LIQ_UNVERIFIED in Shadow; E2 toward SL | `strategy/risk/engine.py`, `strategy/levels.py` |
| B19/B20 | Shadow fill model; causal counterfactual in R | `execution/shadow_broker.py`, `counterfactual/simulator.py` |
| B21 | bounded retries → block → emergency close → flat → ERROR_HOLD | `engine/setup_machine.py` (`_protect`); Live adapter + probes pending |
| B22/B24/B26 | Live-only probes; CONTRACT_PRICE; filters provisional in Shadow | `validation/`, `config/runtime.yaml` |
| B25 | gaps mark data incomplete; no synthesis; stop candidates; re-anchor | `marketdata/`, `indicators/m5_state.py` |

## Resolved (V5.2, 2026-09-26)
| ID | Decision (short) | Where enforced |
|---|---|---|
| B27 (part) | RoomToTP measured from the prospective/current E1; OpposingSwingDistanceATR from SpikeExtreme | `strategy/context/engine.py`, `strategy/exhaustion/engine.py` |
| B28 | Shadow margin `Q_margin_limit = available*leverage/(E1+E2)`, both legs reserved; Live needs a validated margin model | `strategy/risk/engine.py` |
| B29 | run = from the violation (which seeds it) while SEQ holds; the P-Gap must lie fully inside | `strategy/spike.py` |
| B30 | a run that ends before E1 is armed → `EXPIRED_UNARMED` / `E1_NEVER_ARMED` | `engine/setup_machine.py`; DB state list |
| B31 | healthy zero-trade minute → synthetic no-trade M1 (ineligible for P-Gap, Spike, Origin, SEQ); any coverage gap → `DATA_GAP`; M5 inherits flags | `marketdata/m1_builder.py`, `m5_aggregator.py` |
| B32 | a dual pivot may supply the Long (High) and Short (Low) breakout level and obstacles; B11 still applies to trend | `strategy/context/levels.py` |

## Resolved (V5.3, 2026-09-26)
| ID | Decision (short) | Where enforced |
|---|---|---|
| B27 (revised + clarification) | EligibleBreakoutLevels frozen at setup creation (confirmed no later than SpikeOrigin open, unbroken then; dual per B32). BreakoutLevel = highest crossed Swing High (Long) / lowest crossed Swing Low (Short) by a strict Spike M1 close; it may become true or advance during zero-fill pre-Pullback extension and freezes at PullbackStart or the first E1 fill. No distance-based selection. | `strategy/context/levels.py`, `engine/setup_machine.py` |
| B33 | M5 stores synthetic_m1_count, synthetic_fraction, real_trade_count. An all-synthetic bar is valid for indicators, range and liquidity but never becomes a pivot. | `marketdata/m5_aggregator.py`, `indicators/m5_state.py`, `candles_5m` |
| B34 | no pre-gap price across a gap; the anchor comes from the first genuine post-gap trade; otherwise `UNANCHORED` (treated as DATA_GAP) | `marketdata/m1_builder.py` |

## Resolved (V5.4, 2026-09-26)
| ID | Decision (short) | Where enforced |
|---|---|---|
| B37 | all-synthetic healthy M5 bar: never a pivot, may be a 2L/2R neighbour. DATA_GAP/UNANCHORED bar: neither; a window containing one is not confirmed (a gap ends the segment, so no window spans it) | `indicators/m5_state.py` |
| B38 (+ B10 reworded) | no setup-local removal of levels. RoomToTP obstacle iff `E1 < level <= TP` (Long) / `TP <= level < E1` (Short); OpposingSwing candidates strictly beyond the current SpikeExtreme | `strategy/context/levels.py` (`room_obstacles`), context and exhaustion engines |

## Decided (V5.5, 2026-09-26)
| ID | Decision (short) | Where enforced |
|---|---|---|
| B39 | Shadow restart = restore checkpoint + replay the market-event journal after its cursor; identical to an uninterrupted run with complete coverage; never auto-closes. A DATA_GAP overlapping live exposure → `AMBIGUOUS_DATA_GAP` (no fabricated close/PnL, excluded from confirmed stats), then normal warmup/scanning. ERROR_HOLD is only for runtime/system uncertainty. | `runtime/shadow_service.py` (journal runner, per-input atomic commits), `engine/symbol_engine.py` (`_data_gap`, `_hole`), `marketdata/collector.py` (proven-trade journal, GAP events); tests `tests/db/test_shadow_runner.py`, `tests/db/test_shadow_gap.py` |
| B45 | Host sleep produces DATA_GAP exactly as before; detected (wall vs monotonic clock) and shown with long gaps in diagnostics and the WebUI. Production needs an always-on host or no system sleep. | `collector.tick`, `data_gaps.kind = HOST_SLEEP`, `/api/collector` diagnostics |

## OPEN — Live blockers (waiting on user actions or approved probes)
| ID | Status | Next step |
|---|---|---|
| **B40** | Exchange leverage read 12x (API). No API write will be made. | You set BTC_USDT to 10x in the Tabdeal UI → rerun `python -m sp2l validate-readonly --only CROSS_10X`. Live stays blocked unless it reads exactly 10. |
| **B41** | `CROSS_MARGIN_UNVERIFIED` while flat (not inferred). | Verify Cross (and 10x) on an explicitly approved minimum-size position later. |
| **B42** | The drift probe now uses the request midpoint over ≥ 20 samples, with a 500 ms Live bound. The Mac clock was ~0.5 s slow (NTP). | You enable automatic time sync → rerun `python -m sp2l validate-readonly --only SERVER_TIME_DRIFT`. |
| **B43** | Resolved in code (futures REST uses `BTC_USDT`). | — |
| **B44** | availableBalance 0. No validation order while it is 0; minQty/minNotional are never guessed. | You provide dedicated collateral; every order test needs explicit approval. |
| **B45 (production)** | Development keeps Mac sleep → DATA_GAP. | Always-on host, or no system sleep while SP2L services run (display sleep is fine). |

No strategy-rule questions (B01–B38) are open.

## NEW — found in live operation (2026-09-26)
| ID | Finding | Consequence | Options (your decision; nothing changed) |
|---|---|---|---|
| **B46** | Tabdeal closes the futures stream about **once an hour** (the log shows code 1000, and twice the explicit reason "please reconnect"; seen at ~16:18, 16:45, 17:46, 18:46, 19:26 UTC). Reconnects take 4–65 s. There were also 4 local `No route to host` network errors. Each drop correctly creates a DATA_GAP minute. | Under B06/B25/B31 every DATA_GAP re-anchors M5 and needs 150 unbroken M5 bars (12.5 h). The longest unbroken run observed is **11 bars (55 min)**, so Context never becomes warm and Shadow/Live would never trade. | (1) **No rule change, engineering first (recommended):** run two independent, staggered stream connections and prove coverage as the union of both (dedupe by trade `sequence`). Then a single connection's reconnect is not a gap. This must be measured, since Tabdeal may close all connections at the same moment. (2) Keep the rules and accept no warm context on this feed. (3) A strategy-rule change for short reconnect gaps, which is your decision only. |

### B46 — transport-redundancy test (approved 2026-09-26, in progress)
- **Implemented (no strategy, DATA_GAP, warmup or Shadow-eligibility rule changed):**
  - Two independent Tabdeal broadcast connections A and B, B started 600 s after A.
  - Per-connection pong proofs; merged coverage is the union of the proven intervals. Any instant no connection proves is still DATA_GAP.
  - A pong on one connection never proves the other's receipts.
  - Trades are deduplicated by `sequence`, with every duplicate's fingerprint (sequence, exch_ts, price, amount) checked and conflicts recorded.
  - Evidence: `feed_connection_events`, `feed_conflicts`, `collector_heartbeats.detail`, `data_gaps`, and the `market_events` M1 statuses.
- **Correction found in the first minutes of the test:** `sequence` is not unique; one sequence can carry several fills. Identity is now fingerprint + occurrence (multiset union across connections). Data collected before run 7 undercounts same-sequence fills (see `docs/tabdeal_endpoint_map.md`).
- **Measurement:** collector run 7 started 2026-09-26 20:12:39 UTC for 24 h (the Mac is kept awake with `caffeinate -i -s` for that window only). Report: `python -m sp2l feed-report --hours 24`; the WebUI has a "Feed redundancy · B46 test" panel.
- **Acceptance:** at least one continuous segment of >= 150 finalized M5 bars with no DATA_GAP (preferably zero merged gaps over 24 h).
- **If A/B closes turn out correlated:** report it first, then test the rolling-reconnect design (bring up and prove a replacement before retiring the old socket, with timing from measured connection lifetimes).

## V5.6 market-data integrity (2026-09-27): open decisions

**B47 — Should Tabdeal's chart-history bars be used for long-gap repair or startup bootstrap?** (OPEN, owner decision)
- Source: `GET api-web.tabdeal.org/special-margin/plots/history/`. It is public, unauthenticated, and native to Tabdeal's own futures chart.
- Measured against 537 proven live M1 bars (run 7):
  - the bars are TradingView-style continuous: open is the previous close, and high/low include that open;
  - trades are bucketed by Tabdeal record time, which is −25..+427 ms from the WS time;
  - there is no trade count.
- Under that model, 503/537 M1 (93.7 %) match exactly. The 5-minute bars match high/low/close exactly in 85/102 (83 %); differences have a median of 2.5 USD and a maximum of 32.5 USD.
- In about half of all minutes one side of the high/low equals the previous close, so the true trade high/low cannot be recovered. The strategy's Liquidity gate needs trade count, which is not available.
- Recommendation: do NOT make it canonical. Keep it diagnostic-only, as now.
  - Gaps over about 2 min stay UNRECOVERED, so the 150-bar warmup restarts.
  - Reduce such gaps with B49 instead.
  - Accepting it would change indicator inputs and needs an explicit spec decision.

**B48 — Tabdeal's WS broadcast silently omits some trades.** (OPEN)
- Over 2.5 h, 3,731 REST trades were compared with WS trades. 23 trades (0.6 %), often in multi-fill bursts, were never broadcast on either A or B, even though coverage was pong-proven and they were far from any gap or connection event.
- This is consistent with Tabdeal volume being higher in about 5 % of live minutes.
- Pong coverage proves we received everything that was sent, not that everything was sent.
- Options:
  - (a) accept the WS stream as canonical, which is the current state;
  - (b) add continuous REST `recent-trades` polling as a third, completeness channel and define canonical trades as WS ∪ REST. This changes candle values in about 5 % of minutes and needs a spec decision.
- Recommendation: (b), after a further 24 h measurement.

**B49 — Host network outages cause the unrecoverable gaps.** (OPEN, operations)
- Every gap longer than 2 min so far came from the Mac losing DNS/network: `gaierror 8` on both sockets, lasting 2.5 min, 7.5 min and 26 min. A/B redundancy on one host cannot cover this.
- Recommendation: run the collector on an always-on host with a stable uplink (VPS), optionally with a second collector site.

## V5.7 update (2026-09-27)
- **B48 — RESOLVED, now mandatory architecture.** Continuous REST reconciliation is live on the server.
  - REST-only trades are 0.52–0.93 % of canonical trades per hour.
  - Before reconciliation, 17 % of live M1 differed from Tabdeal's own chart (mostly volume). After the identity fixes, 25/25 reconciled minutes (16:39–17:04 UTC) match exactly.
- **B47 — still OPEN.** Chart-history bars are not needed for gaps within the REST window: the maximum recoverable host outage is currently 35–56 s. Longer host outages remain UNRECOVERED.
- **B49 — improved.** The collector now runs on the server. No UNRECOVERED gap has occurred since V5.7; every WS drop and every restart was repaired.

## V5.8 update (2026-09-28)
- **B47 — RESOLVED as tier 3.** Tabdeal chart history (`special-margin/plots/history`) is validated per repair against our own canonical minutes. Offline agreement is 96.85 % exact on 762 minutes (docs/tabdeal_history_source.md). It is accepted only as CANDLE_HISTORY_REPAIR (OHLCV; trade count UNKNOWN; never fills). A failed validation fails closed.
- **B50 — OPEN (Live): LEVERAGE_MISMATCH.** The account reads 61x, but the strategy leverage is exactly 10x. Live stays blocked until the exchange reads 10x. The change must be made by the account owner; SP2L never writes leverage.
- **B51 — OPEN (Live): SERVER_TIME_DRIFT and SYMBOL_FILTERS** read-only checks still fail (unchanged from V5.7).

## V5.12 update (2026-09-30)
- **B47 — bootstrap scope decided.** Tabdeal chart history (same validated source as tier 3) may also fill the canonical M1 series at a cold start (`collector.history_bootstrap`, default off), so M5 Context does not wait 150 live bars.
  - Scope: minutes before the collector's live tail within `max_lookback_s` (default 7 days), fetched in one read-only public request, validated against our own most recent live minutes (tier-3 policy) and failing closed.
  - Stored as `TABDEAL_HISTORY_REPAIRED` / `CANDLE_HISTORY_REPAIR`: OHLCV only, trade count UNKNOWN, never fills or intrabar order. M5 is built from those M1 only.
  - Missing chart minutes: 1–2 minutes → synthetic no-trade minutes; longer → DATA_GAP (owner decision).
  - XAUT agreement: 95.97 % exact on 298 minutes; worst H/L 0.05 % (docs/tabdeal_history_source.md).
