"""0009 market-event journal, AMBIGUOUS_DATA_GAP, engine input cursor, host-sleep records.

- market_events: append-only journal of every collector emission (TRADE, M1 incl.
  DATA_GAP/UNANCHORED) in exact emission order. It is the Shadow runner's causal input
  stream (V5.5 B39): restart = restore checkpoint + replay journal after the cursor.
- candidate states gain AMBIGUOUS_DATA_GAP (B39).
- engine_checkpoints.input_seq: the last journal seq fully applied (atomic per input).
- data_gaps.kind gains HOST_SLEEP (B45 diagnostics).

Revision ID: 0009
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

STATES = (
    "SCANNING",
    "BASE_SPIKE_CONFIRMED",
    "CONTEXT_EVALUATION",
    "EXHAUSTION_EVALUATION",
    "E1_PREPARING",
    "E1_PENDING",
    "E1_REPRICING",
    "PULLBACK_DETECTED",
    "E1_PARTIAL",
    "E1_FILLED",
    "E2_VALIDATING",
    "E2_PENDING",
    "E2_PARTIAL",
    "POSITION_ACTIVE",
    "FINALIZING",
    "CLOSED",
    "REJECTED_CONTEXT",
    "REJECTED_EXHAUSTION",
    "REJECTED_RISK",
    "EXPIRED_UNARMED",
    "EXPIRED_NO_FILL",
    "ERROR_HOLD",
)


def _states(extra: tuple[str, ...]) -> str:
    return ",".join(f"'{s}'" for s in STATES + extra)


def upgrade() -> None:
    st = _states(("AMBIGUOUS_DATA_GAP",))
    op.execute(f"""
    CREATE TABLE market_events (
        seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        symbol      text        NOT NULL,
        kind        text        NOT NULL CHECK (kind IN ('TRADE','M1','GAP')),
        ts          timestamptz NOT NULL,
        payload     jsonb       NOT NULL
    );
    CREATE INDEX market_events_symbol_seq ON market_events (symbol, seq);
    CREATE TRIGGER market_events_append_only BEFORE UPDATE OR DELETE ON market_events
        FOR EACH ROW EXECUTE FUNCTION sp2l_reject_mutation();

    ALTER TABLE engine_checkpoints ADD COLUMN input_seq bigint NOT NULL DEFAULT 0;

    ALTER TABLE candidate_transitions DROP CONSTRAINT candidate_transitions_state_from_check;
    ALTER TABLE candidate_transitions DROP CONSTRAINT candidate_transitions_state_to_check;
    ALTER TABLE candidate_transitions ADD CONSTRAINT candidate_transitions_state_from_check
        CHECK (state_from IN ({st}));
    ALTER TABLE candidate_transitions ADD CONSTRAINT candidate_transitions_state_to_check
        CHECK (state_to IN ({st}));

    ALTER TABLE data_gaps DROP CONSTRAINT data_gaps_kind_check;
    ALTER TABLE data_gaps ADD CONSTRAINT data_gaps_kind_check
        CHECK (kind IN ('DATA_GAP','LATE_TRADE','HOST_SLEEP'));
    """)


def downgrade() -> None:
    st = _states(())
    op.execute(f"""
    ALTER TABLE data_gaps DROP CONSTRAINT data_gaps_kind_check;
    ALTER TABLE data_gaps ADD CONSTRAINT data_gaps_kind_check
        CHECK (kind IN ('DATA_GAP','LATE_TRADE')) NOT VALID;
    ALTER TABLE candidate_transitions DROP CONSTRAINT candidate_transitions_state_from_check;
    ALTER TABLE candidate_transitions DROP CONSTRAINT candidate_transitions_state_to_check;
    ALTER TABLE candidate_transitions ADD CONSTRAINT candidate_transitions_state_from_check
        CHECK (state_from IN ({st})) NOT VALID;
    ALTER TABLE candidate_transitions ADD CONSTRAINT candidate_transitions_state_to_check
        CHECK (state_to IN ({st})) NOT VALID;
    ALTER TABLE engine_checkpoints DROP COLUMN input_seq;
    DROP TABLE market_events;
    """)
