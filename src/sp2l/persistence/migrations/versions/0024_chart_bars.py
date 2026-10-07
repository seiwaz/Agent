"""0024 chart bars from Tabdeal's chart feed (dashboard chart cache).

chart_bars: closed bars of the dashboard chart per market and timeframe, as Tabdeal's own chart
serves them (native resolution, Tehran-aligned 1h / 4h / 1d). A cache only: the chart re-reads
any range it lacks from Tabdeal, and nothing in the engine reads this table.

Revision ID: 0024
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE chart_bars (
        symbol     text           NOT NULL,
        tf         text           NOT NULL,
        open_time  timestamptz    NOT NULL,
        open       numeric(38,18) NOT NULL,
        high       numeric(38,18) NOT NULL,
        low        numeric(38,18) NOT NULL,
        close      numeric(38,18) NOT NULL,
        volume     numeric(38,18) NOT NULL,
        fetched_at timestamptz    NOT NULL DEFAULT now(),
        PRIMARY KEY (symbol, tf, open_time)
    );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE chart_bars;")
