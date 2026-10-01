# SP2L V5

Deterministic SP2L trading engine (M1 execution, M5 context). The authoritative spec is `spec/` (precedence:
`spec/SP2L_RULES.yaml` first). Open decisions: `docs/BLOCKERS.md`. Tabdeal API notes:
`docs/tabdeal_endpoint_map.md`.

Live automation is **disabled** until every runtime-validation item passes (`live_automation_status` view).

## Running the collector
```sh
uv run python -m sp2l collect                 # market data only (public stream, no API key)
uv run python -m sp2l shadow                  # Shadow runner (separate process); needs costs in config/runtime.yaml
uv run python -m sp2l collect --duration 300  # stop after 5 minutes
```
The collector writes raw trades, M1/M5 candles (with data-quality flags), coverage gaps and the
**market-event journal** (`market_events`: proven trades, M1 results, GAP events in exact order) to the
database in `config/runtime.yaml`. Coverage is proven with ping/pong; minutes without proven coverage are
`DATA_GAP`. The Shadow runner is a separate process that consumes the journal. On restart it restores its
last checkpoint and replays the journal from its cursor (V5.5 B39); a coverage gap while a setup is exposed
finalizes it as `AMBIGUOUS_DATA_GAP`.

## Feed redundancy (B46 test)
`config/runtime.yaml` → `collector.connections: [A, B]`, `stagger_s: 600`: two independent stream
connections whose pong-proven coverage is merged. `uv run python -m sp2l feed-report --hours 24` prints
the measurement (disconnects, close reasons, reconnect durations, correlation, merged gaps, duplicate
rate, conflicts, longest M5 segment vs the 150-bar target).

## Always-on services (macOS LaunchAgents, auto-restart)
```sh
scripts/collector-service.sh install|restart|status|logs|uninstall   # public-stream collector
scripts/api-service.sh install|restart|status|logs|uninstall         # API + WebUI, http://127.0.0.1:8765
scripts/shadow-service.sh install|...                               # Shadow runner (after costs are set)
```
Logs: `~/Library/Logs/sp2l/`. Every (re)start is recorded in `collector_runs`, and health heartbeats in
`collector_heartbeats` (visible in the WebUI).

## Read-only exchange validation
`uv run python -m sp2l validate-readonly` runs GET-only authenticated checks (server time, exchange info,
balance, positions, position risk, leverage/margin, open orders) and records redacted evidence in
`runtime_validation_runs`. It cannot place, cancel or modify anything.

Credentials live outside the repo in `~/.config/sp2l/tabdeal.env` (directory 700, file 600). They are
never logged, persisted or shown in the WebUI.

## Development
```sh
uv sync
SP2L_DATABASE_URL="postgresql+psycopg:///sp2l_dev?host=/tmp" uv run alembic upgrade head
uv run pytest          # tests/db uses sp2l_test (override: SP2L_TEST_DATABASE_URL)
uv run ruff check . && uv run mypy
```
