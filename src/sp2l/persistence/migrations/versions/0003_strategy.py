"""0003 strategy records: P-Gaps, spikes, candidates, gate snapshots, E1 revisions, events.

DATA-02: every candidate stores raw metric values, gate booleans, reason codes, spec
version/sha, evaluation timestamp and the M1/M5 candle ids used.
DATA-03: strategy records are append-only; UPDATE and DELETE are rejected by trigger.
Candidate status lives in candidate_transitions (append-only); candidate_current is a view.
Unconstrained numeric keeps metric values exact. +INF metrics use a *_infinite flag.

Revision ID: 0003
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APPEND_ONLY = (
    "spec_versions",
    "pgaps",
    "spikes",
    "candidates",
    "candidate_transitions",
    "context_snapshots",
    "exhaustion_snapshots",
    "e1_revisions",
    "strategy_events",
)

CANDIDATE_STATES = (
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


def upgrade() -> None:
    states = ",".join(f"'{s}'" for s in CANDIDATE_STATES)
    op.execute(f"""
    CREATE FUNCTION sp2l_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'SP2L: % on % is forbidden (append-only, DATA-03)', TG_OP, TG_TABLE_NAME;
    END $$;

    CREATE TABLE spec_versions (
        sha256    text PRIMARY KEY,
        version   text NOT NULL,
        loaded_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE pgaps (
        id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        symbol            text        NOT NULL,
        side              text        NOT NULL CHECK (side IN ('LONG','SHORT')),
        left_candle_id    bigint      NOT NULL REFERENCES candles_1m(id),
        middle_candle_id  bigint      NOT NULL REFERENCES candles_1m(id),
        right_candle_id   bigint      NOT NULL REFERENCES candles_1m(id),
        confirmed_at      timestamptz NOT NULL,
        promoted          boolean     NOT NULL,
        not_promoted_reason text
    );

    CREATE TABLE spikes (
        id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        symbol             text           NOT NULL,
        side               text           NOT NULL CHECK (side IN ('LONG','SHORT')),
        pgap_id            bigint         NOT NULL REFERENCES pgaps(id),
        origin_candle_id   bigint         NOT NULL REFERENCES candles_1m(id),
        origin_low         numeric(38,18) NOT NULL,
        origin_high        numeric(38,18) NOT NULL,
        confirmed_at       timestamptz    NOT NULL
    );

    CREATE TABLE candidates (
        id              uuid PRIMARY KEY,
        symbol          text        NOT NULL,
        side            text        NOT NULL CHECK (side IN ('LONG','SHORT')),
        mode            text        NOT NULL CHECK (mode IN ('BACKTEST','REPLAY','SHADOW','LIVE')),
        spike_id        bigint      NOT NULL REFERENCES spikes(id),
        spec_version    text        NOT NULL,
        spec_sha256     text        NOT NULL REFERENCES spec_versions(sha256),
        created_at      timestamptz NOT NULL
    );

    CREATE TABLE candidate_transitions (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        candidate_id    uuid        NOT NULL REFERENCES candidates(id),
        seq             integer     NOT NULL,
        state_from      text        CHECK (state_from IN ({states})),
        state_to        text        NOT NULL CHECK (state_to IN ({states})),
        primary_reason  text,
        reasons         text[]      NOT NULL DEFAULT '{{}}',
        ts              timestamptz NOT NULL,
        UNIQUE (candidate_id, seq)
    );
    CREATE VIEW candidate_current AS
        SELECT DISTINCT ON (c.id) c.*, t.state_to AS status, t.primary_reason, t.reasons,
               t.ts AS status_ts
        FROM candidates c JOIN candidate_transitions t ON t.candidate_id = c.id
        ORDER BY c.id, t.seq DESC;

    CREATE TABLE context_snapshots (
        id                        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        candidate_id              uuid        NOT NULL REFERENCES candidates(id),
        evaluation_seq            integer     NOT NULL,
        eval_ts                   timestamptz NOT NULL,
        m1_candle_id              bigint      NOT NULL REFERENCES candles_1m(id),
        m5_candle_id              bigint      REFERENCES candles_5m(id),
        spec_sha256               text        NOT NULL REFERENCES spec_versions(sha256),
        status                    text        NOT NULL CHECK (status IN ('PASS','REJECT','UNKNOWN')),
        regime                    text,
        trend                     text,
        chop14                    numeric,
        adx14                     numeric,
        range_high                numeric,
        range_low                 numeric,
        range_position_e1         numeric,
        range_position_origin     numeric,
        range_middle_reject       boolean,
        latest_swing_high         numeric,
        latest_swing_low          numeric,
        breakout_level            numeric,
        breakout_context          boolean,
        htf_alignment             boolean,
        range_edge_origin         boolean,
        htf_opposite_no_breakout  boolean,
        nearest_obstacle          numeric,
        room_to_tp_r              numeric,
        room_to_tp_infinite       boolean,
        room_pass                 boolean,
        volume_ratio              numeric,
        tradecount_ratio          numeric,
        liquidity_status          text CHECK (liquidity_status IN ('PASS','LOW_LIQUIDITY','LIQUIDITY_UNKNOWN')),
        primary_reason            text,
        reasons                   text[]      NOT NULL DEFAULT '{{}}',
        thresholds                jsonb       NOT NULL,
        UNIQUE (candidate_id, evaluation_seq)
    );

    CREATE TABLE exhaustion_snapshots (
        id                              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        candidate_id                    uuid        NOT NULL REFERENCES candidates(id),
        evaluation_seq                  integer     NOT NULL,
        eval_ts                         timestamptz NOT NULL,
        m1_candle_id                    bigint      NOT NULL REFERENCES candles_1m(id),
        m5_candle_id                    bigint      REFERENCES candles_5m(id),
        spec_sha256                     text        NOT NULL REFERENCES spec_versions(sha256),
        status                          text        NOT NULL CHECK (status IN ('PASS','REJECT','UNKNOWN')),
        trend_age_bars                  integer,
        microchannel_len                integer,
        ema20_m5                        numeric,
        atr14_m5                        numeric,
        spike_extreme                   numeric,
        stretch_atr                     numeric,
        spike_atr                       numeric,
        range_position_20               numeric,
        opposing_swing_distance_atr     numeric,
        opposing_swing_infinite         boolean,
        late_trend                      boolean,
        extreme_stretch                 boolean,
        climactic_spike                 boolean,
        at_outer_edge                   boolean,
        fresh_breakout_exception        boolean,
        previous_regime_at_breakout     text,
        breakout_start_m5_open_time     timestamptz,
        sub_reasons                     text[]      NOT NULL DEFAULT '{{}}',
        thresholds                      jsonb       NOT NULL,
        UNIQUE (candidate_id, evaluation_seq)
    );

    CREATE TABLE e1_revisions (
        id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        candidate_id            uuid           NOT NULL REFERENCES candidates(id),
        revision                integer        NOT NULL CHECK (revision >= 0),
        last_spike_candle_id    bigint         NOT NULL REFERENCES candles_1m(id),
        e1                      numeric(38,18) NOT NULL,
        sl                      numeric(38,18) NOT NULL,
        r                       numeric(38,18) NOT NULL CHECK (r > 0),
        tp                      numeric(38,18) NOT NULL,
        e2_reference            numeric(38,18) NOT NULL,
        qty                     numeric(38,18) NOT NULL CHECK (qty > 0),
        wallet_basis            numeric(38,18) NOT NULL,
        modeled_worst_loss      numeric(38,18) NOT NULL,
        modeled_costs           numeric(38,18) NOT NULL,
        context_snapshot_id     bigint         NOT NULL REFERENCES context_snapshots(id),
        exhaustion_snapshot_id  bigint         NOT NULL REFERENCES exhaustion_snapshots(id),
        created_at              timestamptz    NOT NULL,
        UNIQUE (candidate_id, revision),
        CHECK (modeled_worst_loss <= wallet_basis * 0.01)
    );

    CREATE TABLE strategy_events (
        id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        symbol        text        NOT NULL,
        candidate_id  uuid        REFERENCES candidates(id),
        ts            timestamptz NOT NULL,
        event_type    text        NOT NULL,
        payload       jsonb       NOT NULL DEFAULT '{{}}'
    );
    CREATE INDEX strategy_events_candidate ON strategy_events (candidate_id, id);
    """)
    for table in APPEND_ONLY:
        op.execute(f"""
        CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION sp2l_reject_mutation();
        CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION sp2l_reject_mutation();
        """)


def downgrade() -> None:
    op.execute("DROP VIEW candidate_current")
    op.execute("DROP TABLE " + ", ".join(reversed(APPEND_ONLY)) + " CASCADE")
    op.execute("DROP FUNCTION sp2l_reject_mutation()")
