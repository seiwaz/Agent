"""0019 one shared simulated wallet for every SMC market.

- smc_wallets: the wallet (initial balance, the markets it trades); one is active at a time.
- smc_wallet_ledger: append-only DEPOSIT / REALIZED_PNL rows with the running balance.
- smc_signals gains its wallet, the reserved margin and the realized PnL / fees in USDT.

Revision ID: 0019
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE smc_wallets (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        initial_usdt numeric(38,18) NOT NULL CHECK (initial_usdt > 0),
        symbols      text[]      NOT NULL,
        started_at   timestamptz NOT NULL DEFAULT now(),
        ended_at     timestamptz
    );
    CREATE UNIQUE INDEX smc_wallets_one_active ON smc_wallets ((true)) WHERE ended_at IS NULL;

    CREATE TABLE smc_wallet_ledger (
        id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        wallet_id     bigint      NOT NULL REFERENCES smc_wallets(id),
        ts            timestamptz NOT NULL,
        symbol        text,
        signal_id     bigint REFERENCES smc_signals(id),
        kind          text        NOT NULL CHECK (kind IN ('DEPOSIT', 'REALIZED_PNL')),
        amount        numeric(38,18) NOT NULL,
        balance_after numeric(38,18) NOT NULL,
        recorded_at   timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX smc_wallet_ledger_by_wallet ON smc_wallet_ledger (wallet_id, id);
    CREATE TRIGGER smc_wallet_ledger_append_only BEFORE UPDATE OR DELETE
        ON smc_wallet_ledger FOR EACH ROW EXECUTE FUNCTION sp2l_reject_mutation();

    ALTER TABLE smc_signals
        ADD COLUMN wallet_id bigint REFERENCES smc_wallets(id),
        ADD COLUMN margin    numeric(38,18),
        ADD COLUMN pnl_usdt  numeric(38,18),
        ADD COLUMN fees_usdt numeric(38,18);
    """)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE smc_signals DROP COLUMN wallet_id, DROP COLUMN margin,"
        " DROP COLUMN pnl_usdt, DROP COLUMN fees_usdt;"
        " DROP TABLE smc_wallet_ledger; DROP TABLE smc_wallets;"
    )
