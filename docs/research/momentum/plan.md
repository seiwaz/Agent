# Momentum research — pre-registration (written 2026-10-06, before any run)

Research only. Nothing in the live system, `config/` or the engine changes. Code:
`src/sp2l/smc/research/momentum.py` (run: `uv run python -m sp2l.smc.research.momentum`) and
its tests in `tests/smc/test_momentum.py`. Output: `docs/research/momentum/report.md` and data
files next to it.

## 0. Data, split, costs — reused unchanged

- Markets and data: the 9 markets and the loader of the edge study (`scripts/research_edge.py`):
  `research.data.load(db, sym, COVERAGE[sym] + 5, tick, upto=2026-10-05 11:40 UTC)`.
- Split: per market, discovery = the first 2/3 of its own history, holdout = the last 1/3
  (`Market.split`, as in the edge study). An event or trade belongs to the split of its touch /
  entry minute. Random controls and random entries are drawn from the same split only.
- Zones: `research.events.zones` (the engine's `structure.analyze`, Tehran hh:30 grid as in the
  edge study, size filters off), kinds **FVG, OB_extreme, OB_last**, timeframes **1h and 4h**.
- Event: the first M1 minute after the zone is known (close of its last bar) that trades into it,
  within the timeframe's lookback (as in the edge study). Event-study entry = near edge; stop
  one tick beyond the far edge (1R); outcome window 100 bars of the zone timeframe;
  `research.events.outcome` rules (a minute reaching the stop counts as the stop; no target in
  the touch minute).
- Costs (trade level): Tabdeal level 1, maker 0.08 % (limit entries), taker 0.095 % (every
  exit), slippage allowance 0.0326 % on **every** market exit (stop, take-profit trigger,
  trailing stop, momentum exit, time exit; this applies the audit's finding C2).
  R = net PnL / (stop distance + entry fee + stop slippage + taker fee on the slipped stop), so a
  full stop is −1R. Gross = exit level − entry in stop distances, no fee and no slippage.

**Holdout status, stated honestly:** no step of the momentum work has touched the holdout. But
the same calendar period was seen by earlier work that did not use this split (SMC-2.3
full-history backtests, the 2026-10-06 audit, `docs/diagnostics`). Those did not test momentum,
but the analyst has seen full-period outcomes of other rules. The holdout is therefore clean
*for selection*, not pristine for the analyst.

## 1. Momentum states — exact definitions (all known at the touch minute)

For a touch at minute *i* (open time T), only data with close time ≤ T is used:
- **M1** = sign(close[i−1] − close[i−1−1440]) on M1 closes: the 24 h return up to T. Equal → 0.
- **M2** = sign(EMA20[k] − EMA20[k−6]) on closed 1h bars (Tehran grid). k = the last 1h bar
  closed by T. EMA20 is seeded with the SMA of the first 20 closes, α = 2/21.
- **M3** = direction of the latest closed 1h **displacement bar** at or before T. Bar j is
  bullish if close_j > high_{j−1} + ATR14_1h(j−1) and bearish if close_j < low_{j−1} − ATR14_1h(j−1)
  (Wilder ATR, `structure.atr_series`). No displacement yet → 0.
- **B4** (reference) = the structural 4h bias of the edge study (`events.Trend` on 4h, the trend
  after the last closed 4h bar).

Alignment of a long (short) zone with a state S: aligned if S × direction = +1, against if −1,
undefined if 0. Undefined events are excluded from aligned / against comparisons.

## 2. Hypotheses

- **H1 (momentum-aligned zones):** on 1h and 4h, FVG / OB_extreme / OB_last touches aligned with
  a fast momentum state (M1, M2 or M3) do better than touches aligned with B4. They also do better
  than random levels aligned with the same state.
- **H2 (momentum only):** entries at random levels aligned with M1–M3 (no zone) have an edge.
  *Choice fixed now:* H2 uses the matched random levels of the event study (§3), not a separate
  ATR-pullback rule, so zone and no-zone entries differ only in the zone.

## 3. Step 1 — event study (discovery only, before costs)

Controls: for every event, **20** random levels drawn with the edge study's method
(`events.controls` logic): same market and split, a random time within ±7 days, the zone's
distance from price and width in ATR of that time, first touch within the lookback, same outcome
rules. For every control, its own M1 / M2 / M3 / B4 alignment at **its** touch minute is
recorded. Seeds are fixed per market × timeframe.

For each zone kind (3) × timeframe (2) × state (M1, M2, M3, B4) = 24 cells, pooled over the 9
markets. Reported: n, P(+1R / +2R / +3R before −1R), median MFE / MAE, aligned vs against:
- **(a) momentum effect** = random-aligned − random-against: per event, mean of its aligned
  controls − mean of its against controls (events with ≥ 1 of each), bootstrap over events.
- **(b) zone effect given momentum** = zone-aligned − random-aligned: per aligned event, its
  hit − mean of its aligned controls (events with ≥ 1 aligned control), bootstrap over events.
- Zone-aligned vs zone-against, and zone-aligned(M) vs zone-aligned(B4).
- 95 % bootstrap intervals, 2,000 resamples, seed fixed.
- Also: agreement rate of B4 with M1, M2, M3 at every 1h close of the discovery data (both
  nonzero), and the delay in hours from a momentum flip to the structural flip. For every B4
  flip at time Tb into direction d: Tb − (the last time before Tb at which M switched to d), within
  30 days; the share with no such switch is reported. Median and quartiles.

**Primary metric:** P(+1R before −1R); +2R and +3R are reported alongside.

**Step-1 pass rule (fixed now):**
- A zone cell (kind × tf × M ∈ {M1, M2, M3}) **passes** when it has ≥ 100 aligned events and its
  zone effect given momentum (b) has a 95 % CI above 0 at +1R or at +2R.
- An H2 cell (tf × M) **passes** when the momentum effect (a), pooled over the three kinds'
  controls, has a 95 % CI above 0 at +1R or +2R.
- If more than 4 cells pass, the 4 with the largest lower CI bound go on. B4 cells are reference
  only and never go on.
- 24 cells and 6 H2 tests are screened; about 1.5 false positives are expected at 95 %. Step 1
  is only a screen; the gates are step 3 and the holdout.

## 4. Step 2 — exits (discovery, trade level, gross and net)

Entry sets: the step-1 passers (≤ 4). Zone cells trade their aligned events. H2 cells trade, for
each aligned zone event's kind pool, the **first aligned control** of each event (a random level
with the same distance, width and timing).

