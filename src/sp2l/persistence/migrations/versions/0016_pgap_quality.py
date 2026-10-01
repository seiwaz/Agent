"""0016 V5.9 Core P-Gap qualification: every geometric P-Gap's measured quality.

Rows written before V5.9 keep NULL here (never reinterpreted under the new rules).

Revision ID: 0016
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE pgaps
        ADD COLUMN spec_version         text,
        ADD COLUMN impulse_body         numeric(38,18),
        ADD COLUMN impulse_range        numeric(38,18),
        ADD COLUMN impulse_upper_shadow numeric(38,18),
        ADD COLUMN impulse_lower_shadow numeric(38,18),
        ADD COLUMN impulse_body_ratio   numeric,
        ADD COLUMN impulse_direction    text CHECK (impulse_direction IN ('BULLISH','BEARISH','DOJI')),
        ADD COLUMN gap_size             numeric(38,18),
        ADD COLUMN gap_body_ratio       numeric,
        ADD COLUMN gap_size_ticks       numeric,
        ADD COLUMN strong_impulse_pass  boolean,
        ADD COLUMN strong_gap_pass      boolean,
        ADD COLUMN final_pgap_pass      boolean,
        ADD COLUMN failure_reasons      text[],
        ADD COLUMN quality              jsonb;
    CREATE INDEX pgaps_confirmed ON pgaps (symbol, confirmed_at DESC);
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX pgaps_confirmed;
    ALTER TABLE pgaps DROP COLUMN spec_version, DROP COLUMN impulse_body,
        DROP COLUMN impulse_range, DROP COLUMN impulse_upper_shadow,
        DROP COLUMN impulse_lower_shadow, DROP COLUMN impulse_body_ratio,
        DROP COLUMN impulse_direction, DROP COLUMN gap_size, DROP COLUMN gap_body_ratio,
        DROP COLUMN gap_size_ticks, DROP COLUMN strong_impulse_pass, DROP COLUMN strong_gap_pass,
        DROP COLUMN final_pgap_pass, DROP COLUMN failure_reasons, DROP COLUMN quality;
    """)
