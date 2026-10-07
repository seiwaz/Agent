# System B — plan 3: levers to raise the return (written 2026-10-07, before any run)

Question: which known levers raise the return of daily trend following on crypto, measured
risk-adjusted, without fitting to the data? The levers come from the literature, not from our
data:

- **Ensemble + volatility targeting + midpoint trailing stop:** Zarattini, Pagani & Barbon,
  *Catching Crypto Trends* (SSRN 5209907, 2025). Nine Donchian lookbacks, positions sized to a
  25 % annual volatility target. Reported for BTC: CAGR 30 %, Sharpe 1.56, max drawdown 19 %.
- **Diversification across 10–20 coins:** Man Group, *In Crypto We Trend* (2024): the best
  Sharpe comes with about 10–15 coins. Zarattini et al.'s top-20 rotation reports an alpha of
  about 11 % a year over BTC.
- **Pyramiding:** the original Turtle rules (up to 4 units, one added every ½ N).
- **Yield on idle cash:** stablecoin yields of 2–8 % a year (descriptive overlay only).

## Data and split

- BTC: `data/binance_vision_BTCUSDT_1d.csv` (spot, 2017-08-17 → 2026-10-06).
- Coin universe: Binance spot daily klines (bulk archive) for these candidates. The list is
  fixed now and includes delisted and renamed coins to limit survivorship bias:
  BTC ETH BNB XRP ADA SOL DOGE DOT LTC LINK BCH BCHABC BCC BCHSV TRX AVAX MATIC POL XLM ETC EOS
  ATOM UNI FIL LUNA FTT NEO IOTA XMR VET ICP NEAR SHIB WAVES QTUM ONT ZEC DASH APT ARB OP SUI PEPE
  FTM AAVE HBAR ALGO SAND AXS WLD INJ TON TRUMP ENA (all against USDT). A gap of more than 7 days
  in a series (delisting, relaunch) ends it; whatever follows the gap is a separate asset. A
  position in an ending series is sold at its last close.
- **Discovery: up to 2022-12-31. Holdout: 2023-01-01 → 2026-10-06, used once, only for the
  variants selected on discovery.** Caveat: BTC System B results after 2023-09 were already seen
  (reports 1–2), so the BTC holdout is not untouched. The portfolio holdout is.
- Costs: Tabdeal taker 0.095 % + slippage 0.0326 % on every traded notional. Spot: long only,
  gross exposure ≤ 1, no funding. No cash yield in the tests.

## Variants (6, fixed)

| id | what | settings |
|---|---|---|
| V0 | System B baseline (report 2) | entry 20 / exit 10, stop 2 × ATR(20), risk 3 %, spot |
| V1 | V0 + pyramiding (Turtle) | `max_units=4`, `add_atr=0.5` |
| V2 | BTC ensemble, vol target 25 % | see "Ensemble rules" |
| V3 | BTC ensemble, vol target 50 % | same, target 50 % |
| P1 | top-10 coin portfolio, ensemble per coin | coin target 50 %, weight / 10, gross ≤ 1 |
| P2 | top-10 coin portfolio, more risk | coin target 100 %, weight / 10, gross ≤ 1 |

### Ensemble rules (V2, V3, P1, P2)

- Lookbacks L ∈ {5, 10, 20, 30, 60, 90, 150, 250, 360} days. For each L, separately:
  - **long** from the close where the close is ≥ the highest close of the previous L days;
  - its trailing stop = max(the previous stop, (highest close + lowest close of the last L
    days) / 2);
  - **flat** from the close below the stop.
  A lookback is flat until the coin has L days of history.
- Signal s = the mean of the 9 states (0 … 1).
- σ = the annualized standard deviation (√365) of the last 30 daily close-to-close returns.
- Target weight = s × min(target / σ, 1), and for P1 / P2 divided by 10. If the sum over coins
  exceeds 1, all weights are scaled down to sum to 1.
- Decided at the close and traded at the next open, only when the target differs from the
  current weight by more than 0.05 (BTC) / 0.01 (each coin in P1 / P2). Costs apply to the
  traded notional.
- Universe (P1 / P2): on the first day of each month, the 10 candidates with the largest
  30-day dollar volume (close × volume), among those with at least 60 days of history.
  A coin that leaves the universe gets a target of 0.

## Selection (discovery) and confirmation (holdout)

- **Candidate:** Sharpe on discovery ≥ V0's discovery Sharpe + 0.10, and Calmar on discovery ≥
  V0's.
- **Holdout pass** (each candidate, one run): Sharpe ≥ V0's holdout Sharpe, CAGR > 0, and max
  drawdown ≤ ½ of BTC buy & hold's over the holdout.
- Reported for every variant on both periods: CAGR, max drawdown, Sharpe, Calmar, exposure,
  turnover, costs. Also reported, but not used for any decision: a 4 %/year yield on the idle
  cash.

Six variants are tested. A lever counts only if it passes on discovery **and** in the holdout.
A higher CAGR at a higher drawdown is not counted as an improvement: any variant can raise its
CAGR by taking more risk. The question is the return per unit of risk.
