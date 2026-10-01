"""0015 live indicator display: one exact snapshot per finalized M5 bar per Shadow session.

Written by the Shadow engine right after the bar is added to its M5 state (the values the
gates read). Append-only; display only.

Revision ID: 0015
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE indicator_snapshots (
        id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        session_id  uuid        NOT NULL,
        symbol      text        NOT NULL,
        open_time   timestamptz NOT NULL,
        snapshot    jsonb       NOT NULL,
        recorded_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (session_id, open_time)
    );
    CREATE INDEX indicator_snapshots_recent ON indicator_snapshots (symbol, open_time DESC);
    CREATE TRIGGER indicator_snapshots_append_only BEFORE UPDATE OR DELETE
        ON indicator_snapshots FOR EACH ROW EXECUTE FUNCTION sp2l_reject_mutation();
    """)


def downgrade() -> None:
    op.execute("DROP TABLE indicator_snapshots;")
