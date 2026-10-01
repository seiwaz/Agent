"""0008 collector service health, continuous setup checkpoints, read-only validation items.

- collector_runs / collector_heartbeats: every service (re)start and periodic health.
- setup_checkpoints: latest serialized state of every in-progress setup (mutable; the
  append-only history lives in the strategy tables and strategy_events).
- engine_checkpoints: per Shadow session engine + simulated-broker state for restart.
- runtime_validation_items: read-only account checks. Adding items can only make the Live
  gate stricter; LIVE_AUTOMATION_DISABLED remains until every item passes.

Revision ID: 0008
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

NEW_ITEMS = ("BALANCE_READ", "OPEN_POSITIONS_READ", "POSITION_RISK_READ", "OPEN_ORDERS_READ")


def upgrade() -> None:
    op.execute("""
    CREATE TABLE collector_runs (
        id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        started_at    timestamptz NOT NULL,
        ended_at      timestamptz,
        pid           integer     NOT NULL,
        host          text        NOT NULL,
        symbol        text        NOT NULL,
        mode          text        NOT NULL CHECK (mode IN ('COLLECT','COLLECT_SHADOW')),
        spec_version  text        NOT NULL,
        spec_sha256   text        NOT NULL,
        exit_reason   text
    );
    CREATE TABLE collector_heartbeats (
        id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id              bigint      NOT NULL REFERENCES collector_runs(id),
        ts                  timestamptz NOT NULL,
        connected           boolean     NOT NULL,
        healthy_until       timestamptz,
        coverage_lag_ms     integer,
        last_trade_exch_ts  timestamptz,
        trades_total        bigint      NOT NULL,
        late_total          bigint      NOT NULL,
        m1_ok               bigint      NOT NULL,
        m1_synthetic        bigint      NOT NULL,
        m1_data_gap         bigint      NOT NULL,
        m1_unanchored       bigint      NOT NULL,
        gaps_total          bigint      NOT NULL,
        reconnects          bigint      NOT NULL
    );
    CREATE INDEX collector_heartbeats_run_ts ON collector_heartbeats (run_id, ts DESC);
    CREATE TRIGGER collector_heartbeats_append_only BEFORE UPDATE OR DELETE
        ON collector_heartbeats FOR EACH ROW EXECUTE FUNCTION sp2l_reject_mutation();

    CREATE TABLE setup_checkpoints (
        candidate_id  uuid PRIMARY KEY REFERENCES candidates(id),
        session_id    uuid        NOT NULL REFERENCES shadow_sessions(id),
        setup_key     text        NOT NULL,
        state         text        NOT NULL,
        terminal      boolean     NOT NULL,
        seq           integer     NOT NULL,
        checkpoint    jsonb       NOT NULL,
        updated_at    timestamptz NOT NULL
    );
    CREATE INDEX setup_checkpoints_open ON setup_checkpoints (session_id) WHERE NOT terminal;
    CREATE TABLE engine_checkpoints (
        session_id  uuid PRIMARY KEY REFERENCES shadow_sessions(id),
        seq         bigint      NOT NULL,
        engine      jsonb       NOT NULL,
        broker      jsonb       NOT NULL,
        updated_at  timestamptz NOT NULL
    );
    """)
    items = ",".join(f"('{i}')" for i in NEW_ITEMS)
    op.execute(f"INSERT INTO runtime_validation_items (item) VALUES {items}")


def downgrade() -> None:
    items = ",".join(f"'{i}'" for i in NEW_ITEMS)
    op.execute(f"""
    DELETE FROM runtime_validation_items WHERE item IN ({items})
        AND NOT EXISTS (SELECT 1 FROM runtime_validation_runs r WHERE r.item = runtime_validation_items.item);
    DROP TABLE engine_checkpoints, setup_checkpoints, collector_heartbeats, collector_runs;
    """)
