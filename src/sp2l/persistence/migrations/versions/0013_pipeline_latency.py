"""0013 pipeline latency samples (V5.7 finalization latency budget).

One row per (minute, stage): ws_proof, rest_cover, rest_request, match, finalize, persist
(collector) and strategy_eval, e1_decision (Shadow runner). Milliseconds after the minute
closed, except rest_request/match/persist/strategy_eval/e1_decision which are durations or
offsets from finalization as documented in their writers.

Revision ID: 0013
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE pipeline_latency (
        id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        symbol      text        NOT NULL,
        minute      timestamptz NOT NULL,
        stage       text        NOT NULL,
        ms          double precision NOT NULL,
        recorded_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX pipeline_latency_stage_ts ON pipeline_latency (stage, recorded_at);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE pipeline_latency")
