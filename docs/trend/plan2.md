# System B — plan 2: higher risk, and futures (written 2026-10-07, before any run)

Follow-up to `report.md`. Committed before the engine extensions below were run on real data.
Every variant is listed here; nothing is added or dropped after seeing results. Strategy rules
not named here stay as declared in `docs/TREND_STRATEGY.md` (entry 20, exit 10, stop 2 × ATR(20),
no regime filter, pyramiding off).

## Part A — higher risk per trade (spot)

- Data: `data/binance_vision_BTCUSDT_1d.csv` (spot, 2017-08 → 2026-10), Tabdeal costs (taker
  0.095 % + slippage 0.0326 % per fill), the same as report 1.
- Variants: `sizing=risk`, `risk_pct` ∈ {0.02, 0.03, 0.05}, `max_exposure=1` (spot: no
  leverage, so the cap binds when the stop is tight).
- Judged by rules 1–5 of `docs/TREND_STRATEGY.md`. Also reported: the share of entries where the
  exposure cap cut the size.

## Part B — USDT-M perpetual futures

- Prices: Binance USDT-M perpetual BTCUSDT daily klines (data.binance.vision), 2020-01-01 →
  2026-10-06. Tabdeal's own futures history is ~300 days and too short.
- Funding: Binance's actual 8-hourly BTCUSDT funding rates (same archive), summed per UTC day
  and charged on the position's notional at that day's close: longs pay a positive rate,
  shorts receive it. This is a proxy: Tabdeal publishes no funding rate.
- Fees: Tabdeal futures taker 0.095 % + slippage 0.0326 % per fill (`config/runtime.yaml`).
- Margin: cross margin on the whole equity, maintenance rate 0.5 % (`smc.maint_margin_rate`).
  A day whose adverse extreme reaches the liquidation price before the stop closes the
  position there, and the remaining maintenance margin is lost as well.
- Short side (B2): the mirror of the long rules. Enter on a close below the lowest low of the
  previous 20 days; stop 2 × ATR above the fill; exit on a close above the highest high of the
  previous 10 days.
- Variants: B1 long only and B2 long + short, each with `risk_pct` ∈ {0.01, 0.02, 0.03} and
  leverage cap `max_exposure=3`. That makes 6 runs.
- Reference on the same days: the spot variant (`risk_pct=0.01`, long only, `max_exposure=1`,
  no funding), and buy & hold (unlevered, no funding).
- Rules: 1–5 as declared, against buy & hold on the same days, with sub-periods = the first
  two thirds and the last third of this window. Plus:
  6. no liquidation in the run;
  7. B2 only: the short trades together have a positive net result (otherwise shorts add only
     risk).

## Multiple testing

Part A adds 3 variants and part B adds 6, after the 2 of report 1. With 11 variants, a single
borderline pass means little; the conclusion will weigh the pattern across variants, not the
best row.
