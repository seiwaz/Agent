# Tabdeal chart history — tier-3 recovery source (V5.8)

Read-only and public. It is the same data Tabdeal's own futures chart (TradingView datafeed) displays. No other exchange is ever used.

| Property | Value (measured 2026-09-28 from the server) |
|---|---|
| Endpoint | `GET https://api-web.tabdeal.org/special-margin/plots/history/` |
| Auth | None (public, unauthenticated GET; allow-listed in `marketdata/tabdeal_public.py`) |
| Market identity | `symbol=BTC_USDT&first_currency_symbol=BTC&second_currency_symbol=USDT` (special-margin = the futures market SP2L trades) |
| Interval | `resolution=1` (also 5, …). SP2L uses 1 and builds M5 from M1 (MKT-05) |
| Range | `from`, `to` = epoch seconds, inclusive; `countback` = number of bars |
| Pagination | None needed: one request returned 7,200 1-minute bars (5 days). The collector requests the gap plus a 15-minute overlap |
| Depth / retention | 1-minute bars returned for every probe back to 90 days |
| Fields | `time` (bar open, epoch s, UTC), `open`, `high`, `low`, `close`, `volume` (BTC). Envelope `{data: [...], no_data: bool}` |
| Trade count | **Not provided.** It is stored as NULL (UNKNOWN) and never invented |
| Time semantics | Trades are bucketed by Tabdeal **record** time (`created`), which trails the stream time by −25…+653 ms |
| Bar model | TradingView-continuous: `open` = previous bar's close; `high`/`low` include that open |
| Forming bar | The response includes the current, still-forming minute. It is only used once `minute + 60 s + 5 s` has passed |
| Missing minutes | None in 24 h (no zero-volume bars observed). A minute absent from a response fails the repair (no synthesis) |

## Validation against reconciled canonical M1 (scripts/history_validation.py)

The window was 2026-09-27 15:54 → 2026-09-28 05:54 UTC: 762 LIVE_RECONCILED minutes.

| Model | Exact (O,H,L,C,V) | Differences |
|---|---|---|
| RAW (bar taken literally) | 205 / 762 | O 547 (continuity open), H 77, L 64, C 21, V 3 |
| CONTINUITY (open = previous close, H/L include it) | **738 / 762 = 96.85 %** | C 21, L 1, V 3 |

The 24 continuity-model differences, reported separately:
- **20 differ only in close order within the minute.** H, L and V are identical, so it is the same set of trades; the chart orders the minute's trades by record time and SP2L by stream time.
- **4 are small and unexplained:**
  - 19:10 L 84763.4 vs 84763.8;
  - 21:29 V 0.12984 vs 0.12545;
  - 03:34 C and V (the minute right after the 03:32 outage);
  - 05:49 V 0.36305 vs 0.35834.
  - The maximum H/L difference is 0.4 USDT.

M5 (resolution 5, continuity model): 151 / 163 exact. The 12 that differ are V 9, C 2, L 1.

**05:25 (the 1/34 mismatch):**
- canonical close 83029.7; chart close 83041.7;
- volume identical (0.11408) and H/L identical, so it is the same trades.
- The last stream-time trade was 05:25:59.088 BUY 83029.7, after two SELLs at 83041.7 (59.030 and 59.075). Tabdeal recorded the 83029.7 fill before them (record − stream offsets differ per trade, up to +653 ms), so the chart's last trade is 83041.7.
- Classification: **close-order-within-minute**. It is not a data error.

## How it is used (V5.8, `marketdata/history.py`)

A gap is sent to tier 3 when tier 2 fails to prove it: either the recent-trades window never reached the gap start, or the gap is longer than 120 s.

A chart response is accepted for a gap only if all of these hold:
1. every gap minute is present, final and structurally valid;
2. at least 5 overlapping canonical minutes are comparable;
3. at least 80 % of them match exactly under the continuity model;
4. no overlapping minute's H/L differs by more than 0.1 %.

Otherwise the repair fails closed: the minutes become DATA_GAP (UNRECOVERED).

Accepted minutes become `TABDEAL_HISTORY_REPAIRED` / `CANDLE_HISTORY_REPAIR`:
- they have OHLCV, trade count UNKNOWN, and no intrabar order;
- they restore M5, ATR, ADX, EMA, CHOP, pivots, trend, regime and range;
- they never produce fills, PullbackStart, SL/TP order or partial fills;
- a setup exposed across them is finalized AMBIGUOUS_DATA_GAP.

Known representational limit: a repaired bar's open is the previous chart close, and its H/L may include it.
- TR/ATR are unaffected (TR already includes the previous close).
- M5 H/L can only widen, toward the previous bucket's close. Measured on 762 minutes: the RAW model differed in H on 10 % of minutes and in L on 8 %.

