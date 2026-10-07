# SMC Console

Smart Money Concepts engine for Tabdeal futures — **BTC/USDT and XRP/USDT on one shared
simulated 100 USDT wallet** — with a live dashboard.
SMC-2.0 top-down model: 4h bias → 1h setup in strict order (liquidity sweep → break of
structure by close → order block = last opposite candle with the FVG right after it) → limit
at the order block, armed when price first trades into the FVG → stop one tick beyond the OB
wick, one target at the previous HH / LL; fills and exits walked on M1, net of fees.
The strategy is specified in `docs/SMC_STRATEGY.md`. **No orders are ever sent**: signals and
their lifecycle are recorded.

## Processes
```sh
uv run python -m sp2l --symbol BTCUSDT collect   # live market data of one market (one per market)
uv run python -m sp2l smc              # SMC engine: every market, shared wallet, history from Tabdeal
uv run python -m sp2l api --port 8765  # read-only API + WebUI
uv run python -m sp2l --symbol XRPUSDT backtest --days 30 [--set tp_ref=swing --set confirm_exec=true]
```
The engine loads `smc.history_days` of 1-minute history from Tabdeal's chart on start and tops
it up every minute, so every timeframe is complete immediately; live collector candles take
precedence where they exist. It runs without the collector too (chart then updates per minute).

## Dashboard
- **Chart** — market switch (BTC / XRP), timeframe switch (1m…4h), full-screen button.
  Default "Setups" view: only the tradable 1h setups (OB by state, its FVG, the sweep, the
  break, planned SL / TP) and the positions (entry, SL, TP with net R, time stop);
  "All zones (debug)" shows every zone, gap, break, liquidity level and setup. Position
  ticket, top-down ladder, setup list and the setup log.
- **Signals** — full history with the lifecycle timeline of each signal; "Show on chart".
- **Performance** — the shared wallet (balance, equity, fees, ledger, per-market results) and
  the backtest of the parameters in force per market (exit per trade).
- **Strategy** — the model and every parameter (with its hash), factors and reason codes.
- **System** — engine heartbeat, history coverage, collector and data quality.

## Services
- macOS: `scripts/collector-service.sh`, `scripts/smc-service.sh`, `scripts/api-service.sh`
  (`install|restart|status|logs|uninstall`).
- Linux server: `deploy/smc-collector@.service` (one instance per market:
  `smc-collector@BTCUSDT`, `smc-collector@XRPUSDT`), `deploy/smc-engine.service`,
  `deploy/smc-api.service` (WebUI on port 3000).

## Read-only exchange validation
`uv run python -m sp2l validate-readonly` runs GET-only authenticated checks. Credentials live
outside the repo in `~/.config/sp2l/tabdeal.env` (directory 700, file 600).

## System B (trend following, backtest)
Daily Donchian trend following on spot, long only (`src/sp2l/trend/`, spec and pre-declared
evaluation rules in `docs/TREND_STRATEGY.md`):
```sh
uv run python scripts/fetch_daily.py --source binance --symbol BTCUSDT   # daily CSV into data/
uv run python -m sp2l trend-backtest --csv data/binance_BTCUSDT_1d.csv --grid --out docs/trend/btc
```

## Development
```sh
uv sync
SP2L_DATABASE_URL="postgresql+psycopg:///sp2l_dev?host=/tmp" uv run alembic upgrade head
uv run pytest          # tests/db uses sp2l_test (override: SP2L_TEST_DATABASE_URL)
uv run ruff check . && uv run mypy
```
