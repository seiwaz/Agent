# System B on futures with up to 20x leverage — descriptive note (2026-10-07)

Not a pre-registered test: a descriptive check of a user question. Data and costs as in report 2
(Binance BTCUSDT perp 2020-01 → 2026-10, Binance funding, Tabdeal taker fee + slippage, cross
margin, maintenance 0.5 %). Rules unchanged (20 / 10 channel, stop 2 × ATR(20), long only).

Distance from entry to the initial stop: median 7.4 %, min 4.2 %, max 16.2 %. A 20x position is
liquidated by a ~5 % adverse move, so it would be liquidated before the stop.

| sizing | CAGR | max DD | Sharpe | liquidations | 10,000 becomes |
|---|---|---|---|---|---|
| all equity × 1x | 32.2 % | 50.5 % | 0.91 | 0 | 65,046 |
| all equity × 2x | 44.2 % | 76.0 % | 0.88 | 0 | 116,315 |
| all equity × 3x | 43.4 % | 88.9 % | 0.85 | 0 | 112,240 |
| all equity × 5x | 12.8 % | 98.2 % | 0.77 | 0 | 22,473 |
| all equity × 10x | −98.8 % | 100 % | | 1 | 0 |
| all equity × 20x | −98.8 % | 100 % | | 1 (first trade, 2020-01-29) | 0 |
| risk 3 %, cap 20x | 20.7 % | 26.4 % | 0.98 | 0 | 35,340 |
| risk 5 %, cap 20x | 30.4 % | 35.4 % | 1.02 | 0 | 59,222 |
| risk 10 %, cap 20x | 47.2 % | 54.5 % | 1.03 | 0 | 134,113 |
| risk 20 %, cap 20x | 60.0 % | 80.1 % | 0.98 | 0 | 234,536 |
| buy & hold (spot) | 40.5 % | 76.7 % | 0.87 | | |

With risk sizing the leverage actually used stays low: notional / equity median 0.68x (max 1.19x)
at 5 % risk, 1.35x (2.39x) at 10 %, 2.71x (4.77x) at 20 %. The Sharpe stays near 1.0 at every
level: leverage scales return and drawdown together, and funding is paid on the larger notional.

On the exchange: with isolated margin, the leverage setting decides the liquidation price. It
must sit beyond the stop, so leverage ≤ ~1 / (stop distance + buffer): ≤ 5x covers even the
widest stop seen (16 %). A higher setting (e.g. 20x isolated) gets liquidated before the stop.
