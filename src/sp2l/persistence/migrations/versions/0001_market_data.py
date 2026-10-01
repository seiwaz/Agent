"""0001 market data: raw trades, canonical M1/M5 candles, data gaps (MKT-04/05, DATA-01).

Revision ID: 0001
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE raw_trades (
        symbol      text           NOT NULL,
        trade_id    text           NOT NULL,
        exch_ts     timestamptz    NOT NULL,
        recv_ts     timestamptz    NOT NULL,
        price       numeric(38,18) NOT NULL CHECK (price > 0),
        qty         numeric(38,18) NOT NULL CHECK (qty > 0),
        taker_side  text           CHECK (taker_side IN ('BUY','SELL')),
        raw         jsonb,
        PRIMARY KEY (symbol, exch_ts, trade_id)
    ) PARTITION BY RANGE (exch_ts);
    CREATE TABLE raw_trades_default PARTITION OF raw_trades DEFAULT;
    CREATE INDEX raw_trades_symbol_trade_id ON raw_trades (symbol, trade_id);
    """)
    for tf in ("1m", "5m"):
        op.execute(f"""
        CREATE TABLE candles_{tf} (
            id           bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            symbol       text           NOT NULL,
            open_time    timestamptz    NOT NULL,
            close_time   timestamptz    NOT NULL,
            open         numeric(38,18) NOT NULL,
            high         numeric(38,18) NOT NULL,
            low          numeric(38,18) NOT NULL,
            close        numeric(38,18) NOT NULL,
            volume       numeric(38,18) NOT NULL CHECK (volume >= 0),
            trade_count  integer        NOT NULL CHECK (trade_count >= 0),
            {
            "synthetic_no_trade boolean NOT NULL DEFAULT false,"
            if tf == "1m"
            else "synthetic_m1_count integer NOT NULL DEFAULT 0 CHECK (synthetic_m1_count BETWEEN 0 AND 5), "
            "synthetic_fraction numeric GENERATED ALWAYS AS (synthetic_m1_count / 5.0) STORED, "
            "real_trade_count integer GENERATED ALWAYS AS (trade_count) STORED,"
        }
            finalized_at timestamptz    NOT NULL,
            source_hash  text           NOT NULL,
            PRIMARY KEY (symbol, open_time),
            CHECK (close_time > open_time),
            CHECK (low <= open AND open <= high AND low <= close AND close <= high)
        );
        """)
    op.execute("""
    CREATE TABLE data_gaps (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        symbol       text        NOT NULL,
        timeframe    text        NOT NULL CHECK (timeframe IN ('trades','1m','5m')),
        kind         text        NOT NULL DEFAULT 'DATA_GAP' CHECK (kind IN ('DATA_GAP','LATE_TRADE')),
        gap_start    timestamptz NOT NULL,
        gap_end      timestamptz NOT NULL,
        reason       text        NOT NULL,
        detected_at  timestamptz NOT NULL DEFAULT now(),
        resolved_at  timestamptz,
        CHECK (gap_end > gap_start)
    );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE data_gaps, candles_5m, candles_1m, raw_trades CASCADE")
