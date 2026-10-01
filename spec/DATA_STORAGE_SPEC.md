# Data Storage V5

Use PostgreSQL with migrations.

Persist at minimum:
- raw_trades
- candles_1m
- candles_5m
- confirmed_m5_pivots
- pgaps
- spikes
- candidates
- context_snapshots
- exhaustion_snapshots
- e1_revisions
- orders
- fills
- position_snapshots
- strategy_events
- shadow_sessions
- counterfactual_outcomes

Every candidate stores:
- all raw metric values
- boolean gate outputs
- primary and secondary reason codes
- spec version
- exact evaluation timestamp
- M1/M5 candle IDs used

Rejected candidates are never deleted.

## V5.1
- Data-gap records mark affected market data incomplete (B25).
- Candidates and snapshots store `MARGIN_CAPPED_QTY`, `LIQ_UNVERIFIED`, the rounded E2 and the E2/total modeled risk (B17, B18, B23).