Trade rules, the same for every entry set:
- **Entry:** limit at the near edge (control: its level), maker. It fills in the touch minute, and
  only if the state is aligned at that minute (otherwise no trade).
- **Stop (structural):** far edge ∓ 0.2 × ATR(zone TF at the zone's known time), rounded one tick
  away. Stop-first when stop and target share a minute; no target or trailing exit in the fill
  minute.
- **One position per market per entry set**, in touch order (later touches while a position is
  open are skipped).
- **Exits** (one rule per variant; the stop always applies; a safety exit at market after 100
  zone-TF bars):
  1. fixed **2R**, 2. fixed **3R** (a take-profit trigger, executed at market);
  3. **chandelier k = 2**, 4. **k = 3**: at each closed zone-TF bar after the fill, trail =
     highest high (long) since the fill − k × ATR14(zone TF) of that bar. It only ratchets, never
     below the initial stop, and is hit intrabar on M1 (market);
  5. **momentum flip:** market exit at the first 1h close after the fill at which the entry's own
     state (M1 / M2 / M3; for H2 the same) points against the trade;
  6. **time 12** and 7. **time 24** zone-TF bars: market exit at the close of the N-th zone-TF
     bar after the fill.
- **Variant count:** ≤ 4 entry sets × 7 exits = **≤ 28 variants** (cap 30).
- **Random-entry baseline** per variant (identical exits and costs): for every trade, a random
  minute of the same market and split at which the same state is aligned with the trade's side
  (the *aligned* random baseline). Entry at that minute's open (maker), same stop distance in % of
  price, the same exit rule. 100 seeds. **Beats** = the variant's gross mean R per trade is above
  the 97.5th percentile of the 100 seed means.
- Reported per variant: trades, win rate, gross and net mean R with 95 % bootstrap CI, totals, PF,
  per-market gross sign, cost share of 1R (median), and the random baseline.

## 5. Step 3 — selection (fixed now, discovery only)

A variant is a **candidate** only if **all** hold:
1. gross mean R per trade with 95 % CI above 0;
2. ≥ 150 trades pooled;
3. gross total > 0 in at least 6 of 9 markets;
4. beats the aligned random baseline (§4);
5. net mean R > 0 after Tabdeal level-1 costs.

At most 3, ranked by the lower CI bound of gross mean R. **If none: report "no edge" and stop;
step 4 is not run.**

## 6. Step 4 — holdout (once)

Only the candidates, unchanged (same code, parameters and seeds), on the holdout split. Reported
as is, with the same statistics. No re-selection and no tuning afterwards.

## 7. Step 5 — report and stop

`docs/research/momentum/report.md` and a short summary: is the edge momentum, zones or neither;
the best exit; the holdout result; recommendation (build / do not build). No config change, no
deployment.
