# SMC engine — strategy specification (SMC-2.0)

Markets: **BTC/USDT and XRP/USDT**, traded together on **one shared simulated wallet**
(100 USDT). Each market has its own price tick / quantity step (`instruments`). No orders are
sent: signals and their simulated lifecycle are recorded for review. SMC-2.0 replaces the
SMC-1.0 M1-trigger model (2026-10-05, owner's intraday entry model); the SMC-1.0 evidence is
kept at the end of this file.

## Data (no live warmup)
- Every bar comes from one merged 1-minute series: canonical live candles (`candles_1m`,
  collector) where they exist, otherwise **Tabdeal chart history** (`exchange_m1`), fetched
  on demand for `history_days` (35) when the engine starts and topped up every minute.
- Higher timeframes are aggregated from that series in SQL, on Tabdeal's own grid: 1m–15m
  UTC-aligned, **1h and 4h on Tehran time** (bars open at hh:30 UTC).
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
5. **Targets (ladder)** — from the market alone, **never derived from the stop** (owner rule
   2026-10-05; no R-based fallback, no R:R or cost filter):
   - TP1 = nearest internal liquidity / previous swing: the nearest unswept confirmed swing
     high (low) of the **15m or 1h** timeframe beyond the entry. `tp1_frac` (50 %) closes; the
     stop moves to break-even net of fees. No such level: no trade (NO_TARGET).
   - TP2 = the near edge of the next **unfilled 1h FVG** beyond TP1. `tp2_frac` (30 %) closes.
     None: its share stays in the runner.
   - TP3 (runner) = external liquidity beyond TP2 (or TP1): previous UTC day / ISO week high
     (low) not yet taken, or 1h equal highs (lows). The runner trails one tick beyond the last
     confirmed 15m swing (only ever tightened) from TP2 on, or from TP1 on when there is no
     TP2. No TP3: the runner ends on the trailing stop or `max_hold_min`.
   - The net R of each target after fees is computed and shown, for information only.
6. **Size** — risk `risk_pct` of the wallet over the stop distance plus costs; advisory size
   ≤ `max_leverage` (LEVERAGE).
7. **Capacity** — `max_active` (1) per market, `max_positions` (2) across markets; orders are
   never created retroactively (only within 3 minutes of the arming minute / confirmation).

## Lifecycle (`lifecycle.py`, walked on M1 bars)
PENDING → OPEN → **TP1** (50 % out, stop at break-even) → **TP2** (30 % out, trailing) →
**TP** (TP3, rest out). Exits of the rest: **SL** (before TP1, exactly −1R), **BE** (after TP1),
**TRAIL** (the trailing stop of the runner), **TIME_STOP** (neither TP1 nor SL within `time_stop_min` = 180 min =
12 × 15m of the fill, market exit), **TIMEOUT** (`max_hold_min` 1440). Without a fill:
EXPIRED, MISSED (TP1 traded before the fill).
Conservative reading: a stop beats any target in the same minute; no target in the fill
minute; a moved stop (break-even, trailing) counts from the next minute.
R = Σ partᵢ × net per unitᵢ / risk unit, risk unit = stop distance + entry fee + slippage +
taker fee on the slipped stop fill, so a full stop-out is exactly −1R.

## Shared wallet
- Sizing at entry: risk `risk_pct` (1 %) of the current wallet balance; quantity rounded down
  to the market's step; margin (notional / `max_leverage`) must fit the free balance
  (MARGIN_LIMITED / NO_FREE_MARGIN). The ladder shares are whole quantity steps; a share that
  rounds to nothing passes to the next part.
- Booking: **every exit** (TP1, TP2, the final one) is a REALIZED_PNL row in
  `smc_wallet_ledger` with its exit (`part`) and its own fees (its share of the entry fee +
  its exit fee). The signal's PnL / fees are the sums. Margin in use shrinks with the open
  quantity.

## Chart
- Default **Setups** view: only the setups the engine can trade — fresh, valid, unexpired 1h
  setups with the 4h bias within `chart_near_atr` (3) × ATR(1h) of price, or tied to a
  pending / open position; positions' setups first, then the nearest, `chart_top_n` (3). Each
  with its FVG, the sweep (dashed line at the swept level, dot at the wick), its BOS / CHoCH,
  the planned SL and TP1–TP3 (dotted, with their net R). Label: timeframe · age · state;
  colour by state (waiting = direction colour, armed, pending, open). Positions show entry, SL
  (break-even / trailing), TP1–TP3 with net R and the time stop / order expiry.
- **All zones (debug)**: every zone, every gap behind an order block, every BOS / CHoCH and
  liquidity level and every setup in any state (used, invalid, expired).

## Database
Migration `0020_smc_ladder`: `smc_signals` gains `tp1`, `tp2`, `tp3`, `targets` (source and
net R per target), `parts` (the exits), `qty_open`, `realized_r`, `version`; `tp` (= TP3) may
be NULL; the active index covers TP1 / TP2. `smc_wallet_ledger` gains `part` and `fees`.
Signals of SMC-1.0 keep their single TP and finish under the old rule.

## Parameters
All in `config/*.yaml` → `smc` (defaults: `src/sp2l/smc/model.py`), listed on the dashboard's
Strategy page with their hash. Every signal stores the hash of the parameters that made it.

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

