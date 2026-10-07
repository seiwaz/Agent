# System B (TREND-1.0) on BTC — backtest report (2026-10-07)

Rules and evaluation criteria: `docs/TREND_STRATEGY.md` (written and pushed in 3ac47cc, before
any run on real data). Parameters as declared: entry 20, exit 10, stop 2 × ATR(20), no regime
filter, long only, spot. Nothing was tuned after seeing the data.

## Data

| source | file | days | from → to | missing |
|---|---|---|---|---|
| Binance spot BTCUSDT (bulk archive data.binance.vision) | `data/binance_vision_BTCUSDT_1d.csv` | 3,338 | 2017-08-17 → 2026-10-06 | 0 |
| Bitstamp BTCUSD | `data/bitstamp_BTCUSD_1d.csv` | 5,529 | 2011-08-18 → 2026-10-06 | 0 |

The two sources agree on 3,338 common days: median close difference 0.06 %, p99 2.4 %, max 9.9 %
(early years, USD vs USDT). Spot checks match known levels: the 2017 top (19.7k), the Dec 2018
low (3.1k), 2020-03-12, the Nov 2021 top (69k), the Nov 2022 low (15.5k) and the Oct 2025 top
(126k). The decision dataset is Binance from 2017, as declared; Bitstamp from 2011 is context.

Costs: Tabdeal level-1 taker fee 0.095 % + slippage 0.0326 % per fill (`config/runtime.yaml`),
and both doubled for rule 5. Reproduce:

```sh
uv run python scripts/fetch_daily.py --source binance_vision --symbol BTCUSDT
uv run python -m sp2l trend-backtest --csv data/binance_vision_BTCUSDT_1d.csv --set sizing=full --grid --out docs/trend/btc_binance_full
uv run python -m sp2l trend-backtest --csv data/binance_vision_BTCUSDT_1d.csv --set sizing=risk --out docs/trend/btc_binance_risk
# rule 5: --fee 0.0019 --slippage 0.000652 ; context: --csv data/bitstamp_BTCUSD_1d.csv
```

## Results (Binance, 2017-09-06 → 2026-10-06)

Buy & hold is bought on the same first day with the same costs.

| | CAGR | max drawdown | Sharpe | exposure | trades | win rate | avg R | profit factor |
|---|---|---|---|---|---|---|---|---|
| **System B, sizing full** | **53.7 %** | **48.5 %** | **1.25** | 41 % | 45 | 46.7 % | +2.36 | 2.36 |
| **System B, sizing risk (1 %)** | 10.5 % | 11.2 % | 1.16 | 41 % | 45 | 46.7 % | +2.36 | 4.77 |
| Buy & hold | 37.9 % | 83.2 % | 0.82 | 100 % | | | | |

Sub-periods (CAGR / max drawdown / Sharpe):

| | first two thirds (2017-09 → 2023-09) | last third (2023-09 → 2026-10) |
|---|---|---|
| full | 68.1 % / 48.5 % / 1.37 | 28.5 % / 29.4 % / 0.97 |
| risk | 13.5 % / 11.2 % / 1.29 | 4.7 % / 5.7 % / 0.84 |
| buy & hold | 33.2 % / 83.2 % / 0.76 | 47.7 % / 53.0 % / 1.07 |

Per year, sizing full vs buy & hold (exposure):
2017 (from Sep) +127 / +197 % (60 %) · 2018 **−14 / −73 %** (13 %) · 2019 +75 / +94 % · 2020 +288 /
+302 % · 2021 +104 / +60 % · 2022 **−36 / −64 %** (19 %) · 2023 +63 / +156 % · 2024 +59 / +121 % ·
2025 +3 / −6 % · 2026 (to Oct) +8 / −2 %.

Trades (full): 45 closed; losers average −6.5 % over 11 days, winners +40.2 % over 52 days; median
trade −0.23R, best +60.5R; exits 32 channel and 13 stop; longest losing streak 7.

## Evaluation against the pre-declared rules

| rule | sizing full | sizing risk |
|---|---|---|
| 1. max drawdown ≤ ½ of buy & hold (≤ 41.6 %) | **fail: 48.5 %** (2021-10-20 → 2022-12-16) | pass: 11.2 % |
| 2. Sharpe ≥ buy & hold (0.82) | pass: 1.25 | pass: 1.16 |
| 3. CAGR > 0 in both parts | pass: 68.1 % / 28.5 % | pass: 13.5 % / 4.7 % |
| 4. CAGR > 0 in ≥ 75 % of the grid rows | pass: 24 / 24 | pass: 24 / 24 |
| 5. rules 1–3 with costs doubled | rule 1 fails (49.4 %); 2–3 pass (Sharpe 1.22) | pass (11.5 %, 1.14, 13.3 % / 4.4 %) |

**Sizing risk passes all five rules. Sizing full passes 2–5 and misses rule 1:** its drawdown is
58 % of buy & hold's, against the declared limit of 50 %.

Grid (`btc_binance_full/summary.json`, 24 rows per sizing, common window from 2018-03 after the
200-day warmup): sizing full CAGR 14.5–43.2 %, max drawdown 35–66 %, Sharpe 0.54–1.12. The declared
20 / 10 row (fixed in advance, not picked) happens to be the best of the 24. Its neighbours 30 / 10,
55 / 10 and 20 / 20 reach Sharpe 0.93–1.04, so the result does not hinge on it. The regime filter (SMA 200) lowers the drawdown
in most rows and changes the CAGR little. Costs barely matter: about 5 round trips a year.

## What the numbers say

- **The trend effect is there and is not a parameter artifact.** Every one of 48 grid rows is
  positive after costs. The win rate is below 50 %, and the result comes from a few long
  trends. In 2018 and 2022 the system was out of the market most of the time.
- **It weakened recently.** In the last third the system made 28.5 % a year against 47.7 % for
  buy & hold, with a lower Sharpe (0.97 vs 1.07): the 2023–2024 rally was entered late and
  shaken out often. On Bitstamp 2021-09 → 2026-10 it is still slightly ahead (Sharpe 0.63 vs
  0.52, drawdown 46 % vs 77 %).
- **Sizing risk at 1 % is very defensive.** A position is 6–25 % of equity (median 13 %). It returns
  10.5 % a year with an 11 % drawdown. Raising `risk_pct` would scale both, but that was not part
  of the declared test.
- Bitstamp from 2011 (context only): full 87.3 % CAGR / 70.3 % drawdown vs buy & hold 84.2 % /
  84.9 %. The early years are thin and gappy markets.

## Limits

One market (BTC), one dataset per period. Daily bars cannot show the order of events within a
day (the stop is always checked first). Fills are at the daily open with the configured
slippage. Spot only: Tabdeal futures funding is unknown and not modelled, and there are no
shorts. Taxes and an unreachable exchange at the open are not modelled. Past results do not
predict future returns.
