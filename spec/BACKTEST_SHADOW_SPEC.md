# Backtest / Shadow / Counterfactual V5 (rev 5.5)

## Canonical causal engine
Event order per M1 bucket:
1. process raw trades/order events in timestamp order;
2. resolve fills/touches for orders that were already active;
3. finalize M1 candle;
4. if M5 boundary closes, finalize M5 and update pivots/ADX/CHOP/EMA/context state;
5. evaluate structural extension/context/exhaustion;
6. cancel/reconcile/reprice;
7. replacement becomes active only after step 6.

No earlier Low/High may fill an order created at candle close.

## Shadow fills
With raw trades, future touch/cross fills. With OHLC-only, only an order active before candle open may use that candle's range. Partial liquidity is not invented from OHLC.

## Same-bar TP/SL
Without intrabar sequence => AMBIGUOUS and worst-case reporting.

## Rejected-candidate counterfactual
For every candidate rejected by Context/Exhaustion, create a parallel non-executing simulation using the same frozen base SP2L levels and causal fill rules. This simulation:
- never creates exchange orders;
- never reserves real Shadow capital;
- never affects runtime rules;
- exists only to measure whether gates add value.

UI/Analytics must clearly label it COUNTERFACTUAL.

## V5.1 resolutions (B05, B19, B20, B25)
- **Candles (B05):** built from exchange trade timestamps with UTC-epoch buckets.
  - A minute with zero trades is missing.
  - M1 finalizes at boundary + 2 s grace.
  - Late trades are logged and never mutate finalized candles.
  - TradeCount = raw trade count.
- **Gaps (B25):** an unrecoverable feed gap marks the affected data incomplete. No candles are synthesized. Candidate creation stops, origin continuity breaks, and indicators re-anchor and warm up again.
- **Shadow fills (B19):**
  - A touch starts PullbackStart but is not a fill.
  - A resting limit fills its full quantity once a raw trade prints strictly through the limit price.
  - SL fills at the worse of the SL price and the triggering trade price.
  - No partial fills are fabricated.
- **Counterfactual (B20):** continues canonical base-SP2L causally (including repricing on extension) and reports results in R. It is isolated from runtime decisions.

## V5.2 (B31, supersedes the B05 "zero-trade minute = missing" rule)
- **Healthy zero-trade minute:** if WS coverage is known healthy for the full minute and there were zero trades, create a synthetic no-trade M1:
  - `O=H=L=C=previous close`, `volume=0`, `trade_count=0`;
  - `synthetic_no_trade=true`, `data_gap=false`.
  Synthetic candles may maintain indicator and time continuity, but they are ineligible for P-Gap, Spike confirmation, SpikeOrigin and directional-sequence continuation.
- **Incomplete coverage:** if WS coverage is incomplete or disconnected for any part of the interval, mark `DATA_GAP`. Missing market data is not synthesized. Strategy continuity breaks, recursive indicators re-anchor, the normal warmup is required, and candidates are blocked.
- **M5 flags:** M5 inherits explicit data-quality flags from its M1 bars.

## V5.3 (B33, B34)
- **M5 data-quality fields (B33):** every M5 bar stores `synthetic_m1_count`, `synthetic_fraction` and `real_trade_count`.
  - A bar with at least one real trade is valid for indicators, range, liquidity and pivots.
  - An all-synthetic bar (healthy coverage, no trades) is valid for indicators, range and liquidity, but can never create a pivot or Swing High/Low. Its use as a pivot neighbour is PROVISIONAL B37.
- **Post-gap anchor (B34):** a pre-gap close is never carried across a DATA_GAP.
  - After coverage returns, the first genuine post-gap trade establishes the new price anchor.
  - Later fully covered zero-trade minutes are synthesized from the last valid post-gap price.
  - Until such a trade exists, zero-trade minutes are `UNANCHORED` (treated as DATA_GAP) and no synthetic candle is produced.
  - The gap still breaks strategy continuity and forces indicator re-anchoring and warmup.

## V5.5 (B39, B45)
- **Restart (B39):**
  1. Restore the last persisted engine/setup state and its market-event cursor.
  2. Replay every stored market event after the cursor in exact causal (journal) order.
  3. With complete coverage and no DATA_GAP, the result is identical to an uninterrupted run.
  4. No automatic close at restart.
  5. A DATA_GAP overlapping an active order/position finalizes the setup as `AMBIGUOUS_DATA_GAP`.
- **Engine-input atomicity:** everything a single market event causes (strategy rows, checkpoint, cursor) is committed atomically, so a replay can never double-apply an event.
- **Host sleep (B45):** host sleep/wake produces DATA_GAP exactly like any coverage loss; nothing is synthesized across it. Host-sleep and long-coverage-gap events are recorded and shown in diagnostics and the WebUI. Production data collection requires an always-on host, or a Mac that never system-sleeps while SP2L services are active (display sleep is irrelevant).
