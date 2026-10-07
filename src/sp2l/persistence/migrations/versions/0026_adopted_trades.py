"""0026 positions opened directly on Tabdeal are adopted into manual_trades.

origin: CHART (placed from the dashboard) or TABDEAL (found on the exchange and followed from
then on). An adopted position may have no stop / target yet, so sl and tp may be NULL.

Revision ID: 0026
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE manual_trades
        ADD COLUMN origin text NOT NULL DEFAULT 'CHART' CHECK (origin IN ('CHART', 'TABDEAL')),
        ALTER COLUMN sl DROP NOT NULL,
        ALTER COLUMN tp DROP NOT NULL;
    """)


def downgrade() -> None:
    op.execute("""
    DELETE FROM manual_trades WHERE sl IS NULL OR tp IS NULL;
    ALTER TABLE manual_trades
        DROP COLUMN origin,
        ALTER COLUMN sl SET NOT NULL,
        ALTER COLUMN tp SET NOT NULL;
    """)
