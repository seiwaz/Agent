# Breakout / confirmation research — report (2026-10-06)

**Result: no edge. No variant passed step 1, so there are no candidates and the holdout was not
used. As pre-registered, this ends the research line.** Research only: nothing in the live
system, `config/` or the engine changed.

- Pre-registration: [`plan.md`](plan.md), written before any run. SHA-256 `8149889e…dbdb032e`
  (`plan.sha256`, still verifies).
- Code: `src/sp2l/smc/research/breakout.py`; tests: `tests/smc/test_breakout.py` (6, pass). The
  tests cover the walk rules, range queries, CRT detection, −1R stops, split membership, and no
  look-ahead (signals identical when the future is cut).
- Data: `step1.json` (per variant: pooled, per TF, per market), `candidates.json` (empty). No
  `step2.json` or `holdout.json`: those steps did not run.
- Reproduce: `uv run python -m sp2l.smc.research.breakout`.

## Data

The 9 markets, the loader and the split of the earlier studies.

- **42,256 discovery events** (signals × entry modes). Every one has its whole outcome window
  before the split.
- Each filled event has 10 random control bars, matched on candle-1 range (H3) or signal-candle
  body (H4 / H5) in ATR, with the same side, the same stop distance in ATR and the same target
  in R.

## Step 1 — event study (discovery, before costs)

Gross R per event. "vs baseline" = event − its matched random controls, with a 95 % bootstrap CI.
Pass rule: the pooled CI must lie above 0.

| # | variant | filled | P(target) | P(+1R) / P(+2R) | median target (R) | gross mean R [95 % CI] | vs baseline [95 % CI] | pass |
|---|---|---|---|---|---|---|---|---|
| 1 | H3 CRT · market · all | 14,049 | 55.0 % | 49.8 / 33.8 % | 0.85 | +0.002 [−0.015, +0.019] | +0.002 [−0.016, +0.020] | no |
| 2 | H3 CRT · market · HTF-trend | 6,389 | 55.9 % | 50.6 / 35.4 % | 0.85 | +0.030 [+0.005, +0.056] | **+0.025 [−0.000, +0.052]** | no (just) |
| 3 | H3 CRT · limit 50 % · all | 6,077 of 9,733 (62 %) | 44.9 % | 49.7 / 32.7 % | 1.16 | −0.019 [−0.049, +0.010] | −0.019 [−0.050, +0.011] | no |
| 4 | H3 CRT · limit 50 % · HTF-trend | 2,827 of 4,435 (64 %) | 45.7 % | 50.5 / 34.6 % | 1.18 | +0.007 [−0.034, +0.050] | +0.002 [−0.041, +0.048] | no |
| 5 | H4 IFVG · market · plain | 6,749 | 62.8 % | 44.3 / 23.2 % | 0.44 | −0.006 [−0.029, +0.019] | −0.014 [−0.040, +0.015] | no |
| 6 | H4 IFVG · market · after a sweep | 1,766 | 55.2 % | 45.7 / 25.3 % | 0.62 | −0.014 [−0.075, +0.056] | −0.046 [−0.124, +0.031] | no |
| 7 | H4 CISD · market | 719 | 64.0 % | 44.1 / 20.9 % | 0.42 | −0.014 [−0.071, +0.044] | −0.024 [−0.084, +0.042] | no |
| 8 | H4 IFVG · retest · plain | 5,034 of 6,639 (76 %) | 52.8 % | 44.6 / 24.7 % | 0.71 | −0.008 [−0.038, +0.026] | −0.005 [−0.039, +0.029] | no |
| 9 | H4 IFVG · retest · after a sweep | 1,287 of 1,689 (76 %) | 44.2 % | 45.8 / 27.3 % | 1.08 | −0.047 [−0.119, +0.028] | −0.050 [−0.130, +0.036] | no |
| 10 | H4 CISD · retest | 478 of 706 (68 %) | 50.6 % | 43.9 / 21.8 % | 0.68 | −0.026 [−0.116, +0.067] | −0.015 [−0.118, +0.085] | no |
| 11 | H5 run · all | 770 | 34.7 % | 48.7 / 33.1 % | 2.00 | −0.000 [−0.096, +0.098] | −0.042 [−0.147, +0.060] | no |
| 12 | H5 run · with 1D | 329 | 35.9 % | 50.5 / 35.0 % | 2.00 | +0.026 [−0.123, +0.178] | −0.031 [−0.182, +0.125] | no |
| 13 | H5 run · against 1D | 244 | 37.3 % | 50.4 / 33.2 % | 2.00 | +0.046 [−0.132, +0.227] | +0.012 [−0.175, +0.203] | no |

**0 of 13 variants passed.** With 13 tests at 95 %, about 0.65 false positives were expected.

**Limit fills and misses.**
- CRT limit at 50 %: not placed in about 31 % of the events (4,316 of 14,049 and 1,954 of 6,389) (the 50 % level was on the wrong side
  of the close). When placed, 62–64 % filled. Of the misses, about 70 % reached the target unfilled.
- IFVG retest: 68–76 % filled. Of the misses, 92–94 % reached the target first.

The limits miss mostly the winners: the same adverse selection as in the earlier studies.

**Per timeframe** (for the record, not a selection unit; the plan pooled each variant over its
timeframes):
- CRT · market · all: 1h +0.024 [+0.004, +0.044] vs baseline; 4h −0.080 [−0.124, −0.037]; 1D
  −0.120 [−0.226, −0.015].
- The near-miss (variant 2) is essentially 1h. Its 1D cell has a single event, because the weekly
  structural trend is almost never defined in ~40 weekly bars. Per market it is positive in 7 of 9
  (BNB −0.05, LTC −0.08).
- H5 on 4h against the 1D structure: +0.09, on 91 events, with a CI from −0.21 to +0.40.

**Costs** (descriptive, at event level, no position rule; not part of the decision):

| | median cost share of 1R | mean net R per event |
|---|---|---|
| CRT (market) | 29–30 % | −0.30 |
| CRT (limit) | 28 % | −0.30 |
| IFVG (market) | 8 % | −0.12 |
| liquidity run | 20 % | −0.24 |

The best gross effect found (+0.03R on the near-miss) is about a tenth of what Tabdeal's costs
take per trade. On 1h, CRT stops are tighter than the plan assumed (≤ 25 % of 1R).

## Steps 2–4

**Not run.** Step 2 only runs on step-1 passers, and there were none. Step 3 has no candidates
(`candidates.json` is empty), so **step 4 (holdout) is skipped** and the holdout is still unused.
The code refuses `--holdout` without candidates.

## Conclusion

- **Edge before costs:** none in H3 (CRT), H4 (IFVG / CISD) or H5 (liquidity run). The closest,
  CRT at market in the HTF trend direction, is +0.025R over its random baseline, with a CI
  touching 0. That is before costs, and it is about a tenth of the costs.
- **Edge after costs:** none. Every variant loses about 0.1–0.3R per event after Tabdeal level-1
  costs.
- **Holdout:** not used (no candidate).
- **Recommendation: end of the research line — no automated edge on Tabdeal crypto with these
  concepts.**

Across four studies, these SMC / ICT concepts do not beat random entries with the same stops and
targets, before costs, on 9 markets:
- the edge study: OB / FVG pullback limits on 5m–4h;
- the momentum study: zones with fast momentum, and momentum alone;
- the audit: the SMC-2.3 engine itself (0 trades on BTC / XRP);
- this study: entries at the close of a confirming candle (CRT, IFVG / CISD, liquidity runs) and
  retests after a polarity flip.

Tabdeal's costs (0.21 % round trip) then turn every variant clearly negative.
