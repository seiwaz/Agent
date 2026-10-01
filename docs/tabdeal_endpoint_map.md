# Tabdeal FAPI Endpoint Map (draft, read-only docs review)

Source: https://docs.tabdeal.org/ fetched 2026-09-26. Changelog shows the latest version as v0.9.0.
HTML sha256 `c84ff7715cfefe568f1541f2e42dc07b8595f9b3ae6961d1b6680e6873943d8d`. A text snapshot is in
`docs/snapshots/tabdeal_docs_2026-09-26.txt`.

This lists **only what the docs state**. Everything else is marked UNDOCUMENTED and must be closed by a
runtime probe (plan §8) before Live. No request has been sent to Tabdeal except the public docs page.

## Endpoints relevant to SP2L

| Purpose | Endpoint | Auth | Notes from docs |
|---|---|---|---|
| Ping / time | `GET /r/fapi/v1/ping`, `GET /r/fapi/v1/time` | NONE | Signed requests fail if `timestamp` is >60 s ahead or outside `recvWindow` (max 60000). |
| Exchange info | `GET /r/fapi/v1/exchangeInfo?symbol=BTCUSDT` | NONE | Example returns only `pricePrecision`, `quantityPrecision`, `quotePrecision`. **No tickSize, stepSize, minQty or minNotional documented.** |
| Depth | `GET /r/fapi/v1/depth`, `/aggDepth` | NONE | — |
| New order | `POST /fapi/v1/order` | TRADE | Only `LIMIT` and `MARKET`. `timeInForce` is GTC/IOC/FOK ("other types not supported for now"). `reduceOnly`: "not supported for now (default false)". `newClientOrderId` is optional. |
| Query / cancel order | `GET /r/fapi/v1/order`, `DELETE /fapi/v1/order` | TRADE | Status set: NEW, PARTIALLY_FILLED, FILLED, CANCELED, REJECTED. Response has `executedQty`, `cumQty`, `avgPrice`. |
| Open / all orders | `GET /r/fapi/v1/openOrders`, `/allOrders` | TRADE | — |
| User trades | `GET /r/fapi/v1/userTrades` | TRADE | Fill truth via REST. |
| Position risk | `GET /r/fapi/v3/positionRisk` | TRADE | `positionAmt, entryPrice, markPrice, unRealizedProfit, liquidationPrice, leverage, marginType ("cross"), positionSide ("BOTH")`. |
| Position list | `GET /r/fapi/v1/position` | TRADE | Returns position `id` (needed as `positionId`), `status` ACTIVE/closed, `realizedPnl`. |
| Leverage | `GET /r/fapi/v1/leverage`, `POST /fapi/v1/leverage` | TRADE | **No margin-type set endpoint documented**; cross can only be read. |
| Balance / account | `GET /r/fapi/v3/balance`, `/r/fapi/v3/account` | TRADE | `walletBalance, availableBalance, crossWalletBalance, crossUnPnl`. |
| Position SL/TP | `POST /fapi/v1/positionSlTp` | TRADE | **Position-level**: `positionId` (required), `slPrice`, `tpPrice`, `workingType` = `MARK_PRICE` or `CONTRACT_PRICE`. One TP per position by construction. |
| Close position | `DELETE /fapi/v1/position` | TRADE | Closes the whole symbol position with a market order. |
| Liquidations | `GET /r/fapi/v1/forceOrders` | TRADE | At most 100 records; startTime/endTime are "currently not used by the server". |
| Futures trade stream | `wss://api1.tabdeal.org/special_margin/broadcast/`, send plain text `BTC_USDT` | — | Pushes `{"trade": {...}}` per new trade. **Trade message fields, trade IDs and sequence numbers are not documented.** |
| Futures depth stream | `wss://api1.tabdeal.org/special_margin/stream/`, topic `special_margin@BTC_USDT@depth@100ms` | — | — |

## What this changes in the plan (new or sharpened BLOCKERS)

