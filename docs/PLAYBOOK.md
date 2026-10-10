# Playbook: four 1h strategies on the chart

The chart's **Strategy** menu (top bar, next to the timeframes) runs one of four rule-based
strategies on the shown market's closed 1h bars, long and short. Source: the guide
"راهنمای چهار استراتژی معامله‌ی کریپتو در تایم‌فریم ۱ ساعته" (2026-10-09). Code:
`src/sp2l/playbook/` (rules, simulation), `GET /api/playbook`, `web/chart/playbook.js`.

Picking a strategy switches the chart to 1h and draws:
- its lines (4h EMA 200 always; Donchian channels; EMA 50 + ADX in its own pane; the anchored
  VWAPs);
- every setup of the history window as a Long / Short position from its entry to its target or
  stop (green: target zone, red: stop zone), its result in R after costs at the exit, ▲ / ▼ at
  the signal; orders that never filled as a dashed line;
- the **current setup** (a resting order or an open position), highlighted with "NOW", and the
  pattern behind it (equal lows, sweep, BOS, FVG, order block, anchor, 48-bar level).

The panel under the chart: the current setup (entry, stop, target, R:R, valid until; **Draw as
position** puts it on the chart as a Long / Short drawing for the Trade ⚡ button — nothing is
ever sent by the playbook itself), the **checklist** of every rule on the last closed bar for long
and short (what is met, what is missing), the **backtest** of the window (30 / 90 / 180 / 365
days), every setup (click: shown on the chart), and **Scan markets**: the strategy on every
market, those with a live setup first. A new setup on the shown market raises an alert (toast;
sound / notification when alerts are on).

## Rules (long; short is the mirror)

Common: closed 1h bars only, orders act from the next bar; the 4h filter (switchable) wants the
close above the EMA 200 of the last **closed** 4h bar; the stop must be 0.7–3 % from the entry,
else the setup is shown as rejected and not traded. One setup at a time per strategy.

