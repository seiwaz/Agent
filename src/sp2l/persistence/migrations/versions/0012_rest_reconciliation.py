"""0012 V5.7 continuous REST reconciliation: lineage qualities and per-source trade counts.

- candles_1m/5m.quality gains LIVE_RECONCILED, REST_REPAIRED, LIVE_WS_ONLY.
- candles_1m: ws_a/ws_b/rest/rest_only trade counts (null for rows written before V5.7).
- raw_trades: source gains REST (canonical REST-only trades); `sources` records which feeds
  delivered each canonical trade (e.g. "A,B,REST"); reconciled_at.

Revision ID: 0012
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

NEW = (
    "'LIVE_RECONCILED','REST_REPAIRED','LIVE_WS_ONLY','SYNTHETIC_NO_TRADE',"
    "'LIVE_PROVEN_RAW','REPAIRED_TABDEAL','CONFLICTED'"
)
OLD = "'LIVE_PROVEN_RAW','SYNTHETIC_NO_TRADE','REPAIRED_TABDEAL','CONFLICTED'"


def _quality(check: str, valid: str) -> None:
    for tf in ("1m", "5m"):
        op.execute(f"""
        ALTER TABLE candles_{tf} DROP CONSTRAINT IF EXISTS candles_{tf}_quality_check;
        ALTER TABLE candles_{tf} ADD CONSTRAINT candles_{tf}_quality_check
            CHECK (quality IN ({check})) {valid};
        """)


def upgrade() -> None:
    _quality(NEW, "")
    op.execute("""
    ALTER TABLE candles_1m
        ADD COLUMN ws_a_trade_count integer,
        ADD COLUMN ws_b_trade_count integer,
        ADD COLUMN rest_trade_count integer,
        ADD COLUMN rest_only_trade_count integer;
    ALTER TABLE raw_trades DROP CONSTRAINT IF EXISTS raw_trades_source_check;
    ALTER TABLE raw_trades ADD CONSTRAINT raw_trades_source_check
        CHECK (source IN ('WS','REST','REPAIR_REST','RECOVERED_CONFLICT_LOG'));
    ALTER TABLE raw_trades ADD COLUMN sources text, ADD COLUMN reconciled_at timestamptz;
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE raw_trades DROP COLUMN sources, DROP COLUMN reconciled_at;
    ALTER TABLE raw_trades DROP CONSTRAINT raw_trades_source_check;
    ALTER TABLE raw_trades ADD CONSTRAINT raw_trades_source_check
        CHECK (source IN ('WS','REPAIR_REST','RECOVERED_CONFLICT_LOG')) NOT VALID;
    ALTER TABLE candles_1m DROP COLUMN ws_a_trade_count, DROP COLUMN ws_b_trade_count,
        DROP COLUMN rest_trade_count, DROP COLUMN rest_only_trade_count;
    """)
    _quality(OLD, "NOT VALID")
