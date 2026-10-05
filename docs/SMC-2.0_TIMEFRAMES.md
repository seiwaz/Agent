# SMC-2.0 — timeframe matrix (backtest, information only)

Data: Tabdeal 1-minute history, 2026-01-06 .. 2026-10-05 (270 days), BTC/USDT and XRP/USDT,
after fees (maker 0.08 %, taker 0.095 %, slippage 0.0326 %). Rules as in SMC_STRATEGY.md
(sweep → BOS/CHoCH → OB + adjacent FVG; limit at the OB edge; SL one tick beyond the OB wick;
TP1 internal liquidity / previous swing, TP2 next unfilled FVG, TP3 external liquidity +
trailing). Time stop **off**; order expiry = 8 bars of the entry timeframe; max hold 24 h.
Columns: setups armed / trades closed / wins / total R.

| bias / zone / entry | BTC | XRP |
|---|---|---|
| 4h / 1h / 15m, time stop 3 h (config) | 8 / 3 / 0 / −2.17R | 18 / 3 / 2 / +0.30R |
| **4h / 1h / 15m (intraday)** | 8 / 3 / 0 / −3.00R | 18 / 3 / 1 / −1.29R |
| 4h / 1h / 5m | 8 / 3 / 1 / −1.34R | 18 / 3 / 1 / −1.71R |
| **1h / 15m / 5m (scalp)** | 69 / 21 / 3 / −15.11R | 68 / 20 / 1 / −16.41R |
| 4h / 15m / 5m | 69 / 22 / 4 / −16.82R | 68 / 20 / 2 / −14.81R |
| 1d / 15m / 5m | 69 / 20 / 5 / −13.80R | 68 / 23 / 3 / −14.31R |
| 1d / 1h / 5m | 8 / 1 / 0 / −1.00R | 18 / 2 / 0 / −2.00R |
| 1d / 1h / 15m | 8 / 1 / 0 / −1.00R | 18 / 3 / 1 / +0.36R |
| 1d / 4h / 5m | 5 / 0 / – / 0 | 4 / 0 / – / 0 |
| 1d / 4h / 15m | 5 / 0 / – / 0 | 4 / 0 / – / 0 |
| **1d / 4h / 1h (short swing)** | 5 / 1 / 1 / +0.42R | 4 / 0 / – / 0 |

- Only a 15m zone gives more trades (~20 per market); 85–95 % of them hit the stop before
  TP1 (price runs through the 15m OB). 1h / 4h zones: 0–3 trades in 9 months.
- About half of all armed setups are against the bias (BIAS_MISMATCH), on every combination.
- Without the 3 h time stop the intraday row is worse (BTC −3.00R vs −2.17R, XRP −1.29R vs
  +0.30R). A 24 h max hold cuts the 4h-zone swing trades (TIMEOUT).
- `confirm_exec` (enter at the entry-TF BOS/CHoCH after arming): 0–4 trades per market,
  none profitable (BTC −0.96R … 0, XRP −1.56R … +0.06R).
- No combination is profitable with a usable number of trades; the positive rows have 1–3
  trades and mean nothing statistically.
