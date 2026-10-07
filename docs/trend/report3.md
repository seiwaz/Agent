# System B — report 3: levers to raise the return (2026-10-07)

Plan: [`plan3.md`](plan3.md), committed in 486888b before any run. Code:
`src/sp2l/trend/ensemble.py` (tests `tests/trend/test_ensemble.py`); study: `uv run python
scripts/trend_plan3.py` → `docs/trend/plan3/results.json`. Data: `data/universe/` (54 Binance
spot coins, 2017-08 → 2026-10, including delisted LUNA, FTT, FTM, MATIC, XMR, WAVES, EOS and the
BCH forks).

## Result: no lever beat the baseline on discovery, so the holdout was not used

Discovery 2017-09-06 → 2022-12-31, Tabdeal costs, spot. BTC buy & hold: CAGR 27.1 %, max drawdown
83.2 %, Sharpe 0.70.

| id | lever | CAGR | max DD | Sharpe | Calmar | avg exposure | candidate? |
|---|---|---|---|---|---|---|---|
| **V0** | System B 20 / 10, risk 3 % | 35.9 % | 22.5 % | **1.43** | **1.60** | 17 % | (baseline) |
| V1 | + pyramiding (4 units, ½ N) | 53.3 % | 37.9 % | 1.26 | 1.41 | 29 % | no |
| V2 | BTC ensemble 9 lookbacks, vol target 25 % | 18.5 % | 23.1 % | 1.26 | 0.80 | 13 % | no |
| V3 | BTC ensemble, vol target 50 % | 31.7 % | 38.7 % | 1.22 | 0.82 | 24 % | no |
| P1 | top-10 coins, ensemble, coin target 50 % | 15.3 % | 22.6 % | 0.99 | 0.67 | 13 % | no |
| P2 | top-10 coins, coin target 100 % | 23.4 % | 36.2 % | 0.94 | 0.65 | 21 % | no |

The rule asked for Sharpe ≥ 1.53 (V0 + 0.10) and Calmar ≥ 1.60. **No variant met it**, so per the
plan nothing went to the holdout (2023-01 →), which stays unused.

- **Pyramiding** raised the CAGR only by taking more risk. Per unit of drawdown it is worse than
  simply raising V0's risk (table below).
- **Ensemble + vol targeting** (the Zarattini et al. design) had a lower Sharpe than the simple
  20 / 10 channel on this period. Its daily rebalancing is costly: V3 traded 6.3 M of notional
  and paid 8.1 k in costs on 10 k of capital over 5 years.
- **A coin portfolio** did worse than BTC alone. In 2017–2022 the altcoins mostly crashed
  together with BTC (2018, LUNA and FTT in 2022), so diversification bought little. Altcoin
  trends were shorter and costlier than BTC's. This differs from the papers (2015–2024,
  different universe, other rebalancing); our test is survivorship-free and net of Tabdeal costs.

## What does raise the return: more risk on the baseline, and yield on idle cash

Descriptive, on discovery only, added after the planned results. These numbers were not used
to select anything and the holdout was not touched.

| configuration | CAGR | max DD | Sharpe | Calmar |
|---|---|---|---|---|
| V0 risk 3 % | 35.9 % | 22.5 % | 1.43 | 1.60 |
| **V0 risk 5 %** | **53.9 %** | **31.5 %** | **1.48** | **1.71** |
| V0 risk 7 % | 62.3 % | 40.5 % | 1.44 | 1.54 |
| V0 risk 10 % | 64.6 % | 47.5 % | 1.36 | 1.36 |
| V0 full (all-in on each signal) | 70.8 % | 48.5 % | 1.38 | 1.46 |
| V1 pyramiding, risk 2 % (DD like V0 5 %) | 50.0 % | 31.9 % | 1.30 | 1.57 |
| **V0 risk 5 % + 4 %/yr on idle cash** | **58.5 %** | **28.6 %** | **1.57** | **2.05** |

- **Risk 5 % is the efficient point:** it has the highest Calmar of the risk levels. Above it, the
  spot cap (no leverage) binds and each extra point of drawdown buys little return. The full
  period (report 2) agrees: 5 % → CAGR 41.8 %, DD 31.5 %, and 2017–2026 is above buy & hold.
- **Yield on idle cash is the cleanest lever.** At risk 5 % only about 25 % of the capital is in BTC on average; the other ~75 % sits in USDT. At 4 % a
  year, the CAGR rises by ~4.6 points and the drawdown falls (cash earns during the drawdowns).
  The risk is the counterparty: an exchange or lending platform can fail (Celsius, FTX in 2022).
  It also depends on what is available to you; Tabdeal offers no such product we know of.

## Conclusion

1. Across 6 pre-registered variants, none of the literature's levers (pyramiding, ensemble with
   vol targeting, a 10-coin portfolio) improved the **risk-adjusted** return of the plain 20 / 10
   channel on BTC. The simple system is already the efficient one here.
2. **The way to more return is to take more risk with the same system:** risk 5 % per trade on
   spot (≈ 54 % CAGR / 32 % DD on discovery; 42 % / 32 % over 2017–2026). Beyond ~5 % the return
   barely grows while the drawdown does.
3. **Add a yield on idle USDT** if a trustworthy source is available: +4–5 CAGR points at 4 %/year,
   with a lower drawdown.
4. Not recommended from this evidence: pyramiding, futures (report 2: funding), shorts (report
   2), altcoin portfolios.

## Limits

One discovery period (2017–2022) and a fixed candidate list (chosen today, although it includes
delisted coins). The ensemble used the paper's lookbacks but our own rebalancing band; a slower
rebalancing could cut its costs. That would be a new, separate test. The cash-yield overlay
assumes a constant 4 % and no platform failure.
