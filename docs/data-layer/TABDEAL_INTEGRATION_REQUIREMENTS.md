# Tabdeal Integration Requirements V5 (rev 5.5)

Verify current official Tabdeal FAPI docs before Live implementation. Do not infer undocumented behavior.

## Market data
Event-driven raw trade WebSocket is canonical for live price ordering and M1 OHLCV construction. Persist raw trades continuously. Aggregate finalized M5 from canonical finalized M1, not from an independent source unless equivalence is proven.

## Order safety
E1 may be cancel/replaced only while it is a **pure pending entry order**.

Required guard:
```text
order_status == NEW
AND executedQty == 0
AND setup_position_qty == 0
```

Every potential E1 revision uses:
```text
process events through candle close
→ reconcile E1 order
→ reconcile setup-linked position
→ if any fill/position exists: STOP REPRICE and protect
→ otherwise cancel old E1
→ confirm old order inactive
→ confirm executedQty == 0
→ confirm setup_position_qty == 0
→ recompute
→ submit one replacement
```

Never optimistic-cancel. Never replace an order that has partially or fully filled. Ambiguity => ERROR_HOLD.

## Fill truth
Live fill truth = exchange order/fill status, not local touch.

## Protection
Any E1 `executedQty > 0` freezes the entry price and gets SL/single-TP protection immediately.

For partial E1:
- leave remaining E1 quantity at the original E1 price;
- do not move/recreate it at a new price;
- E2 remains disabled until E1 is fully filled;
- at FillWindow expiry cancel only the unfilled remainder.

E2 cannot be sent before full E1 + verified protection.

## Restart
Reconcile account, position, open orders, recent order/trade history, logical E1 revision, E2 and protection before any new order.

## Idempotency
Persist setup_id, leg_id, revision_id, client_order_id, exchange_order_id and transition state. Retry must never duplicate an order.

## E2 add-to-position semantics
E2 is intended to increase the same-direction position:
- Long setup => E2 is another Long/BUY entry.
- Short setup => E2 is another Short/SELL entry.
- E2 must not be a reduce/close order.

If current Tabdeal FAPI exposes a `reduceOnly` flag, use `reduceOnly=false` only after minimum-size runtime validation proves the current semantics. If this is not verified, disable Live E2 and report a runtime-validation blocker; do not guess.

## V5.1 resolutions and documented API facts (docs.tabdeal.org, 2026-09-26)
- **Order types:** FAPI orders are LIMIT or MARKET only; `timeInForce` is GTC/IOC/FOK; there is **no post-only**. E1 therefore uses the B15 submit guard and is never sent while marketable.
- **reduceOnly:** documented as "not supported (default false)". Live E2 stays disabled until the aggregation probe proves that a same-side LIMIT increases the one-way (`BOTH`) position.
- **SL/TP:** position-level via `POST /fapi/v1/positionSlTp` (`positionId`, `slPrice`, `tpPrice`, `workingType`). SP2L uses `workingType=CONTRACT_PRICE` (B24), which must be runtime-validated before Live.
  - On any position close, cancel the E2 and E1 remainder immediately so a pending entry cannot re-open a position.
- **Emergency close (B21):** `DELETE /fapi/v1/position`, which closes the symbol position with a market order. It must be validated before Live.
- **Exchange filters (B26):** only `pricePrecision`/`quantityPrecision` are documented.
  - `10^-precision` is provisional for Shadow and development only.
  - Live needs probe-established accepted increments and minimums.
  - Unknown minQty/minNotional means Live is disabled.
- **Market data (B05, B25):** futures trades come only from `wss://api1.tabdeal.org/special_margin/broadcast/` (plain text `BTC_USDT`). There is no documented futures trade REST endpoint and no klines, so an unrecoverable gap is marked incomplete and never synthesized.
- **Order and fill state:** no futures user-data stream is documented, so state comes from REST polling (`/r/fapi/v1/order`, `/userTrades`, `/r/fapi/v3/positionRisk`).
