"""0010 B46 transport-redundancy measurement.

- feed_connection_events: per-connection CONNECTED / CONFIRMED / CLOSED (reason, lifetime).
- feed_conflicts: same exchange sequence seen with a different payload (never merged).
- collector_heartbeats.detail: per-connection status and lags, merged status, dedup counts.

Revision ID: 0010
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE feed_connection_events (
        id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id   bigint      REFERENCES collector_runs(id),
        symbol   text        NOT NULL,
        conn     text        NOT NULL,
        kind     text        NOT NULL CHECK (kind IN ('CONNECTED','CONFIRMED','CLOSED')),
        ts       timestamptz NOT NULL,
        reason   text,
        detail   jsonb       NOT NULL DEFAULT '{}'
    );
    CREATE INDEX feed_connection_events_ts ON feed_connection_events (symbol, ts);
    CREATE TRIGGER feed_connection_events_append_only BEFORE UPDATE OR DELETE
        ON feed_connection_events FOR EACH ROW EXECUTE FUNCTION sp2l_reject_mutation();

    CREATE TABLE feed_conflicts (
        id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id    bigint      REFERENCES collector_runs(id),
        symbol    text        NOT NULL,
        ts        timestamptz NOT NULL,
        trade_id  text        NOT NULL,
        first     jsonb       NOT NULL,
        other     jsonb       NOT NULL
    );
    CREATE TRIGGER feed_conflicts_append_only BEFORE UPDATE OR DELETE
        ON feed_conflicts FOR EACH ROW EXECUTE FUNCTION sp2l_reject_mutation();

    ALTER TABLE collector_heartbeats ADD COLUMN detail jsonb NOT NULL DEFAULT '{}';
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE collector_heartbeats DROP COLUMN detail;
    DROP TABLE feed_conflicts;
    DROP TABLE feed_connection_events;
    """)
