# SMC-2.0 — audit (Phase 0) and change plan (Phase 1)

Status: **approved 2026-10-05 (all proposed defaults) and implemented** — see "Implementation
notes" at the end. Services on the server are not restarted yet.
Base: branch `smc-engine` @ HEAD (SMC-1.0, `ob_require_fvg: true`, chart gaps deployed 2026-10-05).

## 0. Open questions (the spec is ambiguous here; each has a proposed default)

| # | Question | Proposed default |
|---|---|---|
| Q1 | "FVG **directly** after the OB": the gap must start at the OB candle (FVG = bars j, j+1, j+2, so the FVG's bottom is the OB's high), or any gap between OB and break (today's `ob_require_fvg`)? | Directly: the first gap after the OB, with middle bar j+1. Then the "OB edge touching the FVG" is exactly the OB's proximal edge |
| Q2 | When is the limit placed, and when do the 8 M15 bars start? Placing it when the setup forms means a 1h OB touched more than 2 h later is never traded | Armed when price first trades into the FVG; the limit rests at the OB edge; 8 M15 bars count from arming |
| Q3 | Sweep → BOS: maximum distance between the sweep bar and the BOS bar? Is CHoCH accepted as the displacement as well as BOS? | `sweep_max_bars: 12` (1h bars); both BOS and CHoCH |
| Q4 | "Equal lows": how many swings, and which tolerance? | ≥ 2 confirmed swing lows within `eq_tol_atr: 0.1` × ATR(1h); the sweep must go beyond the lowest of them |
| Q5 | "Last opposite-colour candle": search window, and does a doji (close = open) count? | Last candle with close < open (long) in [sweep bar, BOS bar). No such candle → no setup. Doji does not count |
| Q6 | Fixed-R fallback: in price R or net R? A price-2R TP2 can never pass "net R:R to TP2 ≥ 2" | Net R (target price solved so net R after fees = 1 / 2) |
| Q7 | TP1 "internal liquidity" and TP2 "opposing FVG/OB": on which timeframe? | TP1 = nearest unswept confirmed **15m** swing high (long) beyond entry; TP2 = near edge of the nearest valid opposing **1h** OB/FVG beyond TP1 |
| Q8 | TP3 external liquidity: day/week boundaries (UTC or Tehran)? What if no level exists beyond TP2? | UTC days, ISO weeks; levels PDH/PWH and 1h equal highs. No level → no TP3, the last 20 % runs on the trailing stop until `max_hold_min` |
| Q9 | Trailing: does it start after TP1 or after TP2? Buffer? | After TP2: stop = last confirmed 15m swing low − `trail_buffer_atr` (0.1) × ATR(15m), only ever tightened |
| Q10 | Keep `max_risk_pct` (stop ≤ 1 % of price)? 1h sweep stops on XRP often exceed it | Keep it at 0.01, reason SL_TOO_WIDE (set to 1 to disable) |
| Q11 | M1 trigger path: remove, or keep it reachable through config? | Remove. Keeping it would need a second chart, runner and ladder path, which is not cheap |
| Q12 | "Top 3 by quality" on the chart: which quality? | Positions' zones first, then the remaining zones by distance to price |
| Q13 | MISSED: which target reached before the fill? | TP1 |
| Q14 | Minimum quantity split: qty too small to split 50/30/20 into steps | Parts round down to the quantity step; a part that rounds to 0 moves to the next. R uses the actual fractions |

## 1. Gap table

| Item | Current behaviour (file:function) | Required change | Risk |
|---|---|---|---|
| Timeframes | Bias `bias_tf` 4h (`strategy.evaluate` → `trend_at`); confirmation `confirm_bias_tf` 1h adds score; POI `poi_tfs` [4h, 1h]; trigger `trigger_tf` 1m: every M1 BOS/CHoCH is evaluated (`strategy.find_setups`, `runner.create_signals` FRESH 3 min) | Bias 4h; zone TF `zone_tf` 1h; execution/confirmation `exec_tf` 15m. No M1 trigger. Fills, stops and targets are still walked on **M1 bars** (finer and conservative); every duration is in 15m-bar units | High: replaces the core of `strategy.py` |
| Sweep | Only a score factor: `strategy._sweep` (M1 OB below the last M1 swing low); `_htf_sweep` optional filter. No close-back-inside rule, no equal lows | New `structure.sweeps()`: a 1h bar whose wick goes beyond an unswept confirmed swing low (or equal-lows cluster, Q4) and **closes back inside**. Required | Med |
| Displacement / BOS | `structure.analyze/_break`: close beyond the last unbroken swing (BOS/CHoCH) | Unchanged; must follow a sweep within `sweep_max_bars` (Q3) | Low |
| OB definition | `structure._break`: extreme low/high candle between the broken swing and the break (max `ob_lookback`); filters `ob_min_atr`, `ob_require_fvg` (any gap in the leg, `leg_gaps`) | New `ob_rule: last_opposite` (default): last opposite-colour candle in [sweep, break) (Q5); `extreme` kept for SMC-1.0 replays. `ob_min_atr` 0.5 stays | High: every OB on every TF moves |
| FVG after OB | `ob_require_fvg` = any gap in (OB, break) (`structure.leg_gaps`) | `fvg_adjacent: true`: the gap must start at the OB candle (Q1). The setup stores the FVG | Med |
| Order | Not enforced (sweep is a factor) | `strategy.zone_setups()`: sweep_idx < OB idx ≤ … < break_idx and FVG after OB; anything else is ignored | Med |
| Freshness | `Zone.tested_idx` / `TESTED` (`structure.update_zone`); `_fresh` is a score factor; `find_poi` accepts tested zones | The OB must be untouched until arming; after the first touch it is USED (no longer a POI). Close-through invalidation and expiry unchanged | Low |
| POI overlap with M1 OB | `strategy.find_poi`: an HTF zone must overlap the M1 trigger OB | Removed | Low |
| Trigger | M1 BOS/CHoCH in the bias direction, fresh ≤ 3 min | Arming: first M1 bar that trades into the FVG (Q2). Optional `confirm_exec: false`: when true, wait for a 15m BOS/CHoCH with the bias after arming and enter at market at that close | Med |
| Entry | Limit (maker) at the POI proximal edge (`entry_mode: proximal`, `entry_on: poi`), or the M1 OB mid if price already left; expiry `pending_expiry_min` 120 from the trigger | Limit (maker) at the OB proximal edge (= edge touching the FVG); expiry 120 min (8×15m) from arming | Med |
| SL | `evaluate`: beyond min(POI, M1 OB) − `sl_buffer_atr` (0.25) × ATR(POI TF); `max_risk_pct` cap | Beyond the farther of the sweep wick and the OB far edge − 0.2 × ATR(1h); cap per Q10 | Low |
| TP | Single TP: `tp_mode: fixed` 3R, `min_net_rr` 1. `lifecycle.advance` has a backtest-only partial (`tp1_rr`, `tp1_frac`, `tp1_be`) | Ladder TP1 50 % (stop → BE net of fees) / TP2 30 % / TP3 rest + trailing (Q7–Q9); skip if net R:R to TP2 < 2; fallbacks 1R / 2R (Q6), recorded per target | High |
| `be_at_r` | `lifecycle.advance`: optional BE at +N R (off) | Replaced by BE after TP1; key removed | Low |
| Timeout | `max_hold_min` 1440 → TIMEOUT | + `time_stop_min` 180 (12×15m): market exit if neither TP1 nor SL within it → TIME_STOP; `max_hold_min` 1440 stays | Low |
| Fee filter | None (only `min_net_rr` after fees) | Skip if (entry×maker + SL×taker + SL×slippage) > `max_cost_frac` 0.15 × stop distance → FEE_TOO_HIGH | Low |
| Capacity | `runner.create_signals` (`max_active`), `Wallet.active_count` / `open_margin` and every SQL `state IN ('PENDING','OPEN')` | Unchanged rules; "active" becomes PENDING, OPEN, TP1, TP2 everywhere (store, wallet, API, partial index) | Med: several SQL sites |
| Wallet booking | `Wallet.book_close`: one REALIZED_PNL row at TP/SL/TIMEOUT | One REALIZED_PNL row per exit part (TP1, TP2, final), each with its own entry-fee share + exit fee; the signal's pnl/fees = sums; a full stop is −1R exactly | Med |
| Chart layers / zone selection | `api/smc.py SmcView.analysis`: every valid zone of the TF + HTF zones (+ gaps); `web/app.js shown()`: Focus = nearest 2 OB / 1 FVG per side, merged; All = everything. `radar`: valid POIs with the bias | Default ("Setups") = output of `strategy.zone_setups()` only (rules in §5); "All zones (debug)" = today's All view | Med |
| Backtest | `backtest.run`: M1 triggers → `find_setups`, POI once, capacity, optional tp1 | Same `zone_setups()` + arming + `lifecycle` ladder; per-trade ladder outcome | Med |

## 2. Files

Change:
- `src/sp2l/smc/model.py` — new/removed keys, `Setup` gains sweep, fvg, tp1/tp2/tp3 + sources; VERSION "SMC-2.0".
- `src/sp2l/smc/structure.py` — `sweeps()`, `ob_rule: last_opposite`, `fvg_adjacent`; zones keep their sweep/FVG link.
- `src/sp2l/smc/strategy.py` — replace the M1-trigger path with `zone_setups()` (one source for engine, backtest, API), `arm()`, `plan()` (entry/SL/ladder/filters). Remove `find_poi`, `_htf_sweep`, score factors.
- `src/sp2l/smc/lifecycle.py` — states TP1, TP2, BE, TRAIL, TIME_STOP; ladder, BE, trailing (needs the 15m swings), time stop; conservative rules kept.
- `src/sp2l/smc/runner.py` — arm setups on the minute price enters the FVG (never retroactive); feed 15m swings to the trailing stop; book every exit part.
- `src/sp2l/smc/wallet.py`, `store.py` — partial bookings, active states, new columns.
- `src/sp2l/smc/backtest.py`, `src/sp2l/__main__.py` (backtest CLI print).
- `src/sp2l/api/smc.py`, `src/sp2l/api/app.py` — setups payload, debug mode, ladder in signals, active states.
- `web/app.js`, `web/index.html`, `web/app.css` — Setups / All-zones (debug) switch, setup labels (tf · age · state), lifecycle colours, ladder on position objects, time-stop marker, ticket.
- `config/runtime.yaml`, `config/server.yaml`; `docs/SMC_STRATEGY.md` (SMC-2.0; SMC-1.0 evidence kept and marked); `README.md`.
Add:
- `src/sp2l/persistence/migrations/versions/0020_smc_ladder.py`.
- `tests/smc/test_setups.py`, `tests/smc/test_ladder.py`, `tests/smc/test_lookahead.py`, `tests/db/test_smc_replay.py`, `tests/ui/test_chart.py`.

## 3. Config keys (`smc`, all in the params hash)

New: `zone_tf: 1h`, `exec_tf: 15m`, `ob_rule: last_opposite`, `fvg_adjacent: true`, `sweep_max_bars: 12`, `eq_tol_atr: 0.1`, `confirm_exec: false`, `sl_buffer_atr: 0.2` (changed from 0.25), `tp1_frac: 0.5`, `tp2_frac: 0.3`, `tp1_fallback_r: 1`, `tp2_fallback_r: 2`, `min_net_rr_tp2: 2`, `trail_buffer_atr: 0.1`, `day_boundary: utc`, `time_stop_min: 180`, `pending_expiry_min: 120`, `max_cost_frac: 0.15`, `chart_near_atr: 3`, `chart_top_n: 3`.
Kept: `swing_len`, `atr_len`, `ob_lookback`, `fvg_min_atr`, `ob_min_atr`, `ob_require_fvg`, `fvg_fill`, `bias_tf`, `max_risk_pct`, `tick`, `lookback_*`, `history_days`, `max_hold_min`, `max_active`, `max_positions`, `account_usdt`, `risk_pct`, `max_leverage`.
Removed: `trigger_tf`, `confirm_bias_tf`, `poi_tfs`, `poi_kinds`, `entry_mode`, `entry_on`, `sl_mode`, `min_sl_atr`, `tp_mode`, `tp_rr`, `min_rr`, `target_tfs`, `min_net_rr`, `min_score`, `require`, `be_at_r`, `tp1_rr`, `tp1_be`.

## 4. DB — migration `0020_smc_ladder`

- `smc_signals`: add `tp1`, `tp2`, `tp3` numeric (tp3 nullable, Q8), `targets` jsonb (source and fallback flag per target), `qty_open` numeric, `realized_r` numeric, `version` text. `tp` becomes nullable (it holds TP3, or NULL if there is none); existing rows untouched.
- Rebuild the partial index `smc_signals_active` for `state IN ('PENDING','OPEN','TP1','TP2')`.
- `smc_wallet_ledger`: add `part` text (TP1 / TP2 / TP / SL / BE / TRAIL / TIME_STOP / TIMEOUT) and `fees` numeric; kinds unchanged (REALIZED_PNL per part); append-only trigger kept.
- The open SMC-1.0 BTC position keeps running with its single TP (no tp1/tp2 → old path in `lifecycle`).

## 5. Lifecycle states

PENDING (limit resting) → OPEN → TP1 (50 % out, stop at BE net of fees) → TP2 (another 30 % out, trailing) → TP (TP3, rest out).
Terminal exits: SL (before TP1, −1R exactly), BE (rest stopped at BE after TP1), TRAIL (rest stopped by the trailing stop after TP2), TIME_STOP (neither TP1 nor SL within `time_stop_min`), TIMEOUT (`max_hold_min`), EXPIRED (not filled within 8×15m of arming), MISSED (TP1 before the fill, Q13), CANCELLED.
Conservative rules: stop beats any target in the same M1 bar; no target in the fill bar; a moved stop (BE/trail) counts from the next bar. R = Σ partᵢ × netᵢ / risk_unit.

## 6. Chart

Default "Setups" view (on every TF):
- Fresh, valid, unexpired 1h OBs of complete setups (sweep → BOS → OB + FVG) with the 4h bias, within `chart_near_atr` × ATR(1h) of price or tied to a pending/open position, top `chart_top_n` (Q12);
- their FVG and the sweep level (line from the sweep wick);
- the target levels (TP1–TP3 sources);
- per pending/open position: entry, SL, TP1–TP3 with net R, the time-stop and expiry time (vertical marker).
Hidden: tested/used, invalid, expired, counter-bias, < 0.5 ATR, standalone FVGs, structure labels beyond the lookback.
Labels: `1h OB · 5h · waiting`, colour by state: waiting (fresh), armed (in FVG), pending, open, used.
"All zones (debug)" switch = today's All view (every zone, all gaps). Layer toggles kept. A zone is only ever drawn once it is confirmed (break bar closed).

## 7. Tests

Unit, hand-built candles (`tests/smc/test_setups.py`, `test_ladder.py`):
- sweep → displacement → OB → FVG accepted; wrong order rejected; no sweep rejected; sweep that does not close back inside rejected; equal lows tolerance;
- OB = last opposite candle (not the extreme candle);
- FVG adjacent required;
- freshness: a second touch is not an entry; touched before arming is not a setup;
- entry at the OB/FVG edge; SL = farther of sweep / OB + 0.2 ATR;
- TP ladder: TP1 + BE, TP2, trailing stop moves only up, TRAIL exit, TP3; fallbacks recorded; net R:R to TP2 < 2 skipped;
- time stop, pending expiry from arming, MISSED;
- fee filter;
- a full stop = −1R exactly; partial R and wallet ledger parts sum to the total PnL with fees.
- `test_lookahead.py`: truncating future bars does not change past zones, setups or signals.
- `tests/db/test_smc_replay.py`: backtest trades == the live runner stepped minute by minute over the same stored data.
- `tests/ui/test_chart.py` (Playwright, WebKit is installed): the default chart shows only setup zones; the debug switch shows all.
- ruff, mypy, pytest.

## 8. Deployment

Commit per phase. No service restart until you confirm. Then: migration 0020 → restart engine and API. The open SMC-1.0 position continues under its old rules.

## Implementation notes (Phase 2 / 3)

- Done as planned: items 1–9, migration `0020_smc_ladder`, states, chart, docs (SMC-2.0), tests.
- One extra key: `tp_inside_r: 1` (the "inside 1R" threshold of the TP fallbacks), in the hash.
- `risk_unit` now charges the exit fee on the slipped stop fill, so a full stop is exactly −1R
  (before: about −1R). Stored risk of existing signals is unchanged.
- Margin in use counts only the open quantity after partial exits.
- `tests/db/test_schema.py::test_no_second_target_columns_anywhere` (a rule of the retired SP2L
  tables) now excludes `smc_*` tables, which carry the approved ladder.
- The UI test uses any installed Playwright WebKit build when the bundled one is missing.
- Finding (information only, 270 days, local data, after fees): BTC 1h: 317 BOS/CHoCH → 73 OBs
  (last opposite candle, ≥ 0.5 ATR, FVG right after) → 9 complete sweep→BOS→OB+FVG setups → 8
  armed → **0 accepted**; XRP: 18 armed → 0 accepted. `max_cost_frac` 0.15 with Tabdeal costs
  (≈ 0.21 % round trip) needs a stop ≥ 1.38 % of price while `max_risk_pct` caps it at 1 %:
  no order can pass both. With `max_risk_pct` 0.03 each market trades once in 270 days.

## Owner changes after the plan (2026-10-05)
- Fee filter off, then removed: costs never reject a trade.
- Stop and targets are independent: SL one tick beyond the OB's wick; TP1 nearest internal
  liquidity / previous swing (15m or 1h), TP2 next unfilled 1h FVG, TP3 external liquidity with
  a trailing stop one tick beyond 15m swings. Removed: `sl_buffer_atr`, sweep wick in the stop,
  `max_risk_pct`, `max_cost_frac`, `tp_inside_r`, `tp1_fallback_r`, `tp2_fallback_r`,
  `min_net_rr_tp2`, `trail_buffer_atr`, opposing OB as TP2. No TP2 level: its share stays in the
  runner, which trails from TP1 on.
- One target instead of the ladder (owner, 2026-10-05): TP at the edge of the previous HH
  candle (long) / LL candle (short), `tp_ref: leg` by default; TP1 / TP2 / TP3, break-even,
  trailing stop and their states (TP1, TP2, BE, TRAIL) removed. The ladder columns of 0020 stay
  in the schema, unused.
