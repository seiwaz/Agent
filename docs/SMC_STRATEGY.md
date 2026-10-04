# SMC engine — strategy specification (SMC-1.0)

Markets: **BTC/USDT and XRP/USDT**, traded together on **one shared simulated wallet**
(100 USDT). Each market has its own price tick / quantity step (`instruments`).

The SP2L strategy (Spike / P-Gap / Context / Exhaustion, branch `context-v6`) is retired.
This engine trades Smart Money Concepts top-down and **executes on M1**. No orders are
sent: signals and their simulated lifecycle are recorded for review.

## Data (no live warmup)
- Every bar comes from one merged 1-minute series: canonical live candles (`candles_1m`,
  collector) where they exist, otherwise **Tabdeal chart history** (`exchange_m1`), fetched
  on demand for `history_days` (default 35) when the engine starts and topped up every minute.
- Higher timeframes are aggregated from that series in SQL whenever needed, on Tabdeal's own
  grid: 1m–15m UTC-aligned, **1h and 4h on Tehran time** (bars open at hh:30 UTC).
- Only closed bars are analysed. The chart and the engine read the same series with the same
  parameters, so what the dashboard draws is what the engine reasons about.

## Structure (identical on every timeframe)
| Concept | Rule |
|---|---|
| Swing | high (low) strictly beyond `swing_len` bars on the left, not exceeded by `swing_len` on the right; known `swing_len` bars later |
| BOS / CHoCH | a bar **closes** beyond the last unbroken swing: BOS with the trend, CHoCH against it (first break = BOS) |
| Order block | on a break, the candle with the extreme low (bullish) / high (bearish) between the broken swing and the break (max `ob_lookback` bars); zone = its full range |
| Fair value gap | 3-bar imbalance (`low[i] > high[i-2]` / `high[i] < low[i-2]`) larger than `fvg_min_atr` × ATR |
| Zone status | TESTED when traded into. Invalid (removed from chart and model): an OB once a bar **closes** through its far side, an FVG once it is **completely filled** (a wick reaches its far side; `fvg_fill: close` keeps the close rule); expired after the timeframe's lookback |
| Liquidity (display) | unswept swing highs (BSL) / lows (SSL) |
| Premium / discount (display) | last swing high/low range and its equilibrium |

## Entry model
1. **Bias** — trend of `bias_tf` (4h) must match the trade direction.
2. **POI** — an unmitigated OB / FVG of `poi_tfs` (1h, then 15m) with the bias must overlap the
   M1 order block of the trigger (price reacted inside the higher-timeframe zone).
3. **Trigger** — an M1 BOS / CHoCH in the bias direction.
4. **Execution (M1)** — `entry_mode: market` enters at the trigger candle's close (limit modes:
   proximal / mid / distal of the M1 block or of the POI). Stop beyond the POI and the M1 block
   plus `sl_buffer_atr` × ATR (`sl_mode: poi`). Target = nearest unswept liquidity on any
   analysed timeframe (or the M1 leg extreme) that pays ≥ `min_net_rr` **after fees and stop
   slippage** (`tp_mode: liquidity`; `rr` = exactly that R with liquidity beyond).
5. **Quality** — score from fresh POI, CHoCH, liquidity sweep, M1 displacement FVG, OB/FVG
   confluence at the POI, `confirm_bias_tf` agreement; `score >= min_score`, `require`d factors
   present, stop ≤ `max_risk_pct`, advisory size ≤ `max_leverage` (`account_usdt` × `risk_pct`).
6. Capacity: `max_active` (1) position per market and `max_positions` (2) across markets; a
   POI is traded once (backtest); triggers are never created retroactively (only within 3
   minutes of their close).

## Shared wallet
- Sizing at entry: risk `risk_pct` (1 %) of the current wallet balance; quantity rounded down
  to the market's step. The position's margin (notional / `max_leverage`, cross 10x) must fit
  the free balance (balance minus every open position's margin, on any market); otherwise the
  quantity is reduced to fit (MARGIN_LIMITED) or the trade is skipped (NO_FREE_MARGIN).
- Booking at exit: PnL = qty × move − entry fee − exit fee, appended to `smc_wallet_ledger`
  with the running balance. Open positions are marked to the last price for equity.
- A new wallet starts (the old one is closed, never rewritten) when the initial balance or
  the set of markets changes.

## Lifecycle (M1 bars)
PENDING → OPEN → TP / SL, or EXPIRED (no fill in `pending_expiry_min`), MISSED (target before
fill), TIMEOUT (market exit after `max_hold_min`). Conservative intrabar reading: stop beats
target in the same minute; no target credit in the fill minute. R = net PnL / (stop distance +
entry fee + stop exit fee + slippage), so a full stop is exactly −1R.

## Backtest findings, BTC and XRP (2026-10-04, 30 days, after fees, FVG = fill rule)
| Market | Trades | Win rate | Total | Profit factor |
|---|---|---|---|---|
| BTC/USDT | 60 | 20 % | −30.9R | 0.26 |
| XRP/USDT | 42 | 17 % | −21.3R | 0.35 |

## Earlier backtest findings (2026-10-04, XAUT/USDT, Tabdeal fees 0.08 % maker / 0.095 % taker)
Fees are ~7 USDT per unit round trip, about the size of a typical 15m–1h structural stop.
Over 60 days no tested variant was profitable after fees (default: PF ≈ 0.15–0.26); before
fees the default model is close to break-even (PF ≈ 0.95). The dashboard's Performance page
re-runs the backtest of the parameters in force. Treat live signals as experimental.

## Parameters
All in `config/*.yaml` → `smc` (defaults: `src/sp2l/smc/model.py`), listed on the dashboard's
Strategy page with their hash. Every signal stores the hash of the parameters that made it.
