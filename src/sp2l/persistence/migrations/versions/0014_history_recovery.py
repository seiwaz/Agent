"""0014 V5.8 three-tier recovery: Tabdeal chart-history lineage.

- candles_1m/5m.quality gains RECENT_TRADES_REPAIRED (the V5.8 name of REST_REPAIRED; legacy
  rows keep their recorded value) and TABDEAL_HISTORY_REPAIRED.
- candles_1m/5m.trade_count becomes nullable: NULL = UNKNOWN (a CANDLE_HISTORY_REPAIR bar
  has no trade count; it is never invented).
- candles_1m.repair_type (EXACT_RAW_REPAIR | CANDLE_HISTORY_REPAIR); candles_5m.history_m1_count.

Revision ID: 0014
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

NEW = (
    "'LIVE_RECONCILED','RECENT_TRADES_REPAIRED','TABDEAL_HISTORY_REPAIRED','REST_REPAIRED',"
    "'LIVE_WS_ONLY','SYNTHETIC_NO_TRADE','LIVE_PROVEN_RAW','REPAIRED_TABDEAL','CONFLICTED'"
)
OLD = (
    "'LIVE_RECONCILED','REST_REPAIRED','LIVE_WS_ONLY','SYNTHETIC_NO_TRADE',"
    "'LIVE_PROVEN_RAW','REPAIRED_TABDEAL','CONFLICTED'"
)


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
    ALTER TABLE candles_1m ALTER COLUMN trade_count DROP NOT NULL;
    ALTER TABLE candles_5m ALTER COLUMN trade_count DROP NOT NULL;
    ALTER TABLE candles_1m ADD COLUMN repair_type text
        CHECK (repair_type IN ('EXACT_RAW_REPAIR','CANDLE_HISTORY_REPAIR'));
    ALTER TABLE candles_5m ADD COLUMN history_m1_count integer NOT NULL DEFAULT 0
        CHECK (history_m1_count BETWEEN 0 AND 5);
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE candles_5m DROP COLUMN history_m1_count;
    ALTER TABLE candles_1m DROP COLUMN repair_type;
    """)
    _quality(OLD, "NOT VALID")
