# Breakout / confirmation research — pre-registration (written 2026-10-06, before any run)

The **final** pre-registered study of this research line. **If no candidate passes step 3, the
research line ends** and the report says so.

Research only: nothing in the live system, `config/` or the engine changes.
- Code: `src/sp2l/smc/research/breakout.py` (run: `uv run python -m sp2l.smc.research.breakout`;
  step 4: `--holdout`). Tests: `tests/smc/test_breakout.py`.
- Output: `docs/research/breakout/`.

## 0. Data, split, costs, common rules

- **Markets and data:** the 9 markets, loader and end (2026-10-05 11:40 UTC) of the edge / momentum
  studies (`research.momentum.load_markets`). Discovery = the first 2/3 of each market's history,
  holdout = the last 1/3 (`Market.split`).
- **Split membership:** an event belongs to discovery only if its decision time **and its whole
  outcome window** lie before the split (stricter than earlier studies, so no discovery
  number touches holdout minutes). Holdout events: decision at or after the split, window
  inside the data. Random baselines draw from the same split under the same rule.
- **Timeframes:** 1h and 4h on the Tehran hh:30 grid (as the edge harness); 1D on UTC days;
  weekly = ISO weeks from Monday 00:00 UTC (built from the 1D bars).
- **Structure:** `structure.analyze` with the config's structure parameters (swing_len 5,
  ATR14 Wilder). Swings are known `swing_len` bars after the pivot. Trend = the trend after the
  last closed bar. FVG = three-bar imbalance of any size (as the edge harness). A FVG is known at
  its third bar's close.
- **Decision time t** = the close of the signal bar. Only bars closed by t are used; the
  liquidity checks below use M1 data up to t.
- **ATR** = ATR14 of the signal timeframe at the signal bar. **Tick** = the market's tick.
- **Outcome walk** (M1): from the first minute after t (market entries) or the fill minute
  (limits). A stop touched in the entry / fill minute counts; a target is never credited in
  that minute; stop first when both are touched in one minute.
- **Outcome window:** 100 bars for 1h and 4h, 30 bars for 1D. If neither target nor stop is hit,
  the trade exits at the close of the window's last minute.
- **Costs:** Tabdeal level 1. Market entry: taker 0.095 % + slippage 0.0326 % on the fill. Limit
  entry: maker 0.08 %. Every exit: taker + slippage.
  R = net / (|fill − stop| + entry fee + stop slippage + taker fee on the slipped stop): a full
  stop is −1R. **Gross R** = (exit level − entry level) / |entry − stop|, with no fee and no
  slippage.
- **"Unswept" level:** no M1 trade beyond it from its formation until t (formation: the close of
  a swing's pivot bar, the end of the day / week for day / week levels).
- **Liquidity pools** (known at t, unswept):
  - same-TF confirmed swing highs / lows;
  - **equal highs / lows** = two or more confirmed unswept same-TF swings within 0.1 ATR
    (level = the outermost);
  - the previous UTC day's high / low (PDH / PDL);
  - the previous ISO week's high / low (PWH / PWL).

## 1. Hypotheses and variants (13 in total, cap 20)

A variant is pooled over its timeframes. Timeframes are reported separately, not selected separately.

### H3 "CRT" — 1h, 4h, 1D
- Candle 1 = bar j−1 (CRH = high, CRL = low); candle 2 = bar j.
- **Bearish (short):** high_j > CRH and CRL ≤ close_j ≤ CRH. **Bullish (long):** low_j < CRL and
  CRL ≤ close_j ≤ CRH. A candle 2 beyond both sides is skipped.
- **Stop:** one tick beyond candle 2's wick + 0.2 ATR.
- **Target:** the opposite side of candle 1 (long: CRH − 0.05 ATR; short: CRL + 0.05 ATR). It
  must lie beyond the entry, else no trade.
- **Higher TF (HTF) for the trend filter:** 1h → 4h, 4h → 1D, 1D → weekly. "Counter-correction
  inside a trend" = CRT direction equal to the HTF structural trend at t (trend 0 → excluded).
- **Limit variant:** a limit at 50 % of candle 2 (rounded to the tick away from the market),
  active during the next bar only (j+1), maker, filled on touch. If 50 % is not on the resting
  side of close_j, the limit is not placed (counted).
- **Variants:**
  1. CRT · market · all
  2. CRT · market · HTF-trend
  3. CRT · limit-50 % · all
  4. CRT · limit-50 % · HTF-trend

### H4 "IFVG / CISD" — 1h, 4h
- A FVG known at bar f is **inverted** at the first later bar k (k − f ≤ lookback of the TF:
  400 bars on 1h, 180 on 4h) that closes beyond its far edge: a bullish FVG (bottom, top) with
  close_k < bottom → short; a bearish FVG with close_k > top → long. One inversion per FVG.
- **Stop:** beyond the most recent same-TF swing confirmed by k (short: the last swing high; long:
  the last swing low) + one tick + 0.2 ATR. It must lie beyond the entry, else no trade.
- **Target:** the nearest unswept pool beyond the entry in the trade direction (any of the pool
  types in §0), front-run 0.05 ATR; it must lie beyond the entry. No pool → no trade.
- **After a sweep:** a structure sweep (`analyze` sweeps) of the opposite liquidity (short: highs
  taken; long: lows taken) on a bar in [k − 12, k].
- **CISD:** a new same-TF FVG in the trade direction whose third bar is k (bars k−2..k) and which
  overlaps the inverted zone.
- **Retest variant:** a limit at the inverted zone's near edge (short: the old bottom; long: the
  old top), maker, first touch, active for 24 bars after t. It is cancelled if the stop or the
  target is touched first (a target touch is counted as a missed winner).
