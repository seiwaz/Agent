"""0007 persistence details found while wiring the recorder.

- raw_trades.late: a trade that arrived after its minute finalized (logged, never applied).
- snapshots: m1_candle_id may be NULL (evaluation at a DATA_GAP / synthetic minute boundary),
  and `exact` keeps every raw metric as an exact string (Fractions such as 1/3), since the
  numeric columns are finite-precision renderings.
- cf outcomes: EXPIRED_UNARMED and ERROR are possible counterfactual outcomes.
- candidates.setup_key: the engine's human-readable setup id (e.g. BTCUSDT-7).

Revision ID: 0007
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE raw_trades ADD COLUMN late boolean NOT NULL DEFAULT false;
    ALTER TABLE context_snapshots ALTER COLUMN m1_candle_id DROP NOT NULL;
    ALTER TABLE exhaustion_snapshots ALTER COLUMN m1_candle_id DROP NOT NULL;
    ALTER TABLE context_snapshots ADD COLUMN exact jsonb NOT NULL DEFAULT '{}';
    ALTER TABLE exhaustion_snapshots ADD COLUMN exact jsonb NOT NULL DEFAULT '{}';
    ALTER TABLE candidates ADD COLUMN setup_key text;
    CREATE UNIQUE INDEX candidates_setup_key ON candidates (setup_key) WHERE setup_key IS NOT NULL;
    DROP VIEW candidate_current;
    CREATE VIEW candidate_current AS
        SELECT DISTINCT ON (c.id) c.*, t.state_to AS status, t.primary_reason, t.reasons,
               t.ts AS status_ts
        FROM candidates c JOIN candidate_transitions t ON t.candidate_id = c.id
        ORDER BY c.id, t.seq DESC;
    ALTER TABLE cf.counterfactual_outcomes DROP CONSTRAINT counterfactual_outcomes_outcome_check;
    ALTER TABLE cf.counterfactual_outcomes ADD CONSTRAINT counterfactual_outcomes_outcome_check
        CHECK (outcome IN ('TP','SL','EXPIRED_NO_FILL','EXPIRED_UNARMED','AMBIGUOUS','OPEN',
                           'NO_SIMULATION','ERROR'));
    """)


def downgrade() -> None:
    # Lossy by nature: rows written under 0007 may use the newer outcomes, so the old
    # constraint is restored NOT VALID instead of failing on existing data.
    op.execute("""
    ALTER TABLE cf.counterfactual_outcomes DROP CONSTRAINT counterfactual_outcomes_outcome_check;
    ALTER TABLE cf.counterfactual_outcomes ADD CONSTRAINT counterfactual_outcomes_outcome_check
        CHECK (outcome IN ('TP','SL','EXPIRED_NO_FILL','AMBIGUOUS','OPEN','NO_SIMULATION'))
        NOT VALID;
    DROP VIEW candidate_current;
    CREATE VIEW candidate_current AS
        SELECT DISTINCT ON (c.id) c.id, c.symbol, c.side, c.mode, c.spike_id, c.spec_version,
               c.spec_sha256, c.created_at, t.state_to AS status, t.primary_reason, t.reasons,
               t.ts AS status_ts
        FROM candidates c JOIN candidate_transitions t ON t.candidate_id = c.id
        ORDER BY c.id, t.seq DESC;
    DROP INDEX candidates_setup_key;
    ALTER TABLE candidates DROP COLUMN setup_key;
    ALTER TABLE exhaustion_snapshots DROP COLUMN exact;
    ALTER TABLE context_snapshots DROP COLUMN exact;
    ALTER TABLE raw_trades DROP COLUMN late;
    """)
