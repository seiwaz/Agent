"""0002 indicator anchors, per-bar recursive state and confirmed pivots (CTX-03..08, EXH-01/02).

Recursive state uses unconstrained numeric so a restart resumes bit-exactly (plan §7).

Revision ID: 0002
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE indicator_anchors (
        id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        symbol           text        NOT NULL,
        anchor_open_time timestamptz NOT NULL,
        adx_seed         text        NOT NULL CHECK (adx_seed = 'TALIB_N_MINUS_1'),
        reason           text        NOT NULL,
        created_at       timestamptz NOT NULL DEFAULT now(),
        UNIQUE (symbol, anchor_open_time)
    );
    CREATE TABLE m5_indicator_state (
        symbol       text        NOT NULL,
        anchor_id    bigint      NOT NULL REFERENCES indicator_anchors(id),
        open_time    timestamptz NOT NULL,
        tr           numeric,
        atr14        numeric,
        adx_tr_s     numeric,
        adx_pdm_s    numeric,
        adx_mdm_s    numeric,
        plus_di      numeric,
        minus_di     numeric,
        dx           numeric,
        adx14        numeric,
        adx_status   text NOT NULL CHECK (adx_status IN ('WARMUP','OK','UNKNOWN')),
        ema20        numeric,
        chop14       numeric,
        chop_status  text NOT NULL CHECK (chop_status IN ('WARMUP','OK','UNKNOWN')),
        regime       text CHECK (regime IN ('RANGE','TREND','TRANSITION','UNKNOWN')),
        trend        text CHECK (trend IN ('BULL','BEAR','NEUTRAL','NEUTRAL_INSUFFICIENT_STRUCTURE','INVALID_DUAL_PIVOT')),
        computed_at  timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (symbol, anchor_id, open_time)
    );
    CREATE TABLE confirmed_m5_pivots (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        symbol          text           NOT NULL,
        anchor_id       bigint         NOT NULL REFERENCES indicator_anchors(id),
        pivot_open_time timestamptz    NOT NULL,
        kind            text           NOT NULL CHECK (kind IN ('HIGH','LOW','DUAL')),
        high            numeric(38,18) NOT NULL,
        low             numeric(38,18) NOT NULL,
        confirmed_at    timestamptz    NOT NULL,
        broken_at_m5    timestamptz,
        UNIQUE (symbol, anchor_id, pivot_open_time)
    );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE confirmed_m5_pivots, m5_indicator_state, indicator_anchors")
