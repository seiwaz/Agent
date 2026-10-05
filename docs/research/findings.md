## Findings (2026-10-05) — research only, nothing in the live system changed

### 0. Preconditions
- Fixed with tests (commit 89cae34): inner history holes are re-fetched; every chart request
  starts 5 minutes early and drops the lead bars (Tabdeal's first-bar artifact); a market
  take-profit pays the slippage allowance. All 9 markets were re-fetched with the fixed loader:
  the XRP hole of 2026-05-03..05 (2,877 minutes) is filled; the largest hole left is 30 minutes
  (2026-03-20 19:51–20:21 UTC, the same in every market: a Tabdeal outage).
- Backtest before / after the fixes (SMC-2.1, 270 days): BTC −2.00R (2 closed trades) / −2.00R
  (2 closed; the 2026-03-11 setup is now rejected LOW_NET_RR because its TP pays slippage);
  XRP −1.00R / −1.00R.
- Markets (Tabdeal 1-minute retention measured on 2026-10-05): BTC, XRP, ETH 296 days; SOL,
  DOGE, ADA 253; BNB 202; LTC 197; AVAX 167; LINK only 90 (not used). Only BTC, XRP and ETH
  reach 270 days.

### 1. Is there an edge before costs? (event study, discovery data, 9 markets pooled)
Each zone touch is compared with 5 random levels (same market, timeframe, period, distance from
price and width in ATR); percentage points (pp) of P(+1R before −1R), 95 % bootstrap interval.
- **Order blocks: at most a borderline 1–3 pp.** OB_last: +0.3 (5m), −1.3 (15m), +2.9
  [+0.0, +5.9] (1h), −6.8 (4h); OB_extreme +1.1 [+0.2, +1.9] on 5m, otherwise intervals
  including zero; OB + adjacent FVG and OB + FVG after a sweep: no difference on any timeframe.
- **FVG: a real but tiny edge on 5m and 15m:** +1.1 pp [+0.8, +1.4] and +1.8 pp [+1.1, +2.4];
  about +3 pp on 15m against the 1h / 4h bias. None on 1h / 4h.
- **Almost every zone still loses before costs:** P(+1R before −1R) is 36–51 % (break-even 50 %),
  P(+2R) 26–38 % (break-even 33 %); only 1h / 4h order blocks reach break-even, and there the
  random levels do as well or better (4h OB_extreme 51.4 % vs random 56.6 %).
- Filters: touches on the premium side are clearly worse than random (OB + adjacent FVG 5m
  −19.8 pp [−29.8, −9.0]; FVG 5m −1.9), so the discount rule points the right way, but ~90 % of
  the zones are already in the discount half. Trading **with** the 4h bias is never better than
  against it (FVG 15m: with +0.7 vs against +3.0 pp; OB_last 15m with the 4h bias −2.4 pp
  [−4.5, −0.4]). Freshness: the second touch does as well as the first (OB_last 1h 49.3 % →
  47.1 %, 15m 45.4 % → 48.2 %).

### 2. Trade level (28 variants, 9 markets, discovery)
- **No variant has a gross average (before costs) with a 95 % interval above zero.** The large
  samples: base on 5m zones 4,123 trades, gross −0.077R per trade [−0.144, −0.008] (significantly
  negative); 15m 1,330 trades, +0.003 [−0.105, +0.121]; 1h 228 trades, −0.063 [−0.289, +0.179].
  The best point estimates (full model with swing_len 3: +0.51 over 33 trades; swing_len 2:
  +0.43 over 53) have intervals from about −0.4 to +1.2: noise.
- Random entries with the same stops and targets do about as well as the setups (base 5m
  random gross +0.07, 15m −0.06, 1h −0.23).

### 3. How much of the loss is costs?
- Costs are 58 % of 1R on 5m zones (median stop 0.15 % of price), 42 % on 15m (0.28 %), 26 % on
  1h (0.59 %) and 14 % on 4h (1.32 %); Tabdeal level 1 round trip = 0.2076 % of price.
- Share of the net loss caused by costs: base 1h gross −14.3R → net −65.8R (costs 78 % of the
  loss); base 15m gross +3.7R → net −570.9R (all of it); base 5m gross −316.8R → net −2,497.6R
  (87 %). The rest is entries that are no better than random.
- Levers (best variants and SMC-2.1): a maker take-profit (not available on Tabdeal: no
  reduce-only, TP is a market trigger) adds ~+0.01R per trade; a market entry costs ~−0.06R;
  Tabdeal's higher fee levels help but do not turn 5m / 15m positive; the minimum-stop filter
  (k = 3, 5, 8 × round trip) removes almost every trade. Break-even round trip: full model
  swing_len 3 0.267 % (1.29 × today), swing_len 2 0.251 % (1.21 ×), full on 5m 0.028 %
  (0.13 ×), SMC-2.1 0.033 % (0.16 ×) — but the first two have no reliable gross edge.

### 4. Holdout
No variant met the rules on discovery data (≥ 100 trades, gross interval above zero, positive on
≥ 2/3 of the markets), so nothing was run on the holdout.

### Recommendation
**Stop: no edge was found.** The SMC zones as implemented barely beat random levels in these 9
markets (FVGs on 5m / 15m and a few order-block cases, by 1–3 percentage points, which costs
more than wipe out), and
no configuration has a positive gross expectancy with confidence. Do not switch the bot to any
candidate. If the work continues, it needs a different hypothesis (other entry or exit logic),
tested first with this event study before any trade-level work, and only on 1h / 4h zones where
costs are below ~25 % of the risk.

