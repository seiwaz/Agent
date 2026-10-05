"""0021 drop the TP1 / TP2 / TP3 columns of smc_signals (unused since the single target).

The single target lives in `tp` (and `targets`). Nothing reads or writes these columns.

Revision ID: 0021
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE smc_signals DROP COLUMN tp1, DROP COLUMN tp2, DROP COLUMN tp3;")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE smc_signals ADD COLUMN tp1 numeric(38,18), ADD COLUMN tp2 numeric(38,18),"
        " ADD COLUMN tp3 numeric(38,18);"
    )
