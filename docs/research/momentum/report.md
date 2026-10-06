# Momentum research — report (2026-10-06)

**Result: no edge. Step 1 found nothing to carry forward, so steps 2–4 did not run, and the
holdout is still unused.** Research only: nothing in the live system, `config/` or the engine
changed.

- Pre-registration: [`plan.md`](plan.md), written before any run. SHA-256 `2e4bfd2d…93282e44`
  (`plan.sha256`, still verifies).
- Code: `src/sp2l/smc/research/momentum.py`; tests: `tests/smc/test_momentum.py` (7, pass).
- Data: `step1.json`, `step1_comparisons.json`, `step2.json` (empty), `candidates.json` (empty).
- Reproduce: `uv run python -m sp2l.smc.research.momentum`.

## Data

The edge study's 9 markets, loader, end (2026-10-05 11:40 UTC) and split (discovery = first 2/3
of each market). Zones (FVG, OB_extreme, OB_last; 1h and 4h) come from `research.events.zones`.
Events follow `research.events.outcome`: first touch, entry at the near edge, stop one tick
beyond the far edge, 100-bar window.

- **11,818 discovery events**, each with up to 20 random control levels made with the edge
  study's method.
- Every event and every control carries its own M1 / M2 / M3 / B4 state at its touch minute.
- No holdout event, trade or statistic was computed. The holdout minutes are only loaded into
  memory, as in the edge study.

## Step 1 — event study (discovery, before costs)

Percentage points of P(+1R before −1R) with 95 % bootstrap CIs. Break-even before costs: 50 % for
+1R, 33 % for +2R.

**(b) Zone effect given momentum** (zone aligned − random levels aligned with the same state, paired
per event). This is the H1 pass test.

| tf · zone | M1 | M2 | M3 | B4 (reference) |
|---|---|---|---|---|
| 1h FVG (n aligned 3,163–3,599) | +1.3 [−0.5, +3.2] | +0.7 [−1.0, +2.5] | +0.2 [−1.6, +2.0] | −0.3 [−2.0, +1.5] |
| 1h OB_extreme (495–714) | +1.8 [−3.1, +6.6] | +2.6 [−1.6, +6.7] | +1.8 [−2.1, +5.8] | +1.4 [−2.4, +5.2] |
| 1h OB_last (662–837) | +0.4 [−4.0, +4.7] | +0.1 [−3.8, +3.7] | −1.8 [−6.0, +2.1] | +1.3 [−2.7, +5.4] |
| 4h FVG (857–1,050) | −0.9 [−4.7, +2.6] | −0.6 [−3.9, +2.6] | −1.3 [−4.8, +2.2] | −2.1 [−5.6, +1.4] |
| 4h OB_extreme (50–205) | +6.2 [−8.8, +21.7] | −4.7 [−18.8, +9.5] | +0.4 [−10.1, +11.3] | −0.3 [−7.6, +6.7] |
| 4h OB_last (96–225) | −1.3 [−11.7, +10.1] | −9.1 [−19.1, +1.1] | −7.2 [−16.0, +1.5] | −4.4 [−11.3, +2.9] |

None of the 18 fast-momentum cells has a CI above 0, at +1R or at +2R (+2R is in `step1.json`). The
zones add nothing measurable over random levels aligned with the same momentum.

**(a) Momentum effect** (random levels aligned − random levels against, pooled over the three
kinds). This is the H2 pass test.

| tf | M1 | M2 | M3 | B4 (reference) |
|---|---|---|---|---|
| 1h, +1R | −0.3 [−0.9, +0.3] | **−0.6 [−1.3, −0.1]** | −0.5 [−1.2, +0.1] | **−2.2 [−2.9, −1.4]** |
| 1h, +2R | **−1.4 [−2.0, −0.8]** | **−1.4 [−2.0, −0.9]** | −0.2 [−0.8, +0.3] | **−2.4 [−3.1, −1.7]** |
| 4h, +1R | **−2.1 [−3.7, −0.4]** | **−2.3 [−3.7, −0.7]** | −0.2 [−1.7, +1.3] | **−4.6 [−6.3, −2.9]** |
| 4h, +2R | **−1.7 [−3.4, −0.2]** | **−2.2 [−3.7, −0.6]** | −0.3 [−1.8, +1.2] | **−5.6 [−7.3, −4.0]** |

Momentum alignment is **never** positive. For a resting limit at a pullback level, being aligned
with momentum is neutral to clearly *worse* than being against it. The lagged structural 4h bias
is the worst of the four states.

**H1's comparison with the structural bias** (zone aligned with M vs zone aligned with B4, +1R,
unpaired): fast momentum does slightly better than B4 on FVGs (1h M1 +2.1 [−0.3, +4.3]; 4h M1
+2.6 [−2.0, +7.0]). Every CI includes 0. On order blocks it is mixed and also not significant. The
reason is that B4 alignment hurts (1h FVG aligned − against −2.6 [−5.0, −0.3]; 4h FVG −5.0
[−9.9, −0.3]), not that momentum helps. In absolute terms no aligned cell reaches break-even:
P(+1R) is 43–55 % and P(+2R) 29–39 %, against 50 % and 33 %.

**Agreement and delay** (discovery, every 1h close, 190 B4 flips):

| | agrees with B4 | delay from momentum switch to B4 flip: median [Q1, Q3] |
|---|---|---|
| M1 (24h return) | 57 % | 8 h [3, 17] |
| M2 (EMA20 slope) | 57 % | 10 h [4, 21] |
| M3 (1h displacement) | 58 % | 23 h [3, 57] |

The structural bias does lag fast momentum, by about 8–23 hours, and agrees with it only 57 % of
the time. Being earlier does not turn into better entries.

**Step-1 pass rule** (≥ 100 aligned events and a zone effect with CI > 0 at +1R or +2R; or an H2
momentum effect with CI > 0): **0 of 24 tests passed.** With 24 tests at 95 %, about 1.2 false
positives were expected; none occurred.

## Step 2 — exits

**Not run.** The plan trades only the step-1 passers, and there were none (`step2.json`: 0
variants). So this study cannot say which exit is best. Running the exits on non-passing entries
now would be a new, unregistered analysis.

## Step 3 — selection

**No candidates** (`candidates.json` is empty).

## Step 4 — holdout

**Not run, as the plan requires.** The holdout is still unused for this hypothesis family. The
code refuses `--holdout` without candidates, and refuses a second run.

## Conclusion

- **Is the edge momentum, zones, or neither?** Neither. The SMC zones do not beat random levels
  that are aligned with the same momentum, and momentum alignment does not help a pullback entry
  (it hurts slightly). Fast momentum is less harmful than the structural 4h bias only because
  the bias is harmful, not because momentum adds anything.
- **Best exit:** not determined (step 2 had no entries under the plan).
- **Holdout:** not used.
- **Recommendation: do not build.** Together with `docs/research/findings.md`, this rules out
  1h / 4h OB and FVG zones as an entry edge, with or without a momentum or bias filter. A
  future study would need a different entry hypothesis. The holdout is still unused for any
  such pre-registered test.
