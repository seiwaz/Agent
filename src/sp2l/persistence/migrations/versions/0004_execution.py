"""0004 orders, order events, fills, position snapshots (E1-06, SLTP-03, E2-04, TAB-02).

- one active order per (candidate, leg): at most one live E1 revision, one E2, one SL and
  exactly one TP leg (SLTP-03);
- entry legs E1/E2 are never reduce-only (E2-04: add-to-position, never reducing);
- client_order_id is unique, so a retry can never create a duplicate order (TAB-02).

Revision ID: 0004
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

ACTIVE = "('PENDING_SUBMIT','NEW','PARTIALLY_FILLED','PENDING_CANCEL','UNKNOWN')"


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE orders (
        id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        candidate_id       uuid           NOT NULL REFERENCES candidates(id),
        mode               text           NOT NULL CHECK (mode IN ('BACKTEST','REPLAY','SHADOW','LIVE')),
        leg                text           NOT NULL CHECK (leg IN ('E1','E2','SL','TP')),
        leg_id             text           NOT NULL,
        revision_id        integer        NOT NULL DEFAULT 0,
        client_order_id    text           NOT NULL UNIQUE,
        exchange_order_id  text,
        side               text           NOT NULL CHECK (side IN ('BUY','SELL')),
        order_type         text           NOT NULL,
        price              numeric(38,18),
        stop_price         numeric(38,18),
        qty                numeric(38,18) NOT NULL CHECK (qty > 0),
        reduce_only        boolean,
        status             text           NOT NULL CHECK (status IN
            ('PENDING_SUBMIT','NEW','PARTIALLY_FILLED','FILLED','PENDING_CANCEL',
             'CANCELED','REJECTED','EXPIRED','UNKNOWN')),
        executed_qty       numeric(38,18) NOT NULL DEFAULT 0 CHECK (executed_qty >= 0),
        avg_fill_price     numeric(38,18),
        created_at         timestamptz    NOT NULL,
        updated_at         timestamptz    NOT NULL,
        raw                jsonb,
        CHECK (executed_qty <= qty),
        CHECK (leg NOT IN ('E1','E2') OR reduce_only IS NOT TRUE),
        UNIQUE (candidate_id, leg, revision_id)
    );
    CREATE UNIQUE INDEX orders_one_active_leg ON orders (candidate_id, leg)
        WHERE status IN {ACTIVE};

    CREATE TABLE order_events (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        order_id     bigint      NOT NULL REFERENCES orders(id),
        ts           timestamptz NOT NULL,
        source       text        NOT NULL CHECK (source IN ('LOCAL','EXCHANGE_WS','EXCHANGE_REST','SIMULATOR')),
        status       text        NOT NULL,
        executed_qty numeric(38,18) NOT NULL,
        raw          jsonb
    );

    CREATE TABLE fills (
        id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        order_id          bigint         NOT NULL REFERENCES orders(id),
        exchange_fill_id  text           NOT NULL,
        ts                timestamptz    NOT NULL,
        price             numeric(38,18) NOT NULL CHECK (price > 0),
        qty               numeric(38,18) NOT NULL CHECK (qty > 0),
        fee               numeric(38,18) NOT NULL,
        fee_asset         text           NOT NULL,
        is_maker          boolean,
        raw               jsonb,
        UNIQUE (order_id, exchange_fill_id)
    );

    CREATE TABLE position_snapshots (
        id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        symbol             text           NOT NULL,
        candidate_id       uuid           REFERENCES candidates(id),
        mode               text           NOT NULL CHECK (mode IN ('BACKTEST','REPLAY','SHADOW','LIVE')),
        ts                 timestamptz    NOT NULL,
        source             text           NOT NULL CHECK (source IN ('EXCHANGE','SIMULATOR')),
        qty                numeric(38,18) NOT NULL,
        entry_price        numeric(38,18),
        liquidation_price  numeric(38,18),
        margin_mode        text,
        leverage           integer,
        raw                jsonb
    );
    """)
    for table in ("order_events", "fills", "position_snapshots"):
        op.execute(f"""
        CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION sp2l_reject_mutation();
        """)


def downgrade() -> None:
    op.execute("DROP TABLE position_snapshots, fills, order_events, orders")