- **B15 (marketable E1):** there is **no post-only / GTX**. The recommended "post-only" option is unavailable. A crossing LIMIT E1 fills as taker. A decision is needed: skip, submit anyway, or submit only if the last trade is strictly on the profit side of E1.
- **E2-04 / reduceOnly:** the flag is "not supported (default false)". The docs don't say whether a same-side LIMIT increases a one-way (`BOTH`) position. **Live E2 stays disabled** until the aggregation probe proves it.
- **Protection is position-level (UNRESOLVED #1/#2):** SL/TP attach to `positionId`, not to orders. Still to be proven by probe:
  - does the same TP/SL cover quantity added later by the E1 remainder or E2?
  - what order type executes on trigger?
  - is SL/TP cleared on close?
  - does a still-pending E2 **re-open** a position after SL/TP closes it? Until proven, the engine must cancel E2 and the E1 remainder immediately on close.
- **B24 (new): SL/TP trigger price:** `MARK_PRICE` vs `CONTRACT_PRICE`. The spec doesn't choose, and the choice changes when SL/TP fire.
- **B25 (new): raw-trade completeness and backfill:** there is **no documented public futures trade REST endpoint and no klines**. A WS disconnect creates an unrecoverable gap, so the affected M1 candles can never be rebuilt. The gap policy is needed now (UNRESOLVED #4 turns from "verify" into "known gap"). The trade fields and IDs needed for dedup/continuity are also undocumented.
- **B26 (new): tick/step/min-qty source:** only `pricePrecision`/`quantityPrecision` are documented. `tick = 10^-pricePrecision` is an inference, and SL = OriginLow − 1 tick depends on it. Needs a probe or your confirmation.
- **No futures user-data stream** is documented (the listenKey stream is spot). Order and fill state comes from REST polling, which sets the latency for fill detection and for the cancel-race handling.
- **Cross margin:** it can be read (`marginType`) but not set via the API. The CROSS_10X probe is read-and-verify only.
- **Duplicate `newClientOrderId`** behaviour is undocumented (NO_DUPLICATE_E1 probe).

## Observed on the live stream (2026-09-26, public, no API key)
- Frame format: `{"trade":{"amount":"0.00437","price":"84027.4","updated":"2026-09-26 15:43:42.024000+00:00","sequence":38127901762,"side_name":"Buy","symbol":"BTC_USDT"}}`.
- `sequence` is unique and increasing but **not contiguous** (it jumps by thousands between BTC_USDT trades, so it is probably a global sequence). It can deduplicate but cannot detect loss, so the collector proves coverage with its own ping/pong.
- No snapshot is sent on subscribe; only live trades arrive. About 25–40 BTC_USDT trades per minute during the test.
- **Clock offset:** `recv_ts - exch_ts` ranged from −170 ms to +173 ms, so the local clock is at least ~170 ms behind the exchange's. The 1 s `clock_safety` absorbs it, but the SERVER_TIME_DRIFT probe must measure it before Live.

## Observed with authenticated READ-ONLY checks (2026-09-26, evidence in runtime_validation_runs)
- **Symbol naming:** futures REST uses `BTC_USDT` (all 74 markets are `BASE_QUOTE`). `?symbol=BTCUSDT` gives `exchangeInfo` → `{"symbols": []}`, and `GET /r/fapi/v1/leverage` → HTTP 400 `{"code":1208,"msg":"Invalid symbol"}`.
- **exchangeInfo BTC_USDT:** `pricePrecision 1`, `quantityPrecision 5`, `quotePrecision 3`, status TRADING. There is still no tickSize/stepSize/minQty/minNotional (B26).
- **`/r/fapi/v3/balance` and `/v3/account`:** USDT walletBalance 6.61891795, availableBalance 0, crossWalletBalance 6.61891795; `canTrade: true`; no positions.
- **`/r/fapi/v3/positionRisk?symbol=BTC_USDT`:** `[]` when flat, so marginType is not readable.
- **`/r/fapi/v1/leverage?symbol=BTC_USDT`:** `{"leverage": 12}`.
- **`/r/fapi/v1/openOrders`, `/r/fapi/v1/position?isActive=1`:** empty.
- **Server time:** RTT 0.5–1.7 s from this host; offset 0.6–1.2 s with the local clock ~0.5 s slow per NTP.
- **Stream:** the server closed the WebSocket with code 1000 (NORMAL_CLOSURE) once at 16:18:43 UTC; the collector reconnected in 2.3 s and recorded an ~11 s DATA_GAP.

## `sequence` is not a unique trade id (observed 2026-09-26, B46 test)
- A single `sequence` can carry several distinct trades with the same `updated` timestamp but a different `price`/`amount` (multi-level fills), e.g. `38177414581` at 20:09:11.940 UTC: `84010.2 × 0.00003` and `84001 × 0.0002`. Both connections deliver both payloads.
- Trade identity is therefore `(sequence, exch_ts, price, amount)` plus an occurrence number; across connections the stored multiplicity is the maximum any single connection delivered.
- **Impact on older data:** before collector run 7 (2026-09-26 20:12:39 UTC), candle building and `raw_trades` deduplicated by `sequence` alone, so same-sequence fills after the first were dropped. Candles collected before run 7 can undercount trades/volume and can miss highs/lows. Shadow has not run on that data. Treat pre-run-7 candles as unreliable for backtests.

## Public web-app endpoints (discovered 2026-09-27 from Tabdeal's own UI bundles; read-only, unauthenticated)
| Path (base https://api-web.tabdeal.org) | Method | Params | Response | Used by SP2L |
|---|---|---|---|---|
| `/special-margin/recent-trades/` | GET | `symbol=BTC_USDT` | `{trades:[{created, updated (µs UTC), market_id, price, amount, side, side_name, …}]}`, the last 50 trades, newest first, no paging | V5.6 exact gap repair |
| `/special-margin/plots/history/` | GET | `first_currency_symbol=BTC&second_currency_symbol=USDT&symbol=BTC_USDT&resolution=1|5|…&from=<s>&to=<s>&countback=N` | `{data:[{time (bar open, epoch s), open, high, low, close, volume}], no_data}` | diagnostics only (B47) |
| `/r/service/plots/candle_v2_history/` | GET | `type=…` | mark-price bars used by the chart's mark-price mode | not used |

Tabdeal's futures chart is TradingView 27.006 with this datafeed (`BROWSER_BASE_URL=https://api-web.tabdeal.org`). The chart's timezone is Asia/Tehran for display; `time` values are epoch seconds. Live bars are built in the browser from the same WS broadcast, with open = the previous bar's close.
