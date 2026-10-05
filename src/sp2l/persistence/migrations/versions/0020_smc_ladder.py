"""0020 SMC-2.0 take-profit ladder.

- smc_signals: TP1 / TP2 / TP3 prices, their sources (`targets`), the exits so far (`parts`),
  the open quantity, the R realised so far and the model version. `tp` (the final target,
  TP3) may be NULL: then the rest runs on the trailing stop.
- The partial index of active signals covers the ladder states TP1 and TP2.
- smc_wallet_ledger: each REALIZED_PNL row names its exit (`part`) and carries its own fees.
Existing rows are left as they are (SMC-1.0 signals keep their single TP).

Revision ID: 0020
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE smc_signals
        ALTER COLUMN tp DROP NOT NULL,
        ADD COLUMN tp1        numeric(38,18),
        ADD COLUMN tp2        numeric(38,18),
        ADD COLUMN tp3        numeric(38,18),
        ADD COLUMN targets    jsonb,
        ADD COLUMN parts      jsonb NOT NULL DEFAULT '[]',
        ADD COLUMN qty_open   numeric(38,18),
        ADD COLUMN realized_r numeric(38,18),
        ADD COLUMN version    text;
    DROP INDEX smc_signals_active;
    CREATE INDEX smc_signals_active ON smc_signals (symbol)
        WHERE state IN ('PENDING', 'OPEN', 'TP1', 'TP2');

    ALTER TABLE smc_wallet_ledger
        ADD COLUMN part text,
        ADD COLUMN fees numeric(38,18);
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE smc_wallet_ledger DROP COLUMN part, DROP COLUMN fees;
    DROP INDEX smc_signals_active;
    CREATE INDEX smc_signals_active ON smc_signals (symbol) WHERE state IN ('PENDING', 'OPEN');
    ALTER TABLE smc_signals DROP COLUMN tp1, DROP COLUMN tp2, DROP COLUMN tp3,
        DROP COLUMN targets, DROP COLUMN parts, DROP COLUMN qty_open, DROP COLUMN realized_r,
        DROP COLUMN version;
    """)
