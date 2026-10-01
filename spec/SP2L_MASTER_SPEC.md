# SP2L Master Specification V5 (rev 5.5)

## 1. Base timeframe and causality
M1 closed candles confirm P-Gap and structural extension. Live trade/price events trigger PullbackStart and fills. M5 Context uses finalized M5 candles only; the forming M5 candle is never used for a gate.

## 2. P-Gap
For finalized candles `(i-1,i,i+1)`:
- Bullish: `Low[i+1] > High[i-1]`
- Bearish: `High[i+1] < Low[i-1]`
- Equality is invalid.

**V5.12: the V5.9 quality filter below is REMOVED as a rule.** Every strict geometric P-Gap inside a run is a P-Gap. The values are still measured and recorded with each P-Gap (informational only, never a rejection). What follows is the measured definition (C1 = `i-1`, C2 = `i` the impulse, C3 = `i+1`):
- **Strong impulse.** `range = High(C2)-Low(C2)`, `body = |Close(C2)-Open(C2)|`, `body_ratio = body/range`.
  - A range ≤ 0 fails.
  - Long requires `Close(C2) > Open(C2)` and `body_ratio >= 0.60`.
  - Short requires `Close(C2) < Open(C2)` and `body_ratio >= 0.60`.
  - A doji or an opposite-direction C2 never confirms a P-Gap.
- **Strong gap.**
  - Long `gap = Low(C3)-High(C1)`; Short `gap = Low(C1)-High(C3)`.
  - Required: `gap >= 0.15*body(C2)` and `gap >= 2*tick`. Equality passes.
- **Scope and failures.**
  - The colour requirement applies to C2 only. Runs and all other Spike candles still ignore colour.
  - V5.12: failing these checks changes nothing: the P-Gap still qualifies and may spend the run's first P-Gap (B03).
  - Reason codes: `PGAP_IMPULSE_WRONG_DIRECTION`, `PGAP_IMPULSE_BODY_TOO_WEAK`, `PGAP_GAP_TOO_SMALL_RELATIVE_TO_BODY`, `PGAP_GAP_TOO_SMALL_IN_TICKS`.
  - The thresholds are frozen (measurement only).

## 3. Directional sequence
Long sequence preserves while `Low[current] >= Low[previous]`.
Short sequence preserves while `High[current] <= High[previous]`.
Candle color is not a core gate.

## 4. Spike confirmation
The first valid directional P-Gap of the run (V5.12: every geometric P-Gap qualifies) confirms Spike at the close of the P-Gap right candle.

The WebUI's live forming M1 (V5.9) is display only. It never confirms a P-Gap or Spike and never feeds any gate or decision.

## 5. Spike Origin
At first P-Gap confirmation:
- Long: starting at right candle, repeatedly include previous candle while current.Low >= previous.Low.
- Short: repeatedly include previous candle while current.High <= previous.High.
Stop at first violation or missing finalized candle. Earliest included candle = Origin. Freeze it.

V5.1 (B04): the P-Gap triple itself must satisfy the directional sequence (`Low[i]>=Low[i-1]` and `Low[i+1]>=Low[i]` for Long; symmetric for Short). No separate minimum-R filter.
V5.1 (B03): one candidate per directional run; a new setup on the same side requires a sequence break first.

## 6. E1 and repricing
Initial E1 after Context/Exhaustion/Risk approval:
- Long BUY LIMIT = Low(LastSpikeCandle)
- Short SELL LIMIT = High(LastSpikeCandle)

E1 repricing is permitted **only while no part of the entry has executed and no position for this setup exists**.

Canonical guard:

```text
E1_REPRICE_ALLOWED =
    order_status == NEW
    AND executedQty == 0
    AND setup_position_qty == 0
```

On every new finalized M1 candle, before any reprice decision:
1. process all raw trade/order events through candle close;
2. reconcile the active E1 with the exchange;
3. reconcile the position attributed to this setup;
4. if `executedQty > 0`, order status is `PARTIALLY_FILLED`/`FILLED`, or `setup_position_qty != 0`, **abort repricing immediately** and enter the fill/protection flow;
5. only when `E1_REPRICE_ALLOWED == true`, re-evaluate structural extension, Context Engine and Exhaustion Gate;
6. if a hard gate fails with zero fill/zero setup position, safely cancel/reconcile E1 and finalize the candidate as rejected-after-arm;
7. if extension and all gates pass, request cancellation of old E1;
8. wait until the old E1 is authoritatively inactive and confirm again that `executedQty == 0` and `setup_position_qty == 0`;
9. update LastSpikeCandle;
10. recompute E1, R, single TP, E2 reference, Qty and risk;
11. submit exactly one replacement E1.

