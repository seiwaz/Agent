"""0017 analysis-only probes (RoomToTP breakout-consumed-obstacle evidence, 2026-09-29).

Written by the Shadow engine's isolated RoomProbe; never read by any trading decision.

Revision ID: 0017
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE analysis_probes (
        id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        session_id    uuid        NOT NULL,
        symbol        text        NOT NULL,
        kind          text        NOT NULL,
        version       text        NOT NULL,
        candidate_key text        NOT NULL,
        created_at    timestamptz NOT NULL,
        result        text,
        record        jsonb       NOT NULL,
        recorded_at   timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX analysis_probes_recent ON analysis_probes (symbol, kind, created_at DESC);
    CREATE TRIGGER analysis_probes_append_only BEFORE UPDATE OR DELETE
        ON analysis_probes FOR EACH ROW EXECUTE FUNCTION sp2l_reject_mutation();
    """)


def downgrade() -> None:
    op.execute("DROP TABLE analysis_probes;")
