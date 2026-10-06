# SMC engine — strategy specification (SMC-2.3)

Markets: **BTC/USDT and XRP/USDT**, traded together on **one shared simulated wallet**
(100 USDT). Each market has its own price tick / quantity step (`instruments`). No orders are
sent: signals and their simulated lifecycle are recorded for review. SMC-2.0 replaces the
SMC-1.0 M1-trigger model (2026-10-05, owner's intraday entry model); the SMC-1.0 evidence is
kept at the end of this file. **SMC-2.1** (2026-10-05) adds the discount / premium and minimum
net-R filters, the front-run target, an optional fee filter and CHoCH exit, and turns the
3 h time stop off.

## SMC-2.3 (2026-10-06, owner) — the standard SMC / ICT model for crypto futures
Long described; short mirrored. Every rule is a setting (`config/*.yaml` → `smc` / `costs`);
code defaults keep SMC-2.2 / 2.1, so earlier configs give their earlier trades.

1. **Bias** (`bias_tf` 4h): the 4h structure (last BOS / CHoCH by close) must point the
   trade's way (NO_BIAS / BIAS_MISMATCH). 1h / 4h bars on the **UTC grid** (`htf_grid: utc`,
   00:00, 04:00, … like TradingView and the large venues; `tehran` = Tabdeal's hh:30 grid).
   Detection delay of the 4h bias (first minute beyond the broken level → close of the 4h
   bar that confirms the break), full local history: BTC 2.3 h (UTC) / 2.2 h (Tehran), XRP
   2.2 h / 2.5 h on average.
2. **Setup on 15m** (`zone_tf`), in this order: sweep of confirmed lows (a whole equal-lows
   pool within `eq_tol_atr` 0.1 ATR; close back inside) → BOS / CHoCH by close within
   `sweep_max_bars` 12 → OB = last opposite candle at or after the sweep bar, ≥ `ob_min_atr`
   0.5 ATR → the FVG right after it. **Discount**: the OB's proximal edge in the lower half
   of sweep wick → displacement high (the highest high from the sweep to the bar on whose
   close the setup was known) (`require_discount`, `discount_ref: displacement`;
   NOT_DISCOUNT / NOT_PREMIUM).
3. **Activation and 5m confirmation** (`confirm_exec`, `exec_tf` 5m): activation = price
   first trades into the 15m FVG. Within `confirm_window_min` 240 a 5m BOS / CHoCH with the
   setup must close, reacting from a 5m low (the lowest low between the broken 5m swing and
   the break) that lies inside the zone (OB far edge .. FVG far edge) and was made after
   activation (`confirm_in_zone`). A 5m close beyond the OB's far edge first: ZONE_INVALID.
   No such break in time: NO_CONFIRM.
4. **OB state at order time**: fresh (NOT_FRESH) and valid (ZONE_INVALID).
5. **Entry** (`confirm_entry: limit`, `entry_ref: ltf_fvg_ce`): a limit (maker) from the
   confirming close at 50 % (consequent encroachment) of the newest 5m FVG of the confirming
   move (its third bar between the reaction low and the break); without one, at the proximal
   edge of the move's last opposite 5m candle (`ltf_ob_edge`); without either, at 50 % of
   the 15m FVG (`htf_fvg_ce`, the SMC-2.2 entry). Cancelled `pending_expiry_min` 120 later;
   one order per setup; never retroactive.
6. **Stop** (`sl_mode: structure`): beyond the farther of the 15m OB's wick and the sweep
   wick, plus `sl_buffer_atr` 0.2 × ATR(15m), rounded to the tick away from the entry
   (`ob_height` = SMC-2.2, `wick` = SMC-2.1).
7. **Target** (`tp_mode: liquidity`): the **nearest** unswept external liquidity beyond the
   entry, known at the order: the displacement-leg high, unswept 15m and 1h swing highs
   (reported as equal highs when two lie within 0.1 ATR), the previous UTC day's high (while
   today has not traded above it). The TP sits `tp_front_run_atr` 0.05 × ATR(15m) before it;
   a level whose TP would not lie beyond the entry is skipped; never a farther level for a
   better R:R (`fixed` = `tp_rr` × the stop, SMC-2.2; `hh_ll` = SMC-2.1).
