# Eiwaz Trading System

Smart Money Concepts engine for Tabdeal futures — **BTC/USDT and XRP/USDT on one shared
simulated 100 USDT wallet** — with a live dashboard.
SMC-2.0 top-down model: 4h bias → 1h setup in strict order (liquidity sweep → break of
structure by close → order block = last opposite candle with the FVG right after it) → limit
at the order block, armed when price first trades into the FVG → stop one tick beyond the OB
wick, one target at the previous HH / LL; fills and exits walked on M1, net of fees.
The strategy is specified in `docs/SMC_STRATEGY.md`. **The engine sends no orders**: signals and
their lifecycle are recorded. Real orders are placed only from the chart's Trade button.

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
- **Chart** — 5m / 15m / 1h / 4h / 1d with switchable support / resistance, trendlines, OB,
  FVG, BOS, CHoCH, HH / HL / LH / LL, Donchian, RSI and MACD (each with its settings), the trend
  of every timeframe in the top bar, drawing tools (trend line, horizontal line, long / short
  position, price range, path), full screen. Candles come from Tabdeal's chart; scrolling back
  loads older bars down to the listing. **Trade** (⚡) places the selected / last Long or Short
  drawing on Tabdeal futures (leverage asked first; stop and target set once filled); the panel
  under the chart shows open trades of every market (Cancel / Close) and the history. The market
  picker is a searchable combobox over the config's `markets` (15 USDT futures); chart and trading
  work on each, the SMC engine runs on `symbols` (`docs/CHART.md`).
  **Strategy** (top bar of the chart): four rule-based 1h strategies (Donchian 48/26 long only, EMA 50
  pullback, liquidity sweep + BOS + FVG / OB, anchored VWAP), long and short: the current setup,
  every past setup to its target / stop, the checklist of each rule, a backtest after costs, and a
  scan of every market (`docs/PLAYBOOK.md`).
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

## Login
With `auth.enabled` (on in `config/server.yaml`) the dashboard and its API need a login. Users
and their scrypt password hashes live outside the repo in `~/.config/sp2l/dashboard.auth`
(mode 600): `uv run python -m sp2l --config config/server.yaml set-login admin` asks for the
password (run it again to change it; that also ends the user's sessions). Five wrong passwords
from one address lock it out for 15 minutes; sessions last `auth.session_hours`. Plain HTTP
sends the password unencrypted: open the dashboard through the SSH tunnel
(`ssh -p 2266 -L 3000:127.0.0.1:3000 root@<server>`, then http://localhost:3000) or HTTPS.

## Trading key check
`uv run python -m sp2l --config config/server.yaml trade-check [--market XRPUSDT]` tells whether
the Tabdeal API key in `trading.credentials_file` can trade: it reads the wallet and the market's
leverage, then sets the leverage to the value it already has (nothing changes, no order). An
"Access denied" there means the key is read-only or its IP allow-list lacks the server's IP.

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

### Live (paper)
The `trend` / `trend_live` sections of the config (spot, long only, 20 / 10, stop 2 × ATR,
risk 5 %): `uv run python -m sp2l trend` keeps the Tabdeal history current and journals the
engine's decision after each closed UTC day in `trend_journal` (`alembic upgrade head` first);
`uv run python -m sp2l trend-status` prints the state; the dashboard's **Trend** tab shows it.
Services: `scripts/trend-service.sh` (macOS), `deploy/smc-trend.service` (Linux). No orders.

## Development
```sh
uv sync
SP2L_DATABASE_URL="postgresql+psycopg:///sp2l_dev?host=/tmp" uv run alembic upgrade head
uv run pytest          # tests/db uses sp2l_test (override: SP2L_TEST_DATABASE_URL)
uv run ruff check . && uv run mypy
```
