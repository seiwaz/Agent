"""0025 manual trades placed from the dashboard chart on Tabdeal futures.

manual_trades: one row per trade (a LIMIT entry at the drawing's entry, then the position's
SL / TP once filled). status: PENDING (order resting) -> ACTIVE (position open) -> CLOSED, or
CANCELED / REJECTED / ERROR. `events` is the trade's own log (every exchange call and its
outcome), newest last.

Revision ID: 0025
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE manual_trades (
        id            bigserial   PRIMARY KEY,
        symbol        text        NOT NULL,
        side          text        NOT NULL CHECK (side IN ('LONG', 'SHORT')),
        status        text        NOT NULL CHECK (status IN
                      ('PENDING', 'ACTIVE', 'CLOSED', 'CANCELED', 'REJECTED', 'ERROR')),
        leverage      int         NOT NULL,
        margin_usdt   numeric(38,18) NOT NULL,
        qty           numeric(38,18) NOT NULL,
        entry         numeric(38,18) NOT NULL,
        sl            numeric(38,18) NOT NULL,
        tp            numeric(38,18) NOT NULL,
        client_id     text        NOT NULL UNIQUE,
        order_id      bigint,
        position_id   bigint,
        filled_qty    numeric(38,18) NOT NULL DEFAULT 0,
        avg_entry     numeric(38,18),
        sltp_qty      numeric(38,18) NOT NULL DEFAULT 0,
        exit_price    numeric(38,18),
        realized_pnl  numeric(38,18),
        close_reason  text,
        drawing_id    text,
        last_error    text,
        events        jsonb       NOT NULL DEFAULT '[]'::jsonb,
        created_at    timestamptz NOT NULL DEFAULT now(),
        filled_at     timestamptz,
        closed_at     timestamptz,
        updated_at    timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX manual_trades_open ON manual_trades (symbol) WHERE status IN ('PENDING', 'ACTIVE');
    """)


def downgrade() -> None:
    op.execute("DROP TABLE manual_trades;")
