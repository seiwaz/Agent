"""0023 System B (trend following) paper journal.

trend_journal: one row per market and closed UTC day, written by the trend service right after
the day closed: what the engine decided on that close (BUY / SELL / ADD / HOLD / WAIT), the
levels it used and the paper wallet after it. The first row for a day is kept (what was
announced at the time is never rewritten).

Revision ID: 0023
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE trend_journal (
        symbol       text        NOT NULL,
        day          date        NOT NULL,
        params_hash  text        NOT NULL,
        action       text        NOT NULL CHECK (action IN ('BUY', 'SELL', 'ADD', 'HOLD', 'WAIT')),
        close        numeric(38,18) NOT NULL,
        entry_level  numeric(38,18),
        exit_level   numeric(38,18),
        atr          numeric(38,18),
        stop         numeric(38,18),
        position_qty numeric(38,18) NOT NULL DEFAULT 0,
        equity       numeric(38,18) NOT NULL,
        details      jsonb       NOT NULL DEFAULT '{}'::jsonb,
        recorded_at  timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (symbol, day)
    );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE trend_journal;")
