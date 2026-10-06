"""0022 SMC-2.3 futures funding.

- smc_signals.funding: funding paid so far per unit (price units; negative = received), so a
  restarted engine continues the R; funding_usdt: the USDT booked for it.
- smc_wallet_ledger: kind FUNDING (one row per funding time a position spans).

Revision ID: 0022
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE smc_signals
        ADD COLUMN funding      numeric(38,18) NOT NULL DEFAULT 0,
        ADD COLUMN funding_usdt numeric(38,18);
    ALTER TABLE smc_wallet_ledger DROP CONSTRAINT smc_wallet_ledger_kind_check;
    ALTER TABLE smc_wallet_ledger ADD CONSTRAINT smc_wallet_ledger_kind_check
        CHECK (kind IN ('DEPOSIT', 'REALIZED_PNL', 'FUNDING'));
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE smc_wallet_ledger DROP CONSTRAINT smc_wallet_ledger_kind_check;
    ALTER TABLE smc_wallet_ledger ADD CONSTRAINT smc_wallet_ledger_kind_check
        CHECK (kind IN ('DEPOSIT', 'REALIZED_PNL'));
    ALTER TABLE smc_signals DROP COLUMN funding, DROP COLUMN funding_usdt;
    """)
