# System B — report 2: higher risk, and futures (2026-10-07)

Plan: [`plan2.md`](plan2.md), committed in 5dc17eb before the runs. Every planned variant is
reported below. One extra descriptive run (futures without funding) was added after seeing the
results and is labelled as such. Outputs: `docs/trend/plan2/<variant>/` (`summary.json` with the
grid, `trades.csv`, `equity.csv`).

## Part A — higher risk per trade (spot BTCUSDT, 2017-09-06 → 2026-10-06)

Tabdeal costs, long only, no leverage (`max_exposure=1`). Buy & hold over the same days: CAGR
37.9 %, max drawdown 83.2 %, Sharpe 0.82.

| risk per trade | CAGR | max drawdown | Sharpe | first ⅔ / last ⅓ CAGR | Sharpe last ⅓ (B&H 1.07) | size capped | 2× costs: CAGR / DD / Sharpe | rules 1–5 |
|---|---|---|---|---|---|---|---|---|
| 1 % (report 1) | 10.5 % | 11.2 % | 1.16 | 13.5 / 4.7 % | 0.84 | 0 % | 10.3 / 11.5 / 1.14 | all pass |
| 2 % | 19.4 % | 17.6 % | 1.24 | 24.9 / 9.2 % | 0.87 | 0 % | 19.0 / 18.1 / 1.21 | all pass |
| 3 % | 27.6 % | 22.5 % | 1.28 | 35.2 / 13.6 % | 0.89 | 0 % | 26.9 / 23.2 / 1.26 | all pass |
| **5 %** | **41.8 %** | **31.5 %** | **1.33** | 52.5 / 22.6 % | 0.96 | 15 % of entries | 40.7 / 32.2 / 1.30 | **all pass** |

Grid (rule 4): 24 / 24 rows positive at every risk level.

- Return and drawdown grow with the risk, and the Sharpe holds (1.16 → 1.33). At 5 % the spot
  cap (no leverage) cut 15 % of the entries, which keeps the drawdown down.
- **At 5 % the CAGR is above buy & hold (41.8 vs 37.9 %), with about ⅜ of its drawdown (31.5 vs
  83.2 %).**
- Per year at 5 % (vs buy & hold): 2018 −6.5 / −73 %, 2020 +210 / +302 %, 2021 +83 / +60 %, 2022
  −22.7 / −64 %, 2023 +53 / +156 %, 2024 +49 / +121 %, 2025 −1.3 / −6.3 %.
- Same caveat as report 1: in the last third (2023-09 →) every variant's Sharpe is below buy &
  hold's (0.84–0.96 vs 1.07). Rule 3 only asks for a positive CAGR there, so the rules pass.

## Part B — USDT-M perpetual futures (BTCUSDT perp, 2020-01-21 → 2026-10-05)

Binance perp daily prices and Binance's actual funding as a proxy for Tabdeal. Tabdeal futures
fees, cross margin (maintenance 0.5 %), leverage cap 3. Buy & hold over the same days: CAGR
40.5 %, max drawdown 76.7 %, Sharpe 0.87.

| variant | CAGR | max DD | Sharpe | first ⅔ / last ⅓ CAGR | funding paid (of 10,000) | liquidations | shorts: trades / net | rules |
|---|---|---|---|---|---|---|---|---|
| long only, 1 % | 8.2 % | 13.0 % | 0.87 | 11.4 / 2.1 % | 1,964 | 0 | – | 1–4, 6 pass; **5 fails** (Sharpe 0.85 < 0.87 at 2× costs) |
| long only, 2 % | 14.9 % | 20.6 % | 0.94 | 20.8 / 4.1 % | 4,430 | 0 | – | all pass |
| long only, 3 % | 20.7 % | 26.4 % | 0.98 | 28.9 / 5.8 % | 7,430 | 0 | – | all pass |
| long + short, 1 % | 7.8 % | 11.7 % | 0.79 | 11.1 / 1.6 % | 1,897 | 0 | 30 / −414 | **2, 5, 7 fail** |
| long + short, 2 % | 14.1 % | 18.2 % | 0.83 | 20.1 / 2.9 % | 4,247 | 0 | 30 / −1,365 | **2, 5, 7 fail** |
| long + short, 3 % | 19.2 % | 24.1 % | 0.85 | 27.7 / 3.9 % | 7,055 | 0 | 30 / −2,984 | **2, 5, 7 fail** |
| reference: spot-like 1 % (no funding, no leverage) | 10.3 % | 11.2 % | 1.10 | 14.4 / 2.7 % | 0 | 0 | – | (reference) |

Grid: 24 / 24 positive in every futures variant.

Added after the results (descriptive, not a planned test): the same long-only futures runs
**without funding** give 1 %: 10.3 % / Sharpe 1.10, 2 %: 18.7 % / 1.17, 3 %: 26.1 % / 1.22.

- **Funding is the main cost of futures.** A trend follower is long exactly when the crowd is
  long and funding is high (2020: 17 %, 2021: 31 % a year for a long). It costs 2–5 CAGR points
  and about 0.2 of Sharpe. Fees, even doubled, matter less.
- **The short side loses.** It has 30 trades with a 37 % win rate and a negative net result in
  both sub-periods, at every risk level. BTC's upward drift and its sharp rebounds hit
  breakdown shorts. Adding shorts lowered the Sharpe below buy & hold.
- **Leverage was never used.** With risk sizing up to 3 % the largest position was 0.72 × equity,
  so no liquidation could happen. On spot, the 1 × cap first binds at 5 % risk (15 % of entries).
- Both the futures window (from 2020) and its last third (2024-07 →) are weaker: Sharpe 0.47–0.49
  in the last third, against 0.62 for buy & hold.

## Conclusion

1. **Higher risk on spot works best.** Every level from 2 % to 5 % passes all rules. 3 % gives
   27.6 % CAGR with a 22.5 % drawdown. 5 % beats buy & hold's return with ⅜ of its drawdown.
   5 % is where the spot cap starts to bind.
2. **Futures add nothing here, and shorts hurt.** Long-only futures pass at 2–3 % but are
   strictly worse than the same rules on spot, because of funding. Long + short fails three
   rules. On Tabdeal the actual funding is unknown; it was taken from Binance.
3. Across 11 variants the pattern is consistent: the long trend signal is robust, the short
   signal is not, and costs are minor except funding. The recent period (2023/24 →) is weaker
   than buy & hold in Sharpe for every variant. That is the main risk going forward.

Suggested configuration for the live (paper) step: **spot, long only, `risk_pct` 0.02–0.03**, with
5 % as the aggressive end. Do not trade the short side.

## Limits

As in report 1: BTC only; daily bars (the stop is checked first within a day); fills at the open
with the configured slippage. Binance funding stands in for Tabdeal's. October 2026 funding is
not published yet, so its 5 days are charged none. Futures history starts in 2020.
