## D. Findings (2026-10-05) — decision needed, nothing changed

All numbers below come from the sections that follow (scripts/diag_smc.py). The **current**
parameters closed only 3 trades in 270 days (BTC 2, XRP 1, all stops, −3.00R): too few to
diagnose, so the per-trade statistics use the **wide** population (same rules with
`require_sweep: false`, 35 closed trades, −20.76R).

### Confirmed bugs (fix proposals, not applied)
1. **XRP data hole.** 2026-05-03 00:02 .. 05-05 00:00 UTC (2,877 minutes) is missing in the
   database; Tabdeal's chart has it (122 bars in a 2-hour sample). `history.ensure_history`
   only extends the head and the tail, so an inner hole stays. Proposal: an inner-gap repair
   pass (re-fetch every hole longer than a few minutes), then re-run the backtests.
2. **First bar of each fetch chunk.** Tabdeal's chart API returns a different open / high
   for the first bar of every request (BTC 2026-06-22 14:00: open 65,530.2 when the request
   starts at 14:00, 65,525.0 when it starts at 13:58). The database stores the first minute of
   each 2-day chunk that way (XRP 2026-02-22 00:00 open 1.43009 vs 1.43037). Impact: one
   minute per two days. Proposal: fetch each chunk with a few minutes of overlap.
3. **Target fills too well (model gap).** The backtest credits the exact TP price; on Tabdeal a
   take-profit is a trigger executed at market (owner's screenshot: trigger 86,132, average
   fill 86,111.9, −0.023 %). Proposal: charge the slippage allowance on TP exits too (or model
   a resting maker TP if that is what the owner uses).

Checked and correct: limit fill on touch vs through changes **0** trades (entries are traded
through); the stop-before-target and no-target-in-the-fill-minute rules decided **0** trades;
fees are charged once, on notional, maker in / taker out, and a stop is exactly −1R; no
look-ahead (tests); no duplicate minutes, no inconsistent OHLC; 1,920 re-fetched minutes match
except item 2.

### What drives the bot's losses (wide population, measured)
1. **The entries lose before any cost.** Gross result −16.7 stop distances on 35 trades (BTC
   −2.75, XRP −13.92); 32 of 35 end at the stop; win rate 6–12 %; shorts 19 trades, 0 wins,
   −19.0R. Costs are not the main problem.
2. **The 1h / 4h grid.** The same rules on UTC-aligned 1h / 4h bars instead of Tabdeal's Tehran
   grid (hh:30): −20.76R → −7.93R (**+12.8R**); current population −3.00R → +3.01R (+6.0R).
   The largest single effect: where the bars are cut changes swings, OBs, FVGs and sweeps.
3. **The 4h bias filter picked the losing side in this period.** Trades with the bias:
   33, −18.76R; the same setups against the bias: 20, +1.53R (bias check off: **+3.5R**).
4. **Costs.** Median stop 0.47 % (BTC) / 0.81 % (XRP) of price, minimum 0.15 %; costs are
   20–31 % of 1R; 14.9 stop distances in total; without any cost **+2.3R**.
5. **Far target, no management.** 11 of 32 losers first reached +1R and 4 reached +2R before
   their stop (TP = the HH / LL of the leg).

### Owner vs bot
Not measurable yet: `manual_trades.csv` is missing. One data point: the owner's BTC long
closed at TP on 2026-10-05 01:32 Tehran (22:02 UTC); for that move the bot had no 1h setup,
and the 5m version of it (OB 85,381–85,413) would only have been entered on the retest at
08:54 Tehran, after the owner had already taken profit — the owner does not wait for a retest
of the OB (or uses a lower timeframe).

### Next step proposed
1. The owner fills `docs/diagnostics/manual_trades.csv` (template next to it; 10–20 trades are
   enough) and marks the 20 loser charts in section C ("would take / would not take, why").
   Also: which grid the owner's charts use (Tabdeal = Tehran hh:30, TradingView = UTC hh:00).
2. Approve the data fixes 1–2 (and 3), then re-run this diagnosis.
3. Only then decide rule changes, using the owner's trades.