8. **Filters**: `min_net_rr` 2 (net R at the TP: maker entry fee, taker exit fee with TP
   slippage, the stop's slippage in the risk unit; LOW_NET_RR) and `max_cost_frac` 0.20
   (entry fee + stop exit fee + slippage per unit above 20 % of the stop distance:
   COST_HEAVY). Filters never move the TP or the SL.
9. **Size and capacity**: 1 % of the shared wallet over stop + costs, ≤ 10× leverage, margin
   within the free balance, 1 position per market, 2 in total. **Liquidation guard**
   (`liq_buffer_r` 1, `maint_margin_rate` 0.005 assumed; cross margin): the estimated
   liquidation (equity not backing other positions − qty × move = rate × notional) must lie
   at least one stop distance beyond the stop, otherwise a smaller size, or no trade when
   even the smallest step fails (LIQ_LIMITED). At 1 % risk it rarely binds.
10. **After entry**: TP, SL, or TIMEOUT at 24 h (market); no time stop, no CHoCH exit;
    same-minute rule: SL first. **Funding** (`costs.funding_rate`, `funding_interval_h`):
    Tabdeal publishes no funding rate or interval (public market data and its academy,
    checked read-only 2026-10-06), so it is modelled as **0**; when set, every funding time
    an open position spans lowers its R and books a FUNDING ledger row.

### Backtest, full local history (2025-12-13 .. 2026-10-06, after fees, for information)
| | BTC SMC-2.2 | BTC SMC-2.3 | XRP SMC-2.2 | XRP SMC-2.3 |
|---|---|---|---|---|
| complete 15m setups | 70 | 70 | 70 | 70 |
| orders / closed trades | 8 / 3 | 0 / 0 | 6 / 1 | 0 / 0 |
| win rate | 33 % | — | 0 % | — |
| gross R / net R | −1.89 / −1.80 | 0 / 0 | −1.07 / −1.00 | 0 / 0 |
| PF | 0.10 | — | 0 | — |
| average stop | 1.39 % of price, 4.4 × the round-trip cost | — | 1.10 %, 4.9 × | — |
| rejections | BIAS_MISMATCH 26, NO_CONFIRM 19, ZONE_INVALID 9, NOT_FRESH 7, NO_BIAS 1 | ZONE_INVALID 36, NO_CONFIRM 16, BIAS_MISMATCH 8, LOW_NET_RR 5, NOT_FRESH 4, COST_HEAVY 1, NOT_DISCOUNT / NOT_PREMIUM 2, NO_BIAS 1 | NO_CONFIRM 25, BIAS_MISMATCH 23, ZONE_INVALID 10, NOT_FRESH 5, NO_BIAS 1 | ZONE_INVALID 32, NO_CONFIRM 21, BIAS_MISMATCH 10, LOW_NET_RR 5, COST_HEAVY 2, NOT_FRESH 2 |

SMC-2.3 found no tradable setup in this history: all 36 (BTC) / 32 (XRP) ZONE_INVALID are a
5m close beyond the OB's far edge while waiting for the confirmation from inside the zone;
of the 18 BTC setups that were confirmed, the 4h bias, LOW_NET_RR and freshness removed the
rest. For information (BTC, same history): without the filters 5 orders (2 TP, 2 MISSED,
1 TIMEOUT, +0.40 R); without the in-zone rule and the filters 8 orders (−0.38 R).

## SMC-2.2 (2026-10-06, owner) — superseded by SMC-2.3 before it was deployed
The same structure code, with these settings (`config/*.yaml`; every new key defaults to the
SMC-2.1 behaviour, so older configs are unchanged):
- **Zones on 15m** (`zone_tf`), bias **4h**: sweep → BOS/CHoCH by close → fresh OB (last
  opposite candle) with the FVG right after it.
- **5m confirmation**: after price first trades into the FVG, wait up to 4 h
  (`confirm_window_min` 240) for a 5m BOS / CHoCH with the setup (`confirm_exec`, `exec_tf` 5m);
  then a **limit at 50 % of the FVG** (`entry_ref: fvg_mid`, `confirm_entry: limit`), cancelled
  2 h later (`pending_expiry_min`). The OB must still be untouched (NOT_FRESH) and valid.
- **SL beyond the OB by its own height** (`sl_mode: ob_height`; long: OB low − OB height).
- **TP at 3 × the stop distance** (`tp_rr` 3, price R:R 1:3); the HH / LL is still recorded.
- **Filters off**: `require_discount` false, `min_net_rr` 0 (the owner's chosen variant).

Backtest (270 days to 2026-10-06, after fees, local merged M1 series): BTC 48 confirmed setups,
8 orders, 3 filled and closed, 1 win, **−1.80 R**; XRP 42 / 6 / 1 closed, **−1.00 R**. Most
orders expire unfilled (BTC 5 of 8): after a 5m break price rarely returns to 50 % of the FVG
within 2 h. Confirmation windows of 2 h / 4 h / 6–48 h gave BTC 5 / 8 / 8 orders; most of the
extra confirmed setups are rejected by the 4h bias (23–32), an OB closed through (9–13) or
already touched (7–10). Longer order expiry (4–24 h) filled more orders and lost more
(−2.8 to −3.8 R). Samples this small decide nothing.

## Data (no live warmup)
- Every bar comes from one merged 1-minute series: canonical live candles (`candles_1m`,
  collector) where they exist, otherwise **Tabdeal chart history** (`exchange_m1`), fetched
  on demand for `history_days` (35) when the engine starts and topped up every minute.
- Higher timeframes are aggregated from that series in SQL: 1m–15m and 1d UTC-aligned; 1h and
  4h on `htf_grid` — **UTC** in SMC-2.3 (00:00, 04:00, …), `tehran` = Tabdeal's own chart
  (bars open at hh:30 UTC, SMC-1.0 .. 2.2).
- Only closed bars are analysed; a swing is usable only `swing_len` bars after it formed. The
  chart, the engine and the backtest call the same functions with the same parameters.

## Structure (identical on every timeframe, `structure.py`)
| Concept | Rule |
|---|---|
| Swing | high (low) strictly beyond `swing_len` bars on the left, not exceeded by `swing_len` on the right; known `swing_len` bars later |
| BOS / CHoCH | a bar **closes** beyond the last unbroken swing: BOS with the trend, CHoCH against it (first break = BOS) |
| Order block | `ob_rule: last_opposite`: the last opposite-colour candle (bearish for a bullish break) between the broken swing and the break (max `ob_lookback` bars); zone = its full range; at least `ob_min_atr` (0.5) × ATR. `extreme` = the SMC-1.0 rule |
| OB gap | `ob_require_fvg` + `fvg_adjacent`: the FVG right after the block (bars j, j+1, j+2, any size) must exist; when it closes after the break the block is known one bar later |
| Fair value gap | 3-bar imbalance (`low[i] > high[i-2]` / `high[i] < low[i-2]`); standalone gaps below `fvg_min_atr` (0.5) ATR are not zones |
| Sweep | a bar whose wick runs beyond unswept confirmed swing lows (highs) and **closes back inside**, beyond the deepest swing it took. Swings within `eq_tol_atr` (0.1) × ATR are one equal-lows (highs) pool: taking only part of it is not a sweep |
| Zone status | TESTED when traded into; invalid once a bar **closes** through the far side (OB) / a wick fills it (FVG); expires after the timeframe's lookback |

## Entry model (`strategy.py`)
1. **Bias** — the trend of `bias_tf` (4h) must point the trade's way when the order is placed.
2. **Setup on `zone_tf` (1h), in this order** (`zone_setups`, one source for engine,
   backtest and chart): a) liquidity sweep (`require_sweep: true`; switched off and
   restored on 2026-10-05 after the backtest without it lost more, see SMC-2.0_TIMEFRAMES.md); b) displacement: a close beyond structure
   (BOS or CHoCH) at most `sweep_max_bars` (12) bars after the sweep; c) order block = last
   opposite candle, **at or after the sweep bar**; d) the FVG right after it. Out of order
   (block before the sweep, no sweep, sweep without a close back inside, break too late):
   ignored. The setup is known at the close of the bar that completes it.
