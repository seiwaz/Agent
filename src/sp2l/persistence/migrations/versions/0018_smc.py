"""0018 Smart Money Concepts engine (replaces the SP2L strategy).

- exchange_m1: Tabdeal chart history (1-minute OHLCV), fetched on demand so analysis never
  waits for a live warmup. Canonical live candles (candles_1m) take precedence where present.
- smc_signals: one row per accepted M1 trigger (entry / SL / TP and its lifecycle).
- smc_signal_events: append-only lifecycle trail (CREATED, FILLED, TP, SL, ...).
- smc_runner_state: heartbeat and status of the SMC runner process.

The SP2L strategy tables of 0003-0017 are left untouched (history is kept); nothing writes
to them any more.

Revision ID: 0018
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE exchange_m1 (
        symbol     text           NOT NULL,
        open_time  timestamptz    NOT NULL,
        open       numeric(38,18) NOT NULL,
        high       numeric(38,18) NOT NULL,
        low        numeric(38,18) NOT NULL,
        close      numeric(38,18) NOT NULL,
        volume     numeric(38,18) NOT NULL,
        fetched_at timestamptz    NOT NULL DEFAULT now(),
        PRIMARY KEY (symbol, open_time),
        CHECK (low <= open AND open <= high AND low <= close AND close <= high)
    );

    CREATE TABLE smc_signals (
        id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        symbol      text        NOT NULL,
        key         text        NOT NULL,
        side        text        NOT NULL CHECK (side IN ('LONG', 'SHORT')),
        state       text        NOT NULL,
        created_at  timestamptz NOT NULL,
        entry       numeric(38,18) NOT NULL,
        sl          numeric(38,18) NOT NULL,
        tp          numeric(38,18) NOT NULL,
        risk        numeric(38,18) NOT NULL,
        rr          numeric(38,18),
        net_rr      numeric(38,18),
        score       integer     NOT NULL,
        tp_source   text,
        trigger_kind text       NOT NULL,
        poi_tf      text,
        detail      jsonb       NOT NULL,
        qty         numeric(38,18),
        notional    numeric(38,18),
        leverage    numeric(38,18),
        filled_at   timestamptz,
        closed_at   timestamptz,
        exit_price  numeric(38,18),
        result_r    numeric(38,18),
        last_m1     timestamptz,
        params_hash text        NOT NULL,
        updated_at  timestamptz NOT NULL DEFAULT now(),
        UNIQUE (symbol, key)
    );
    CREATE INDEX smc_signals_recent ON smc_signals (symbol, created_at DESC);
    CREATE INDEX smc_signals_active ON smc_signals (symbol) WHERE state IN ('PENDING', 'OPEN');

    CREATE TABLE smc_signal_events (
        id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        signal_id   bigint      NOT NULL REFERENCES smc_signals(id),
        ts          timestamptz NOT NULL,
        kind        text        NOT NULL,
        price       numeric(38,18),
        detail      jsonb,
        recorded_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX smc_signal_events_by_signal ON smc_signal_events (signal_id, id);
    CREATE TRIGGER smc_signal_events_append_only BEFORE UPDATE OR DELETE
        ON smc_signal_events FOR EACH ROW EXECUTE FUNCTION sp2l_reject_mutation();

    CREATE TABLE smc_runner_state (
        symbol       text PRIMARY KEY,
        started_at   timestamptz NOT NULL,
        heartbeat_at timestamptz NOT NULL,
        last_m1      timestamptz,
        status       jsonb       NOT NULL DEFAULT '{}'
    );
    """)


def downgrade() -> None:
    op.execute(
        "DROP TABLE smc_runner_state; DROP TABLE smc_signal_events; DROP TABLE smc_signals;"
        " DROP TABLE exchange_m1;"
    )