- **Variants:**
  5. IFVG · market · plain
  6. IFVG · market · after a sweep
  7. CISD · market
  8. IFVG · retest · plain
  9. IFVG · retest · after a sweep
  10. CISD · retest

### H5 "Liquidity-run continuation" — 1h, 4h
- **Trigger:** bar k **closes** beyond a pool unswept until the open of bar k (equal highs /
  lows, PDH / PDL, PWH / PWL; a single swing is not a pool here). The body |close − open| ≥ 1 ATR
  must point in the trade direction (long: close > open and close > pool level).
- **Entry:** market at close_k.
- **Stop:** long: 50 % of bar k − 0.2 ATR − one tick (short mirrored).
- **Target:** the nearest unswept pool beyond the entry (same pool types), front-run 0.05 ATR. If
  there is none within 4R, a 2R target.
- **1D structure** = the 1D trend at t.
- **Variants:**
  11. run · all
  12. run · with the 1D structure
  13. run · against the 1D structure

## 2. Baselines (every variant)

For every event, **10 random control bars** of the same market, timeframe and split, with the
same side and a market entry at the control bar's close:
- H3 controls are matched by candle-1 range in ATR (bar j'−1 range / ATR at j' within ±25 % of the
  event's). H4 / H5 controls are matched by the signal candle's body in ATR (within ±25 %).
- The same stop distance in ATR (of the control bar) and the same target in R as the event.
- The same outcome rules and window.

**Event − baseline** = per event, the event's gross R − the mean gross R of its controls. 95 %
bootstrap CI over events (2,000 resamples). Limit variants: filled events only; the fill rate and
the misses (target reached unfilled / expired / cancelled by the stop) are reported. Events with
no matched control are dropped from the difference and counted.

## 3. Step 1 — event study (discovery, before costs)

Per variant, per TF and pooled, and per market: n, P(target before stop), P(+1R) and P(+2R)
before −1R, median MFE / MAE (R), median hold time, mean gross R, and event − baseline.

- **Primary step-1 metric:** event − baseline in gross R.
- **A variant passes step 1** when that difference (pooled) has a 95 % CI above 0.
- 13 tests at 95 % → about 0.65 false positives are expected; step 1 is a screen.

## 4. Step 2 — trade level (discovery, gross and net)

Only the step-1 passers. The same events, one position per market at a time per variant (in
decision order).

Reported:
- mean R gross and net with 95 % CIs, PF, win rate, max drawdown in R (cumulative net, pooled in
  time order);
- trades per month (= trades / mean discovery length in months);
- cost share of 1R (median);
- per market, per side, per quarter.

**Trade-level baseline:** 100 seeds. Each trade is replaced by one random matched control (§2)
under the same position rule. **Beats** = the variant's gross mean R is above the 97.5th
percentile of the 100 seed means.

## 5. Step 3 — selection (fixed now)

A **candidate** must meet **all** of:
1. ≥ 150 pooled discovery trades;
2. gross mean R 95 % CI above 0;
3. net mean R > 0;
4. gross total R > 0 in ≥ 6 of the 9 markets;
5. beats its baseline (§4).

At most 3, ranked by the lower bound of the gross CI. **None → step 4 is skipped and the
research line ends.**

## 6. Step 4 — holdout (once, candidates only, unchanged)

The same code and rules, on holdout events, run once (the code refuses a second run).

A candidate is **validated** only if its holdout net mean R > 0 **and** the lower bound of the
90 % bootstrap CI of its holdout gross mean R > 0. Otherwise it is **not validated**.

## 7. Step 5 — report and stop

`docs/research/breakout/report.md` and a short summary:
- which hypothesis, if any, has an edge before and after costs;
- the holdout result;
- the final recommendation: "build candidate X" with its exact rules, or "end of the research
  line: no automated edge on Tabdeal crypto with these concepts".

No config change, no deployment.