1. **Previous-high breakout, Donchian 26-bar exit, long only.** The previous high: the highest
   swing high (5 bars each side, as the chart's HH / LH labels; of equal highs the first) of the
   last 240 bars that no close has gone above since. It stays where it is until price closes
   above it — the guide's 48-bar high dropped as soon as an old high left its 48-bar window, so
   an entry could come below the real previous high. Signal: a **bullish candle whose body
   crosses the previous high** (opens at or below it, closes above it). Entry: the next open.
   Stop: entry − 2 N, N = ATR(20) (Wilder) of the signal bar, fixed. Exit: a close below the
   lowest low of the previous 26 bars, at the next open; or the stop. No shorts. (The guide:
   48-bar high / 24-bar low, both sides; changed after the BTC tests below.)
2. **EMA 50 pullback.** EMA 50 above the 4h EMA 200, close above it, EMA 50 above its value 5 bars
   ago, ADX(14) > 20; ≥ 2 ATR(14) from the highest high of the previous 15 bars to the signal low;
   signal candle: low ≤ EMA 50, close > EMA 50, bullish. Buy stop at its high, valid 3 bars,
   cancelled on a close below its low. Stop: lowest low of the last 3 bars − 0.5 ATR. Target 3R.
3. **Liquidity sweep + BOS + FVG / OB.** Equal lows: two swing lows (3 bars each side) within
   0.25 ATR in the last 72 bars, not traded below since; swept by a wick below with a close above
   both. Within 12 bars: a close above the swing high standing at the sweep (BOS), the move
   carrying a candle with a body ≥ 1.5 ATR and an FVG ≥ 0.5 ATR around it. Order block: the last
   bearish candle before that candle; limit at its body top, valid 24 bars (our choice: the guide
   sets none); void if the target trades first ("missed") or a close below the stop. Stop: the
   sweep's low − 0.1 ATR. Target: the highest high from 24 bars before the first equal low to the
   sweep (our reading of "the high of the previous move"); R:R ≥ 2.
4. **Anchored VWAP pullback.** Anchor (mechanical, no hindsight): the lowest low of bars
   [i − 120, i − 12] after a decline of ≥ 3 ATR from the highest high of the 48 bars before it,
   not traded below since. VWAP (typical price × volume) from the anchor rising over 5 bars, close
   above it, a close > 1 ATR above it in the last 15 bars; signal candle: low ≤ VWAP + 0.5 ATR,
   bullish, close above the VWAP. Buy stop at its high, valid 3 bars, cancelled on a close below
   the VWAP or the signal low. Stop: min(signal low, VWAP) − 0.5 ATR. Target 2R.

## Backtest model

Fills: market entry at the next open; a stop entry at its price or the open when the bar gaps
through it; a limit at its price or the open. On the fill bar only the stop counts; stop and
target in one bar: the stop; a gap through the stop exits at the open. Costs from `costs` in the
config: taker fee + slippage allowance for market / stop entries and stop / rule exits, maker fee
for limit entries and targets, funding at every funding time held. R = the move to the stop at the
fill; results are after costs. Tested: no look-ahead (a setup is the same whatever bars come
later), the 4h EMA from closed 4h bars only, each rule (`tests/unit/test_playbook.py`).

## What a test on real data said (not a promise)

**Strategy 1 on BTC, 2020-01 → 2026-10** (Binance BTCUSDT 1h, ~6.7 years, Tabdeal's costs, 4h
filter on).

The entry level: the previous high (body crossing it) against the 48-bar high, all long only with
the 26-bar exit:

| Entry level | Trades | Win | Total R | Avg R | PF | Max DD R | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|
| 48-bar high (the previous version) | 298 | 28 % | +134.3 | +0.45 | 1.55 | 32.3 | −6.5 | +1.1 |
| previous high, swing highs of the last 72 bars | 254 | 26 % | +151.9 | +0.60 | 1.75 | 24.9 | +7.9 | −3.2 |
| previous high, last 120 bars | 209 | 27 % | +157.2 | +0.75 | 1.97 | 19.7 | +8.7 | +2.0 |
| **previous high, last 240 bars (in use)** | 173 | 28 % | +152.8 | +0.88 | 2.15 | 15.0 | +9.9 | +6.7 |
| previous high, last 480 bars | 144 | 26 % | +113.4 | +0.79 | 1.98 | 16.7 | +1.2 | +8.0 |
| previous high, never forgotten | 35 | 31 % | +24.5 | +0.70 | 1.97 | 8.4 | +1.3 | 0 |

The previous high halves the drawdown and doubles the average trade; 120–240 bars are a plateau
(not one lucky value); kept for ever, the level sits at a past top for months (none of 2022 or
2023 traded). The same 240-bar rule on the 8 markets over 13 months below: 165 trades, +59.4 R
(the 48-bar high: 338 trades, +13.5 R), better on 7 of 8 markets, worse on ADA. Yearly on BTC:
2020 +58.6, 2021 −1.6, 2022 −1.8, 2023 +59.3, 2024 +21.7, 2025 +9.9, 2026 +6.7 R.

The earlier step, the guide's 48/24 against long only and 26-bar channels:

| Variant | Trades | Win | Total R | Avg R | PF | Max DD R | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|
| 48/24 long + short (the guide) | 561 | 27 % | +123.2 | +0.22 | 1.27 | 37.5 | −21.5 | +13.2 |
| 48/24 long only | 305 | 29 % | +133.7 | +0.44 | 1.55 | 30.1 | −6.3 | +3.2 |
| 48/26 long only | 298 | 28 % | +134.3 | +0.45 | 1.55 | 32.3 | −6.5 | +1.1 |
| 26/24 long only | 376 | 27 % | +122.6 | +0.33 | 1.41 | 28.3 | −7.1 | +0.2 |
| 26/26 long only | 366 | 27 % | +122.3 | +0.33 | 1.41 | 31.1 | −8.1 | −1.8 |

Long only doubles the average trade and cuts the drawdown; a 26-bar exit is as good as 24 (the
difference is noise); a 26-bar entry is worse. Over the last 13 months alone (BTC falling) long +
short did better (+10.4 R against +1.2 R): shorts pay in a falling market, longs in a rising one.
2020 alone gave ~+65 R of the total in every variant.

The guide's original rules (Donchian 48/24 long + short) on ~13 months of public 1h / 4h candles (Binance spot, 2025-09 → 2026-10; BTC,
ETH, SOL, XRP, BNB, ADA, LINK, AVAX), Tabdeal's fees from the config, 4h filter on:

| Strategy | Trades | Total (R, after costs) | Average |
|---|---|---|---|
| 1 Donchian 48/24 | 786 | +56.9 | +0.07 |
| 2 EMA 50 pullback | 327 | −15.7 | −0.05 |
| 3 Sweep + BOS + FVG / OB | 1 | −1.1 | — (the rules rarely all meet: ~10 complete patterns per market a year, most then filtered or missed) |
| 4 Anchored VWAP | 800 | −62.8 | −0.08 |

Spread over markets: Donchian from −13.2 R (LINK) to +35.8 R (SOL); EMA 50 from −15.1 R (BTC)
to +7.9 R (SOL); anchored VWAP from −21.4 R (LINK) to +8.3 R (AVAX). Win rates 20–41 %, drawdowns
of 7–34 R. One year, one regime: the guide's own
caveat stands — paper-trade before real money, and do not tune the numbers to the past.
