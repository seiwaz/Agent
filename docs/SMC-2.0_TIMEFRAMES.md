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


## Without the liquidity-sweep condition (`require_sweep: false`, owner 2026-10-05)

Same data, costs and rules otherwise. Columns: trades closed / wins / stopped before TP1 /
total R. Expiry = 8 entry-TF bars.

| bias / zone / entry | BTC | XRP | both |
|---|---|---|---|
| 1h/15m/5m · time stop 3 h | 131 / 23 / 102 / -87.82R | 122 / 23 / 93 / -73.23R | -161.05R |
| 1h/15m/5m · no time stop | 129 / 23 / 106 / -80.37R | 121 / 17 / 104 / -81.46R | -161.83R |
| 4h/15m/5m · time stop 3 h | 128 / 26 / 99 / -81.30R | 123 / 22 / 95 / -75.35R | -156.65R |
| 4h/15m/5m · no time stop | 125 / 23 / 101 / -77.15R | 122 / 19 / 103 / -78.06R | -155.21R |
| 4h/1h/5m · time stop 3 h | 18 / 5 / 11 / -5.65R | 21 / 7 / 12 / -9.48R | -15.13R |
| 4h/1h/5m · no time stop | 18 / 4 / 14 / -8.31R | 21 / 3 / 18 / -15.35R | -23.66R |
| 4h/1h/15m · time stop 3 h | 23 / 7 / 14 / -8.01R | 24 / 8 / 14 / -10.00R | -18.01R |
| 4h/1h/15m · no time stop | 23 / 3 / 20 / -12.16R | 24 / 2 / 22 / -19.71R | -31.87R |
| 1d/15m/5m · time stop 3 h | 99 / 21 / 75 / -59.81R | 125 / 28 / 94 / -63.98R | -123.79R |
| 1d/15m/5m · no time stop | 96 / 18 / 78 / -52.73R | 124 / 24 / 100 / -59.45R | -112.18R |
| 1d/1h/5m · time stop 3 h | 14 / 6 / 7 / +3.28R | 17 / 4 / 13 / -11.27R | -7.99R |
| 1d/1h/5m · no time stop | 14 / 5 / 9 / +1.81R | 17 / 1 / 16 / -15.23R | -13.42R |
| 1d/1h/15m · time stop 3 h | 16 / 6 / 9 / +1.71R | 19 / 5 / 14 / -9.96R | -8.25R |
| 1d/1h/15m · no time stop | 16 / 4 / 12 / -2.27R | 19 / 1 / 18 / -15.64R | -17.91R |
| 1d/4h/5m · time stop 3 h | 2 / 0 / 1 / -1.33R | 2 / 0 / 2 / -2.00R | -3.33R |
| 1d/4h/5m · no time stop | 2 / 0 / 1 / -1.66R | 2 / 0 / 2 / -2.00R | -3.66R |
| 1d/4h/15m · time stop 3 h | 2 / 0 / 1 / -1.33R | 3 / 1 / 2 / -1.85R | -3.18R |
| 1d/4h/15m · no time stop | 2 / 0 / 1 / -1.66R | 3 / 0 / 3 / -3.00R | -4.66R |
| 1d/4h/1h · time stop 3 h | 3 / 0 / 1 / -1.58R | 3 / 1 / 2 / -1.91R | -3.49R |
| 1d/4h/1h · no time stop | 3 / 1 / 1 / -1.24R | 3 / 0 / 3 / -3.00R | -4.24R |

- Removing the sweep multiplies the setups (1h zone: 69 / 72 instead of 8 / 18; 15m zone:
  ~350 instead of ~70) and the trades (1h zone: 14–24 per market, 15m zone: ~100–130).
- Every combination loses on XRP; 75–85 % of the trades are stopped out before TP1.
- The only positive results are BTC 1d / 1h / 5m (+3.28R, 14 trades, PF 1.46) and
  1d / 1h / 15m with the time stop (+1.71R, 16 trades); the same rows lose about 10–11R on XRP.
- The 3 h time stop helps on nearly every row.
- Config row (4h / 1h / 15m, time stop 3 h): BTC −8.01R over 23 trades, XRP −10.00R over 24.