3. **Fresh OB only** — the order is **armed** on the first minute after the setup that trades
   into the FVG. The limit (maker fee) rests at the **OB edge touching the FVG** (the OB's
   proximal edge), from the setup's confirmation, so it can fill in the arming minute; it is
   cancelled `pending_expiry_min` (120 = 8 × 15m) after arming. One order per setup: a later
   touch is never an entry (NOT_FRESH). `confirm_exec: true` instead waits, after arming, for
   an `exec_tf` (15m) BOS / CHoCH with the setup inside that window and enters at market
   (taker) at its close.
4. **Stop** — one tick beyond the order block's wick (below its low for a long, above its
   high for a short). Nothing else: no ATR buffer, no cap on the distance.
5. **Target** — one, from the market alone, **never derived from the stop** (owner rule
   2026-10-05): the edge of the previous **HH** candle (long: its high) / **LL** candle (short:
   its low) **made after the OB candle and before price first came back to the OB**
   (`tp_ref: leg`, default; closed 1h bars after the OB + the minutes of the forming one, up
   to the order); `swing`: the last confirmed 1h swing high / low beyond the entry. The TP
   sits `tp_front_run_atr` (0.05) × ATR(1h) **before** that level, rounded to the tick towards
   the entry (0 = the exact level); the level itself is recorded and drawn. None beyond the
   entry: no trade (NO_TARGET).