If a partial/full fill is discovered at any point in the cancel/replace race, the replacement is forbidden.

**Once any E1 quantity is filled, the E1 price is frozen for the life of that setup.**
- no new E1 revision is allowed;
- no remaining E1 quantity may be moved to a new price;
- for a partial fill, the unfilled remainder stays at the original E1 price until it fills, expires under the FillWindow rule, or is cancelled by a terminal position event;
- the executed quantity is protected immediately;
- E2 remains disabled until E1 becomes fully filled.

No replacement is submitted while order or position state is uncertain.

## 7. PullbackStart
- Long: first live price <= active E1.
- Short: first live price >= active E1.
This freezes further E1 repricing. Live touch is not fill truth in Live; exchange status is.

## 8. Four-candle fill window
The M1 candle containing PullbackStart is #1. At close of #4:
- zero fill: cancel remainder, `EXPIRED_NO_FILL`.
- partial fill: cancel remainder, keep filled quantity protected, permanently cancel E2.

## 9. SL and TP
- Long SL = OriginLow - tickSize.
- Short SL = OriginHigh + tickSize.
- `R = abs(E1-SL)`.
- Long TP = E1 + R.
- Short TP = E1 - R.
Exactly one TP. No TP2, partial TP, AB=CD execution target or trailing target.

## 10. E2
After E1 FULL fill + reconciled position + verified original SL/single TP:
- `E2=(E1+SL)/2`
- Qty(E2)=Qty(E1)
- same original SL and same single TP
- no candle expiry
- no exit recomputation from average exchange entry.

E2 is an **add-to-position** order in the same direction as E1; it must never be interpreted as a reducing/closing order.
If Tabdeal exposes `reduceOnly`, set `reduceOnly=false` only after current runtime validation confirms the exact API semantics. If those semantics are not verified, Live E2 remains disabled rather than guessed.

## 11. Capacity
2 logical slots per market: E1 + reserved E2. No second setup on same market while reserved/active.

## 12. Runtime learning
Forbidden. Analytics may inform a future manually versioned spec only.

## V5.1 resolutions (B02, B14, B15, B23)
- **Structural extension (B02):** every finalized M1 candle that continues the directional sequence before PullbackStart becomes the new LastSpikeCandle.
- **E1 submit guard (B15):** never submit a marketable E1.
  - Long E1 may be sent only when `last_trade > E1`; Short only when `last_trade < E1`. Equality is not sufficient.
  - If it is not safe to submit, no order is sent. The setup waits for the next finalized M1 close and is fully re-evaluated.
  - E1 is never converted into a market or taker entry, and there are no retroactive fills.
- **After PullbackStart with zero fill (B14):** when exchange reconciliation confirms `executedQty == 0` and `setup_position_qty == 0`, Context and Exhaustion are still rechecked on every M1 close.
  - Repricing stays frozen.
  - A gate failure cancels the still-unfilled E1.
- **E2 rounding (B23):** E2 is rounded to the tick grid **toward SL**. The actual E2 risk and the total setup risk are recomputed from the rounded E2. Never assume exactly 0.5R after rounding.

## V5.2 resolutions (B29, B30, B31)
- **Directional run (B29):**
  - A Long run starts after the previous Low-decrease violation, continues while `Low[n] >= Low[n-1]`, and ends at the first finalized M1 with `Low[n] < Low[n-1]`.
  - A Short run is symmetric: `High[n] <= High[n-1]`, ending when `High[n] > High[n-1]`.
  - The violating candle is not part of the old run and may seed the next one.
  - The qualifying directional P-Gap must lie fully inside the run.
  - A run also ends at a synthetic no-trade M1 or a DATA_GAP (B31).
- **Unarmed expiry (B30):** if a spike/run ends before E1 was ever successfully armed/submitted, the setup is finalized as `EXPIRED_UNARMED` with reason `E1_NEVER_ARMED`. `EXPIRED_NO_FILL` is reserved for an E1 that was active and received zero fill.
- **Synthetic no-trade M1 (B31):** synthetic candles never count toward a P-Gap, Spike confirmation, SpikeOrigin or directional-sequence continuation.