## V5.12: startup history bootstrap (XAUT_USDT)

**Source facts** (tested live 2026-09-30):
- It is the same feed the Tabdeal futures web chart uses (`tabdeal.org/panel/trade/XAUT_USDT?trade_type=specialMargin`). The page's TradingView datafeed uses `https://api-web.tabdeal.org` + `/special-margin/plots/history/`.
- Query (page key order): `first_currency_symbol=XAUT&second_currency_symbol=USDT&from=<s>&to=<s>&resolution=<r>&countback=<n>&symbol=XAUT_USDT`. The page keeps bars with `from <= time < to`.
- No auth and no paging: one request returns the whole range.
  - Full history at res 1: 136,791 bars from 2026-06-27 09:13 UTC (~13 MB, ~30 s). Res 5: 27,435 bars; res 15: 9,146; res 240: 571.
  - The bootstrap uses a 180 s timeout for res 1 (`history_bootstrap.timeout_s`).
- XAUT_USDT futures were listed around 2026-06-27; earlier ranges return `no_data`. That is the listing date, not a retention cutoff (BTC_USDT goes back to 2025-10-29).
- Supported resolutions: 1, 5, 15, 30, 60, 120, 180, 240, 360, 480, 720, 1D, 1W. `4H` is rejected (HTTP 400); use `240`. The datafeed timezone is Asia/Tehran, so 4h and 1D bars are aligned to Tehran time (4h bars open at 00:30, 04:30, … UTC).
- `XAU_USDT` returns HTTP 500; the market is `XAUT_USDT`.
- Bars follow the continuity model above (open = previous close, H/L include it), carry no trade count, and the last bar is still forming.
- The full res 1 series has 387 missing minutes in 302 runs. Most are single minutes (the chart omits minutes without trades). One real hole: 2026-07-23 18:43 → 18:59 UTC (15 min).
- Fetched res 5 bars do not always equal aggregated res 1 bars (27,193 / 27,429 exact; mostly V/C from record-time bucketing). The bootstrap therefore builds M5 only from the stored M1 (MKT-05) and never stores chart res 5 bars.

**Agreement with our own XAUT canonical minutes** (`scripts/history_validation.py`, 2026-09-30, `docs/history_validation_xaut.json`):
- 298 LIVE_RECONCILED minutes over 6 h: **95.97 %** exact under the continuity model (286/298). BTC was 96.85 %.
- The 12 differences are volume (11), close (8) and one H/L. The worst H/L difference is 1.97 USD (≈ 0.05 %, inside the 0.1 % limit).
- M5 (res 5 vs canonical M5): 65/71 exact.
- The bootstrap's own check (the 60 most recent live minutes) was 60/60 exact on a 7-day dry run that would store 9,595 minutes.

**What the bootstrap does** (`marketdata/bootstrap.py`, `runtime/reconcile.bootstrap_history`; config `collector.history_bootstrap`, default off):
1. Once per collector start, immediately if at least `min_live_minutes` (default 5) live canonical minutes are already stored from any run (otherwise as soon as they exist, checked every 10 s), it fetches res 1 for `[now − max_lookback_s, now]` in one request.
2. It validates the response against the `validate_minutes` most recent live canonical minutes with the tier-3 policy (≥ 5 comparable, ≥ 80 % exact, H/L within 0.1 %). With no live minutes, or on any failure, nothing is written and the normal 150-bar warmup continues (fail closed). A failed fetch is retried a few times.
3. It drops the forming bar (a minute is final only after minute + 60 s + settle).
4. It stores every missing minute before the collector's tail as `TABDEAL_HISTORY_REPAIRED` / `CANDLE_HISTORY_REPAIR` (OHLCV, trade count UNKNOWN; never fills, PullbackStart, SL/TP order or intrabar order). This includes the collector's partial first minute, so the history joins the live series with no hole. Stored minutes are never changed.
5. Missing chart minutes (owner decision 2026-09-30): a run of 1–2 minutes between known minutes becomes a synthetic no-trade minute (B31: O=H=L=C = previous close, volume 0). A longer run stays DATA_GAP (its M5 bucket has no bar).
6. It rebuilds every touched M5 bucket from canonical M1 and records one `gap_repairs` row (`V5.12_HISTORY_BOOTSTRAP`).
7. The Shadow picks the series up without a restart: a new session primes from the stored contiguous M5 tail, and a running session that is still warming up re-heals from it within a minute.

It can also be run by hand: `python -m sp2l --config config/server.yaml bootstrap-history [--lookback-s N] [--apply]` (dry run by default).
