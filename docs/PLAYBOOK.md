# Playbook: four 1h strategies on the chart

The chart's **Strategy** menu (top bar, next to the timeframes) runs one of four rule-based
strategies on the shown market's closed 1h bars, long and short. Source: the guide
"راهنمای چهار استراتژی معامله‌ی کریپتو در تایم‌فریم ۱ ساعته" (2026-10-09). Code:
`src/sp2l/playbook/` (rules, simulation), `GET /api/playbook`, `web/chart/playbook.js`.

Picking a strategy switches the chart to 1h and draws:
- its lines (4h EMA 200 always; the Donchian channel; EMA 50 + ADX in its own pane; the anchored
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

1. **Donchian Long** (the user's own rules; long only). A Donchian channel of 26 bars as
   TradingView draws it: upper line = the highest high of the last 26 bars (the bar itself
   included), lower line = the lowest low, middle line = their average. Signal on bar i, judged
   on the channel of bar i − 1:
   - uptrend: the last close above the middle line, and the middle line higher than 26 bars
     earlier (the user's "candles above the middle line"; the close alone left no edge);
   - the upper line flat for ≥ 10 bars and the lower line flat for ≥ 5 bars ("for a fairly long
     time" / "for a while with it");
   - a strong bullish candle closes above the flat upper line: body ≥ 60 % of the candle's range
     and ≥ 1 ATR(14).

   Entry: the next open. Exit: the next open after the first close below the middle line. The
   stop placed on the exchange is the lower line at the signal (a safety net: never reached in
   the BTC test); R is measured to it (on BTC ~4.3 % from the entry), so the 0.7–3 % stop filter
   does not apply to this strategy.
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

**Donchian Long on BTC, 2020-01 → 2026-10** (Binance BTCUSDT 1h, ~6.7 years, Tabdeal's costs,
4h filter on): 57 trades, 44 % winners, PF 1.6, +7.3 R (R to the lower line, ~4.3 % away), max
drawdown 2.2 R. In price terms: +48.6 % summed over the trades, +0.85 % a trade on average (best
+9.5 %, worst −4.3 %), held 25 h on average. By year (R): 2020 +3.1, 2021 +0.7, 2022 −1.3, 2023
+4.5, 2024 −0.7, 2025 +1.8, 2026 −0.7. The 8 markets over the last 13 months: 31 trades, −9.5 R.

How the open points were settled (BTC, the same period):

| Reading | Trades | Win | PF | Note |
|---|---|---|---|---|
| uptrend = last close above the middle, body ≥ 50 % of the candle, no 4h filter | 129 | 36 % | 1.05 | break-even after costs |
| + body ≥ 60 % and ≥ 1 ATR | 110 | 36 % | 0.97 | |
| + the middle line rising | 68 | 41 % | 1.25 | |
| + the 4h EMA 200 filter (in use; switchable in the panel) | 57 | 44 % | 1.6 | |

Flat lengths (upper / lower) from 5 / 0 to 20 / 10 bars all stayed near break-even without the
trend and 4h conditions; with them 5–15 / 5 bars gave PF 1.4–1.8.

Against the strategy it replaced (a body crossing the previous high, 26-bar low exit; 173 trades,
+0.83 % a trade, ~+150 % summed): about the same per trade, a third of the trades, so a third of
the total. Kept here for reference (code in git history: commit "Playbook strategy 1: entry on a
bullish body crossing the previous high").

Earlier versions, for the record. The previous-high entry against the 48-bar high (long only,
26-bar exit):

| Entry level | Trades | Win | Total R | Avg R | PF | Max DD R | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|
| 48-bar high | 298 | 28 % | +134.3 | +0.45 | 1.55 | 32.3 | −6.5 | +1.1 |
| previous high, last 240 bars | 173 | 28 % | +152.8 | +0.88 | 2.15 | 15.0 | +9.9 | +6.7 |

The guide's 48/24 against long only and 26-bar channels:

| Variant | Trades | Win | Total R | Avg R | PF | Max DD R | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|
| 48/24 long + short (the guide) | 561 | 27 % | +123.2 | +0.22 | 1.27 | 37.5 | −21.5 | +13.2 |
| 48/24 long only | 305 | 29 % | +133.7 | +0.44 | 1.55 | 30.1 | −6.3 | +3.2 |
| 48/26 long only | 298 | 28 % | +134.3 | +0.45 | 1.55 | 32.3 | −6.5 | +1.1 |

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