6. **Filters** — they may reject a trade; they never move the stop or the target:
   - `require_discount` (true): dealing range = sweep wick → the HH (long) / LL (short) of
     the target (the OB wick when a setup has no sweep). A long's entry must be at or below
     50 % of it, a short's at or above (NOT_DISCOUNT / NOT_PREMIUM).
   - `min_net_rr` (2): the net R at the TP, computed as the lifecycle computes R (entry fee,
     exit fee, slippage), must reach it (LOW_NET_RR).
   - `max_cost_frac` (**0 = off**, owner): entry fee + stop exit fee + slippage per unit above
     this share of the stop distance rejects the trade (COST_HEAVY).
7. **Size** — risk `risk_pct` of the wallet over the stop distance plus costs; advisory size
   ≤ `max_leverage` (LEVERAGE).
8. **Capacity** — `max_active` (1) per market, `max_positions` (2) across markets; orders are
   never created retroactively (only within 3 minutes of the arming minute / confirmation).

## Lifecycle (`lifecycle.py`, walked on M1 bars)
PENDING → OPEN → **TP** / **SL**, or **INVALIDATED** (`exit_on_choch`, default off: market
exit at the close of a 1h CHoCH against the open trade, after the fill), **TIME_STOP**
(`time_stop_min`, **0 = off** since SMC-2.1; signals created with a time stop keep it),
**TIMEOUT** (`max_hold_min` 1440). Without a fill: EXPIRED, MISSED (the target traded before
the fill). Conservative reading: a stop beats the target in the same minute, the target and
the stop come before an invalidation in the same minute; no target in the fill minute. R = net PnL per unit / risk
unit, risk unit = stop distance + entry fee + slippage + taker fee on the slipped stop fill,
so a stop-out is exactly −1R.

## Shared wallet
- Sizing at entry: risk `risk_pct` (1 %) of the current wallet balance; quantity rounded down
  to the market's step; margin (notional / `max_leverage`) must fit the free balance
  (MARGIN_LIMITED / NO_FREE_MARGIN).
- Booking: the exit is a REALIZED_PNL row in `smc_wallet_ledger` with its kind (`part`) and
  its fees (entry fee + exit fee).

## Chart
- Default **Setups** view: only the setups the engine can trade — fresh, valid, unexpired 1h
  setups with the 4h bias within `chart_near_atr` (3) × ATR(1h) of price, or tied to a
  pending / open position; positions' setups first, then the nearest, `chart_top_n` (3). Each
  with its FVG, the sweep (dashed line at the swept level, dot at the wick), its BOS / CHoCH,
  the planned SL, the TP at its front-run price and the HH / LL it refers to, the 50 % line of
  the dealing range (dotted). Label: timeframe · age · state · net R · cost share; colour by
  state (waiting = direction colour, armed, pending, open). Setups rejected by a filter are
  not shown here. Positions show entry, SL, TP and the order expiry (and the time stop when
  one is set).
- **All zones (debug)**: every zone, every gap behind an order block, every BOS / CHoCH and
  liquidity level and every setup in any state (used, invalid, expired, rejected) with its
  reasons.

## Database
Migration `0020_smc_ladder`: `smc_signals` gains `targets` (source, net R, level), `parts`
(the exit), `qty_open`, `realized_r`, `version`; `smc_wallet_ledger` gains `part` and `fees`.
Migration `0021`: the unused `tp1` / `tp2` / `tp3` columns are dropped.
Migration `0022` (SMC-2.3): `smc_signals.funding` (funding paid per unit so far; a restart
continues from it) and `funding_usdt`; ledger kind `FUNDING`. Signal details store the 5m
confirmation (break, reaction origin, the 5m FVG / OB).
Signals of SMC-1.0 keep their single TP and finish under the old rule.

