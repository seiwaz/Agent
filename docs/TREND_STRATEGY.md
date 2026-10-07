# System B — daily Donchian trend following (TREND-1.0)

Why: four pre-registered studies (`docs/research/`) found no SMC / ICT entry edge on Tabdeal
after costs. System B tests a different hypothesis with the strongest published evidence for
BTC: time-series momentum on daily bars (multi-week trends persist), traded long only on spot.

Code: `src/sp2l/trend/` (`model.py` parameters, `backtest.py` engine and statistics, `data.py`
loaders). Tests: `tests/trend/test_trend.py`. Backtest only: nothing in the live SMC engine,
its database tables or the dashboard changed.

## Rules

All decisions use **closed UTC daily bars**. A signal is decided on the close of day *t* from
bars up to *t*; the order fills at the **open of day *t* + 1** (market order).

| | rule | key (default) |
|---|---|---|
| Entry | close of *t* > highest high of the previous `entry_len` days (excluding *t*) | `entry_len` (20) |
| Regime filter (optional) | and close of *t* > SMA(`regime_ma`) of closes | `regime_ma` (0 = off) |
| Initial stop | fill − `stop_atr` × ATR(`atr_len`, Wilder) of day *t* | `stop_atr` (2), `atr_len` (20) |
| Stop handling | resting from the fill on (also on the fill day): low ≤ stop → exit at the stop, or at the open if the day opened below it (gap) | |
| Exit | close of *t* < lowest low of the previous `exit_len` days → sell at the next open | `exit_len` (10) |
| Order of checks per day | fills at the open → stop (intraday) → mark at the close → signals for tomorrow | |
| Sizing: `risk` | quantity = `risk_pct` × equity / (`stop_atr` × ATR): a stop-out loses `risk_pct` of equity (1R) | `risk_pct` (0.01) |
| Sizing: `full` | all equity in (spot timing: in the market or in USDT) | |
| Exposure cap | notional ≤ `max_exposure` × equity (1 = spot, no leverage) | `max_exposure` (1) |
| Pyramiding (off) | add a unit when close ≥ last fill + `add_atr` × N (N = ATR at the first entry), up to `max_units`; every unit's stop moves to last fill − `stop_atr` × N | `max_units` (1), `add_atr` (1) |
| Short side | none (spot, long only) | |

Costs: every fill pays `fee` (default `costs.taker_fee`) and `slippage` (default
`costs.slippage_allowance`), as fractions of the notional / price, against the trade. These
defaults are Tabdeal **futures** values; on spot, pass the actual spot fee with `--fee`.

## Data

The rules need years of daily bars: a 20 / 10-day channel trades about 6–10 times a year, so
Tabdeal's ~300 days of 1-minute history give too few trades to judge anything.

```sh
# long history (public, read-only, no key): Binance from 2017-08, Bitstamp from 2011-08
uv run python scripts/fetch_daily.py --source binance --symbol BTCUSDT
uv run python scripts/fetch_daily.py --source binance_vision --symbol BTCUSDT  # where the API says 451
uv run python scripts/fetch_daily.py --source bitstamp --symbol btcusd
# any other daily CSV works too (TradingView export, ...): date, open, high, low, close[, volume]
```

## Run

```sh
uv run python -m sp2l trend-backtest --csv data/binance_BTCUSDT_1d.csv --grid --out docs/trend/btc
uv run python -m sp2l trend-backtest --csv data/binance_BTCUSDT_1d.csv --set sizing=full --set regime_ma=200
uv run python -m sp2l --symbol BTCUSDT trend-backtest --days 290      # Tabdeal history (short)
uv run python -m sp2l trend-backtest --csv ... --fee 0.0025 --slippage 0.0005   # spot fees
```

Output (JSON, and with `--out`: `summary.json`, `trades.csv`, `equity.csv`):
- `data`: bars, first / last day, missing days, repaired rows.
- `full`, `first_two_thirds`, `last_third`: strategy vs buy & hold over the same days (buy & hold
  bought on the close of the first signal bar, with the same costs): total return, CAGR,
  volatility, Sharpe and Sortino (rf 0, 365 days), max drawdown with its dates, Calmar; exposure;
  trade statistics (win rate, R per trade, profit factor, average win / loss, holding time,
  exits by kind, fees, the longest losing streak).
- `yearly`: calendar-year return of both, and the exposure.
- `grid` (`--grid`): `entry_len` {10, 20, 30, 55} × `exit_len` {5, 10, 20} × `regime_ma`
  {0, 200} × `sizing` {risk, full}, all over one common window. It is a **sensitivity** table:
  it shows whether the result depends on the exact numbers. It is not for picking the best row.

## Evaluation rules (fixed before the first run on real data)

Results: [`docs/trend/report.md`](trend/report.md).

The defaults above (20 / 10 / 2 × ATR(20), filter off) are the ones under test. They come from
the literature (Donchian / Turtle rules), not from this data. Judged on BTC daily data from
2017 on, with Tabdeal costs, `sizing=full` (comparable to buy & hold) and `sizing=risk`:

1. **Risk:** the max drawdown is at most half of buy & hold's over the same days.
2. **Risk-adjusted return:** Sharpe ≥ buy & hold's on the full period.
3. **Stability:** CAGR > 0 in both the first two thirds and the last third.
4. **Robustness:** CAGR > 0 in at least 75 % of the grid rows of the same sizing.
5. **Costs:** rules 1–3 still hold with fee and slippage doubled.

Total return above buy & hold is **not** required: the hypothesis is that trend following keeps
most of the long-run gain while avoiding most of the 75–85 % drawdowns. If rules 1–5 hold, the
next step is the live wiring (signals at the daily close, paper only, as the SMC engine does).
If they do not, the result is reported as it is, and nothing is built on it.

## Not modelled

Spot borrowing (only with `max_exposure` > 1, unused), funding (spot has none), the exchange
being unreachable at the open, partial fills, taxes. Daily bars do not show whether the stop or
the close came first within a day; the stop is always checked first (conservative).
