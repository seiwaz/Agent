# SMC backtest audit (SMC-2.3, 2026-10-06)

**Question:** is the backtest correct, before anyone judges whether the strategy is any good?
**Scope:** `sp2l.smc.backtest.run` and everything it calls (`structure`, `strategy`, `lifecycle`,
`timeframes`), the M1 history it reads, the live runner and wallet only where they must match it.
**Nothing in the strategy code or the parameters was changed.** All audit code is in `audit/` with
its own tests (`tests/audit/test_audit.py`, 5 tests, pass). Every number below was computed by
those scripts. The raw outputs are the `docs/audit/*.json` files next to this report.

| Input | Value |
|---|---|
| History | merged M1 series (`load_bars`), frozen at **2026-01-08 11:00 → 2026-10-05 11:00 UTC (270 days)** |
| Markets | BTCUSDT, XRPUSDT (traded); 7 more (ETH, SOL, DOGE, ADA, BNB, LTC, AVAX; 167–270 days) only to get more trades for the execution checks |
| Parameters | `config/runtime.yaml` (**current**, SMC-2.3). Two comparison sets: **nofilter** = current without the reject-only filters (`require_discount` off, `min_net_rr` 0, `max_cost_frac` 0) and **smc22** = the SMC-2.2 settings documented in `docs/SMC_STRATEGY.md` |
| Costs | maker 0.08 %, taker 0.095 %, slippage allowance 0.0326 %, funding 0 (`config/runtime.yaml`) |

Reproduce:

```bash
uv run python -m audit.data && uv run python -m audit.lookahead && uv run python -m audit.reference && uv run python -m audit.execution && uv run python -m audit.funnel && uv run python -m audit.funnel cf && uv run python -m audit.stats && uv run python -m audit.charts && uv run python -m audit.slippage
```

---

## 0. The fact that frames everything

The current parameters make **0 trades on BTC and XRP in 270 days**, and **1 trade across all 9
markets** (ADA, −1R). The engine finds plenty of setups but rejects almost all of them:

| 9 markets, 489 complete 15m setups | current | nofilter | smc22 |
|---|---|---|---|
| ZONE_INVALID (5m close beyond the OB's far edge while waiting) | 236 | 236 | 48 |
| NO_CONFIRM (no 5m break from inside the zone within 4 h) | 123 | 123 | 164 |
| BIAS_MISMATCH / NO_BIAS | 63 | 63 | 164 |
| NOT_FRESH | 33 | 33 | 65 |
| LOW_NET_RR / COST_HEAVY / NOT_DISCOUNT / NOT_PREMIUM | 54 | — | — |
| **accepted orders** | **1** | **34** | **48** |
| filled and closed trades | 1 | 11 | 14 |

So the current configuration's performance **cannot be measured at all**. To audit the
execution, cost and statistics code, the checks in sections C–G use the 83 orders and 26 closed
trades of all three parameter sets. The engine walks these with exactly the same code path.

---

## A. Data integrity — PASS (two minor flags)

| Check | BTC | XRP | Evidence |
|---|---|---|---|
| A1 minutes present / expected | 388,705 / 388,800 | 388,662 / 388,800 | `audit/data.py:holes` |
| A1 holes inside the range | 22 (13 × 1 min, 5 × 2–4, 4 × 5–29), longest 29 min 2026-03-20 19:51 | 43 (28 × 1, 10 × 2–4, 4 × 5–29, 1 × 30), longest 30 min, same time | the same 2026-03-20 hole in every market = Tabdeal outage; not filled, not synthesised |
| A1 duplicates / out of order / OHLC impossible / price ≤ 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | also enforced by the `exchange_m1` CHECK constraint |
| A1 synthetic flat zero-volume minutes | 0 | 0 | |
| A2 random sample vs Tabdeal chart (24 × 60 min, stratified over the period) | 1,440 compared: 1 open and 1 low differ (2026-09-27 11:11, see flag 1) | 1,440 compared: 0 differ | `audit/data.py:exchange_sample` |
| A3 first / last minute of stored fetch chunks (30 random of 136 chunks, ±3 min) | 393 minutes, 60 of them chunk-edge minutes: 0 differ | 393 / 60: 0 differ | the first-bar artefact fixed in 89cae34 is gone |
| A4 HTF bars: backtest `aggregate` (Python) vs live `load_bars` (SQL), 5m…1d, UTC and Tehran grids | 0 mismatches except the first bar of 4h / 1d (and 1h on the Tehran grid) | same | first bucket of the series is partial (starts 11:00), warmup only |
| A4 HTF bars built from buckets with missing minutes | 21–25 per TF | 17–43 per TF | holes above; not flagged in the bars |
| A5 fabricated highs / lows | none found in the code or the data | | |

**Grid used:** 1m–15m and 1d on the UTC grid. 1h / 4h follow `htf_grid` = `utc` (00:00, 04:00 … UTC). Python and SQL aggregation agree bar for bar.

**Flag 1 — mixed sources on BTC.** BTC uses the live collector's own candles for 1,037 minutes
(2026-09-26 15:53 → 09-27 11:13 UTC; the merged series prefers `candles_1m`). They differ from
Tabdeal's chart in **573 opens (55 %), 89 highs, 99 lows, 22 closes, 186 volumes**. No setup or
trade falls in that window, so the impact is **0 R**. Fix (proposal): when they disagree, use the
chart series for backtests, or document which source wins.

**Flag 2 — partial buckets.** `aggregate` emits the first, partial HTF bucket as a full bar, and
bars with internal holes are not marked. This only touches warmup and about 0.1 % of bars.
Impact 0 R measured. Fix (proposal): drop the leading partial bucket in `aggregate`, as `load_bars`
already does.

---

## B. Look-ahead and repainting — PASS

| Check | Result | Evidence |
|---|---|---|
| B1 swing confirmation | every swing (BTC 9,805 / 3,302 / 822 / 206 / 27 on 5m / 15m / 1h / 4h / 1d) is known exactly `swing_len` = 5 bars after its pivot. Mean delay from the pivot's close to the moment it is known: **5m 0.42 h, 15m 1.25 h, 1h 5 h, 4h 20 h, 1d 120 h** (XRP identical) | `audit/lookahead.py:swing_delay` |
| B2 only closed HTF bars | `aggregate` / `load_bars` exclude the forming bucket. Forming-bar information is only used through `m1_range` on final M1 bars up to the decision minute | code + B5 |
| B3 known-at time per object | every zone carries `created_idx`; setups carry `confirmed_at`; sweeps and breaks their bar index. Orders only use `last_closed(…, t)` | code + B4/B5 |
| **B4 repainting replay** (bar by bar) | analysis re-run on every prefix and compared with the single full-history analysis: swings, BOS/CHoCH, sweeps, zones (boundaries, gap, known-at bar), tested / mitigated state as of that bar, trend, ATR, OB↔event link. **BTC and XRP: 5m 2,000 steps, 15m 3,000, 1h 3,000, 4h 1,596, 1d 246 → 0 differences of any kind** | `audit/lookahead.py:replay`; the test plants a repaint and the check catches it |
| **B5 truncation / prefix invariance** | whole backtest (context → setups → orders → trades) at 8 random cut points × 3 parameter sets × 2 markets: **1,935 setup decisions (summed over cuts) and 147 closed trades compared, 0 differences** | `audit/lookahead.py:truncation` |
| B6 freshness / invalidation | NOT_FRESH and ZONE_INVALID use `tested_idx` / `mitigated_idx` ≤ the last closed bar (B5). Independently recomputed: 63 / 63 BTC+XRP ZONE_INVALID decisions occur at the same minute, 6 / 6 NOT_FRESH had the OB touched before the order | `audit/funnel.py` |
| B7 liquidity target | target = nearest swing / leg extreme / previous-day level unswept up to the order minute (M1 for the forming bar). Identical on truncated data (B5) | B5 |
| B8 ATR on closed bars | ATR series identical in every replay step (B4) | B4 |
| B9 third-party SMC libraries | none (`pyproject.toml`). Own causal implementation | |

**Live-window dependence (backtest vs live engine).** The live engine analyses only the last
`lookback_<tf>` bars at each step. The backtest analyses the full history once. Every
backtest decision was recomputed from a live-style window context at the minute the runner would
decide it (BTC 64, XRP 63 setups × 3 parameter sets):
- all accepted / rejected decisions with an order are **identical** (entry, SL, TP, reasons). Accepted counts match 5/5, 8/8, 5/5, 6/6;
- the 4h bias from a 180-bar window equals the full-history bias on **1,440 / 1,440** bars (both markets);
- 15–21 differences per set, **all of one kind**: a NO_CONFIRM that the backtest decides at the window end and the live code one 5m bar later (`confirmation` returns `None` until a bar closes after the window). This is a rejection, so **0 R**.

---

## C. Execution realism — PASS (rules consistent; nothing in the population depends on them)

`audit/execution.py:ref_walk` is an independent lifecycle written from the documentation. With
default rules it reproduces the engine on **83 / 83 orders** (state, fill minute, exit minute, R to
1e-12). Each realism switch was then applied on its own:

| Check | Engine rule | Trades affected (of 83 orders / 26 closed) | Δ R |
|---|---|---|---|
| C1 limit fill: touch vs trade-through ≥ 1 tick | touch | 0 | 0.000 |
| C1 limit placed through the market (would fill as taker) | not modelled | 0 | 0.000 |
| C2 market entry slippage | **not charged** (entry = confirming close) | 0 (no market entries in these sets) | 0.000 |
| C2 market exits (TIMEOUT / TIME_STOP / INVALIDATED) | at the M1 close, **no slippage** | 2 TIMEOUT | −0.015 (nofilter), −0.008 (smc22) |
| C3 stop gapped through (minute opens beyond the SL) | filled at SL − allowance | 0 | 0.000 |
| C4 take profit | market trigger: taker fee + slippage (e.g. BTC TP 68,887.9 → fill 68,865.44) | — | correct |
| C5 SL and TP in the same minute | SL first | 0 | 0.000 |
| C6 TP in the fill minute | not credited | 0 (1 SL in the fill minute, booked as SL) | 0.000 |
| C7 expiry (120 min), one order per setup (key), `max_active` per market | as live | — | — |
| C7 `max_positions` across markets / shared margin | **not simulated** (single-market backtest) | 0 overlapping BTC/XRP trades | 0.000 |
| C8 liquidation vs stop (cross, 0.5 % maintenance, wallet 100) | guard `liq_buffer_r` 1 | 0 of 26 liquidate before the stop (max leverage 1.66×) | 0.000 |
| all realistic switches together | | | −0.015 / −0.008 |

**Proposed fixes (not applied):**
- charge the slippage allowance on market entries and on TIMEOUT / TIME_STOP / INVALIDATED exits (`lifecycle._close` callers);
- a limit on the wrong side of the market should fill at the next open as taker;
- for gaps, fill at min(SL, open) − allowance.

These rules never fired here, so the corrections are for correctness, not for these results.

**Live paper runner only:** a limit becomes visible to the runner one minute after its `created_at`
(`known = order.t + 1 min`), but `advance_active` walks from `created_at`. The paper engine can
therefore credit a fill in a minute before the order existed. All 9 recorded live signals (SMC-1.0)
filled in their creation minute. In the backtest it matters for 1 / 83 orders (DOGE, nofilter).
Fix: walk live signals from the minute after they become known.

---

## D. Costs — PASS with two gaps

**D1 worked examples** (`audit/execution.py`, per unit unless noted):

| | BTC LONG (nofilter, 2026-03-04, TP) | XRP LONG (nofilter, 2026-01-14, SL) |
|---|---|---|
| entry (maker limit) / SL / exit level | 67,813 / 66,155.2 / TP 68,887.9 | 2.14452 / 2.10719 / SL 2.10719 |
| exit fill (level ∓ 0.0326 %) | 68,865.4425 | 2.1065031 |
| entry fee 0.08 % × entry | 54.2504 | 0.001716 |
| exit fee 0.095 % × exit fill | 65.4222 | 0.002001 |
| price move | +1,052.4425 | −0.038017 |
| net | +932.7700 | −0.041734 |
| risk unit = stop + entry fee + SL slippage + taker fee on the slipped stop | 1,657.8 + 54.2504 + 21.5666 + 62.8270 = 1,796.4439 | 0.03733 + 0.001716 + 0.000687 + 0.002001 = 0.041734 |
| **R** (net / risk unit) | **+0.5192** (gross, no costs: +0.6484) | **−1.0000** |
| qty / notional / fees USDT / PnL USDT | 0.00055 / 37.30 / 0.0658 / +0.513 | 23.96142 / 51.39 / 0.0891 / −1.000 |

Fees are charged on notional, once per side, maker in / taker out. They are not charged on margin or multiplied by leverage. **Correct.**

**D2 funding — NOT TESTABLE (modelled as 0).** Tabdeal publishes no rate. The code books it
correctly when a rate is set. Sensitivity: the 26 trades span 18 funding times; at 0.01 % / 8 h
(longs pay) the total changes by **−0.02 R**. Negligible at this holding time.

**D3 slippage — the allowance was measured on the wrong market.** `slippage_allowance` 3.26 bp
comes from XAUT_USDT (`docs/cost_evidence.json`). Measured now with the same method (90 order-book
samples, public tape, `docs/audit/slippage.json`):

| | p99 half-spread | p99 stop overshoot | allowance |
|---|---|---|---|
| BTC_USDT | 0.83 bp | 2.19 bp | **3.02 bp** |
| XRP_USDT | 0.47 bp | 3.87 bp | **4.33 bp** |

BTC is covered. XRP is under-charged by 1.07 bp, about **−0.01 R per XRP stop** at the median XRP
stop. Fix (proposal): per-market allowances, re-measured periodically. The tape sample is short
(448–672 trades), so these are point estimates.

**D4 gross vs net** (closed trades, R):

| | n | gross R | net R | cost share of 1R |
|---|---|---|---|---|
| current | 1 | −1.00 | −1.00 | |
| nofilter | 11 | **−1.65** | −2.74 | |
| smc22 | 14 | **−8.74** | −9.50 | |
| all 26 | | | | median **15.5 %**, max 34.6 %; median stop 1.13 % of price (min 0.39 %); round trip 0.2076 % |

Both comparison sets already lose **before** costs. Costs are not what makes them lose.

---

## E. R and accounting — backtest PASS, live wallet FAIL

| Check | Result |
|---|---|
| E1 R definition | R = net PnL per unit / (stop distance + entry fee + SL slippage + taker fee on the slipped stop fill). **17 / 17 stop-outs are exactly −1 R** (Decimal equality) |
| E2 size from risk / stop, exchange step, minimum notional | wallet (`Wallet.size`) floors to the instrument step ✓. But `strategy.evaluate` floors **every** market to 0.00001: **11 / 11 XRP orders** have a size that is not a multiple of XRP's 0.1 step (e.g. 23.96142). Backtest R does not depend on size, so **0 R**, but the backtest's `qty`, `notional` and LEVERAGE check are slightly wrong. Tabdeal's minimum notional is not checked anywhere: NOT TESTABLE |
| **E3 ledger sums to the balance** | **FAIL (live wallet).** `smc_wallet_ledger` wallet 1: deposit + amounts = **100.0517**, last `balance_after` = **98.2651**. Signal 3's TIMEOUT is booked **twice** (rows 4 and 5, both +1.786658, both `balance_after` 101.269529), and its TIMEOUT event is also stored twice; signal id 5 is missing. Two engine processes advanced the same signal concurrently: each read the same balance, so the balance column is right by luck, while the amount column double-counts +1.79 USDT |

Fix for E3 (proposal): one engine per database, enforced with a Postgres advisory lock at start-up. Plus a unique key on the ledger (signal_id, part) and on the event (signal_id, kind, ts), so a replay cannot book twice. Fix for E2: pass the instrument step into `evaluate`.

---

## F. Independent re-computation

| Check | Status | Result |
|---|---|---|
| F1 structure | PASS | `audit/reference.py`, written from the rule text: Wilder ATR, pivots, BOS/CHoCH, FVG, last-opposite OB with the adjacent FVG (incl. the deferred gap), equal-lows-aware sweeps. Full history, BTC + XRP, 5m / 15m / 1h / 4h: **0 differences** in 28,033 swings, 10,835 breaks, 18,495 FVGs, 3,191 OBs, 9,679 sweeps |
| F1 lifecycle | PASS | `ref_walk`: 83 / 83 orders identical |
| F1 rejections | PASS | ZONE_INVALID 63 / 63 at the same minute, NOT_FRESH 6 / 6 (`audit/funnel.py`) |
| F1 confirmation / target / filter layer | partly | not re-implemented independently; covered by B5 (causality), the funnel re-check and the hand audit |
| F2 hand audit | READY FOR THE OWNER | `docs/audit/trades/*.svg` + `index.json`: **17 charts, every closed BTC / XRP trade of all sets plus other markets: 7 winners, 10 losers** (fewer than 10 winners exist in the whole population). Each shows the sweep, the break from its swing, the OB and FVG shaded only from the minute they were known, the 5m confirmation, order, fill, exit and the ledger row. I checked trade 05 (BTC smc22) by hand: SL = OB low − OB height, entry = FVG midpoint rounded down, TP = 3 × stop, exit = SL × (1 − 0.0326 %). All match |
| F3 backtest == live replay | NOT TESTABLE | the 9 recorded live signals are SMC-1.0 (market triggers, parameter hashes aa17… / c1e4…). Those parameters are not stored, and no SMC-2.3 signal exists yet. Replaced by the live-window test in B (identical decisions) |

---

## G. Statistics and robustness — INCONCLUSIVE

| | current | nofilter | smc22 |
|---|---|---|---|
| G1 closed trades (9 markets) | **1** | 11 | 14 |
| G2 net expectancy, 95 % bootstrap CI | −1.00 | −0.25 [−0.60, +0.10] | −0.68 [−1.00, −0.12] |
| G2 gross expectancy, 95 % CI | −1.00 | −0.15 [−0.55, +0.25] | −0.62 [−1.00, +0.04] |
| G3 random entries, same sides / stops / targets / costs (500 seeds) | −0.05 | −0.12 [−0.49, +0.34] | −0.18 [−0.83, +0.47] |
| label | inconclusive | inconclusive, = random | inconclusive, below random |

- **G4 breakdowns** (n ≤ 10 per cell, for the record): nofilter longs −3.01 R / 8, shorts +0.26 R / 3. smc22: 12 of 14 trades are full stops (−1 R); its one big winner (ETH +2.30 R) is **92 %** of all gross winning R. Per quarter and hour of day in `stats.json`.
- **G5 multiple testing:** on the same 2026 history, at least **28 research variants** (`docs/research/variants.csv`) plus SMC-1.0 (3 documented set-ups) plus SMC-2.0 → 2.3 plus diagnostic variants have been tried, about 40 configurations. SMC-2.3 was set after a full-history backtest, so the research holdout (last third) **is no longer clean**.
- **G6 parameter neighbourhood:** current, all 14 neighbours (swing_len 3/4/6/7, sl_buffer_atr 0.1/0.3, ob_min_atr 0.3/0.7, fvg_min_atr 0.3/0.7, confirm_window 120/480, sweep_max_bars 6/24): **0 trades on BTC and XRP in every case**. nofilter: BTC −0.44 … +1.06 R on 1–8 trades: noise.

---

## Rule-level findings (definitions, not code bugs — the owner decides)

1. **NOT_FRESH contradicts `confirm_in_zone`.** With `confirm_in_zone` the 5m reaction must start
   inside the zone (OB far edge … FVG far edge). If it starts inside the OB, the OB has been touched, and
   once that 15m bar has closed the limit order is rejected NOT_FRESH. All 33 NOT_FRESH setups
   (9 markets) are of this kind (BTC/XRP 6 / 6 verified). Counterfactual (audit-only copy, OB
   treated as fresh): current → 0 of 33 accepted (other filters reject them: **0 R**); nofilter → 33
   orders, 13 closed, +0.18 R.
2. **ZONE_INVALID decides half of all setups** (236 / 489): a single 5m close beyond the OB's far
   edge while waiting. The stop sits further out (beyond the sweep wick + 0.2 ATR), so the setup is
   abandoned on moves the stop would have survived. The rule works as written (verified). Whether it
   is intended is the owner's call.
3. **"Last unbroken swing"** is implemented as *the most recently confirmed swing*: a newer, lower
   swing high replaces an older, higher one that is still unbroken. BOS/CHoCH therefore fire on
   internal structure. This is consistent with the docstring and the reference, but it is a
   definitional choice worth stating in `docs/SMC_STRATEGY.md`.
4. **Latent:** `strategy.confirmation` computes the exec-TF bucket without `htf_grid`. That is correct
   while `exec_tf` is 1m / 5m / 15m, and wrong for exec_tf = 1h / 4h on the Tehran grid. No impact now.

## Red flags

| Red flag | Present? |
|---|---|
| Chart picture better than the ledger | no: charts are drawn from the same objects as the ledger |
| Results change on repeat / truncation | no (B4, B5) |
| Zones drawn before the bar that completes them | no (B4; charts shade zones only from their known time) |
| **Very few trades after many filters** | **yes**: 1 trade from 489 setups (current); 34 orders without the filters |
| **All profit from a handful of trades** | **yes**: smc22 92 % of winning R from one trade; nofilter's best trade 35 % |
| Fees or slippage hardcoded / zero | fees no (evidence file). **Slippage measured on XAUT, not BTC/XRP; funding 0** (no published rate) |

---

## Final summary

**1. Is the backtest correct?** Yes for everything that decides trades. There is no look-ahead and
no repainting (bar-by-bar replay and truncation: 0 differences). Independent re-implementations
match the structure and the lifecycle exactly. Fees and R are correct, and a stop-out is exactly
−1 R. The live engine's sliding windows make the same decisions as the backtest. Confirmed defects,
ranked by impact in R on these results:

| # | Defect | Impact |
|---|---|---|
| 1 | Market exits (TIMEOUT etc.) without slippage | −0.015 R / −0.008 R |
| 2 | XRP slippage allowance taken from XAUT (3.26 vs measured 4.33 bp) | ≈ −0.01 R per XRP stop |
| 3 | Funding 0 | −0.02 R at 0.01 % / 8 h |
| 4 | Market entries without slippage; no gap or marketable-limit fills | 0 R here (never triggered) |
| 5 | Backtest size rounded to 0.00001 for every market (XRP step 0.1) | 0 R (size-free R) |
| 6 | Live ledger double booking (two engine processes) | 0 R; +1.79 USDT phantom in the ledger amounts |
| 7 | Live paper fills credited one minute before the order exists | 0 R in the backtest (1 / 83 orders) |
| 8 | BTC mixed data source (1,037 collector minutes ≠ chart) | 0 R |

**2. After fixing them**, the results barely move (≤ 0.02 R). The current SMC-2.3 rules still
make **0 BTC / XRP trades (1 in 9 markets)**, so there is nothing to put a CI on. Comparison sets:

| | gross | net | 95 % CI | random baseline |
|---|---|---|---|---|
| nofilter | −0.15 R / trade | −0.25 R / trade | [−0.60, +0.10] | −0.12 |
| smc22 | −0.62 R / trade | −0.68 R / trade | [−1.00, −0.12] | −0.18 |

All of this is on 11–14 trades and inconclusive. Neither set beats random entries with the same
stops and targets.

**3. The most likely reason for the gap between expectations and results:** it is not the
backtest. The rule stack almost never lets a trade through: half of all setups die on a single 5m
close beyond the OB, a quarter on the 4 h confirmation window, and the rest on bias and the
filters. The few trades that remain lose before costs, about as often as random entries do. This
matches the earlier research (`docs/research/findings.md`: OB/FVG touches beat random levels by at
most 1–3 percentage points before costs).

*Nothing has been changed in the strategy code. Waiting for the owner's decision.*