## Parameters
All in `config/*.yaml` → `smc` (defaults: `src/sp2l/smc/model.py`), listed on the dashboard's
Strategy page with their hash. Every signal stores the hash of the parameters that made it.
SMC-2.1 keys: `tp_front_run_atr` 0.05, `require_discount` true, `min_net_rr` 2,
`max_cost_frac` 0 (off), `exit_on_choch` false, `time_stop_min` 0 (off).
SMC-2.2 keys: `confirm_entry`, `confirm_window_min`, `entry_ref` (`fvg_mid`), `sl_mode`
(`ob_height`), `tp_rr` 3. SMC-2.3 keys (code default → SMC-2.3 value): `htf_grid` tehran →
utc, `confirm_in_zone` false → true, `entry_ref` ob_edge → ltf_fvg_ce, `sl_mode` wick →
structure, `sl_buffer_atr` 0.2, `tp_mode` hh_ll → liquidity, `discount_ref` target →
displacement, `min_net_rr` 2, `max_cost_frac` 0 → 0.2, `liq_buffer_r` 0 → 1,
`maint_margin_rate` 0.005; `costs.funding_rate` 0, `costs.funding_interval_h` 8.

## Reason codes (a setup evaluated when its order would exist)
| Code | Meaning |
|---|---|
| NO_BIAS / BIAS_MISMATCH | the 4h bias is undefined / against the trade |
| NO_CONFIRM | no 5m BOS / CHoCH from inside the zone within `confirm_window_min` |
| ZONE_INVALID | the OB was closed through (15m, or a 5m close while waiting for the confirmation) or expired before the order |
| NOT_FRESH | the OB was already touched (first touch only) |
| BAD_STOP | the stop would sit on the wrong side of the entry |
| NO_TARGET | no HH / LL (hh_ll) or unswept liquidity (liquidity) beyond the entry |
| NOT_DISCOUNT / NOT_PREMIUM | entry (target) / the OB's proximal edge (displacement) not in the discount / premium half of the dealing range |
| LOW_NET_RR | net R at the TP below `min_net_rr` |
| COST_HEAVY | fees + slippage above `max_cost_frac` of the stop (when on) |
| LEVERAGE | the size would need more than `max_leverage` |
| LIQ_LIMITED | the liquidation would lie within `liq_buffer_r` × the stop beyond the stop even at the smallest size |

## SMC-1.0 (retired 2026-10-05) — evidence
SMC-1.0 traded M1 BOS / CHoCH triggers reacting inside 4h / 1h order blocks with a single
fixed 1:3 target. Its results, for the record:

### Evidence for the SMC-1.0 configuration (270 days, 2026-01-07 .. 10-04, after fees)
| Setup | BTC/USDT | XRP/USDT |
|---|---|---|
| previous: market entry, liquidity target, 6 h, all zones | −206.7R / 680, PF 0.49 | −189.1R / 670, PF 0.54 |
| limit at the 4h/1h zone edge, 1:3, 24 h | −34.4R / 191, PF 0.73 | −33.9R / 181, PF 0.73 |
| **+ zone filters (OB-only POI, ≥ 0.5 ATR zones)** — deployed | **−5.0R / 67, PF 0.88** | **−13.0R / 63, PF 0.69** |

Still not profitable: the least-loss setup found. Also measured (90 days): FVGs are filled
93–99 % of the time and react worse than random ranges as entry zones; OBs react 5–10
percentage points better than random ranges on 15m / 1h (none on 5m); zones below half an ATR
behave like random ranges; a break-even stop at +1R and the higher-timeframe sweep filter
did not help; the "standard" liquidity target with ≥ 1:2 was worse than a fixed 1:2 / 1:3.

### Backtest findings, BTC and XRP (2026-10-04, 30 days, after fees, FVG = fill rule)
| Market | Trades | Win rate | Total | Profit factor |
|---|---|---|---|---|
| BTC/USDT | 60 | 20 % | −30.9R | 0.26 |
| XRP/USDT | 42 | 17 % | −21.3R | 0.35 |

### Earlier backtest findings (2026-10-04, XAUT/USDT, Tabdeal fees 0.08 % maker / 0.095 % taker)
Fees are ~7 USDT per unit round trip, about the size of a typical 15m–1h structural stop.
Over 60 days no tested variant was profitable after fees (default: PF ≈ 0.15–0.26); before
fees the default model is close to break-even (PF ≈ 0.95). The dashboard's Performance page
re-runs the backtest of the parameters in force. Treat live signals as experimental.

