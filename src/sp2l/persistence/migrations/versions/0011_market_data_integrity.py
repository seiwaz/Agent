"""0011 V5.6 market-data integrity: candle lineage, auditable revisions, gap repairs.

- candles_1m / candles_5m: `quality` (LIVE_PROVEN_RAW | SYNTHETIC_NO_TRADE | REPAIRED_TABDEAL
  | CONFLICTED), `revision` and `reconciled_at`. A canonical candle may change ONLY through a
  recorded revision (the previous version is kept in candle_revisions; a trigger enforces it)
  and is never deleted.
- candle_revisions: append-only history of every replaced canonical candle (old + new
  values, qualities, source, reason).
- gap_repairs: every merged coverage gap and its outcome (REPAIRED / UNRECOVERED), method and
  evidence. data_gaps.resolution mirrors it.
- raw_trades.source: WS | REPAIR_REST | RECOVERED_CONFLICT_LOG (the raw payload column
  already exists and is now filled).
- market_events.kind gains GAP_RESOLVED (informational; the runner derives nothing from it).

Revision ID: 0011
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

QUALITIES = "'LIVE_PROVEN_RAW','SYNTHETIC_NO_TRADE','REPAIRED_TABDEAL','CONFLICTED'"


def upgrade() -> None:
    for tf, synth in (("1m", "synthetic_no_trade"), ("5m", "synthetic_m1_count = 5")):
        op.execute(f"""
        ALTER TABLE candles_{tf}
            ADD COLUMN quality text NOT NULL DEFAULT 'LIVE_PROVEN_RAW'
                CHECK (quality IN ({QUALITIES})),
            ADD COLUMN revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
            ADD COLUMN reconciled_at timestamptz;
        UPDATE candles_{tf} SET quality = 'SYNTHETIC_NO_TRADE' WHERE {synth};
        """)
    op.execute("""
    ALTER TABLE candles_5m ADD COLUMN repaired_m1_count integer NOT NULL DEFAULT 0
        CHECK (repaired_m1_count BETWEEN 0 AND 5);

    CREATE TABLE candle_revisions (
        id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        symbol        text        NOT NULL,
        timeframe     text        NOT NULL CHECK (timeframe IN ('1m','5m')),
        open_time     timestamptz NOT NULL,
        revision      integer     NOT NULL CHECK (revision >= 1),
        old           jsonb       NOT NULL,
        new           jsonb       NOT NULL,
        old_quality   text        NOT NULL,
        new_quality   text        NOT NULL,
        source        text        NOT NULL,
        reason        text        NOT NULL,
        reconciled_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (symbol, timeframe, open_time, revision)
    );
    CREATE TRIGGER candle_revisions_append_only BEFORE UPDATE OR DELETE
        ON candle_revisions FOR EACH ROW EXECUTE FUNCTION sp2l_reject_mutation();

    CREATE FUNCTION sp2l_candle_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'canonical candles are never deleted (V5.6)';
        END IF;
        IF NEW.revision <> OLD.revision + 1 OR NOT EXISTS (
            SELECT 1 FROM candle_revisions r WHERE r.symbol = NEW.symbol
              AND r.timeframe = TG_ARGV[0] AND r.open_time = NEW.open_time
              AND r.revision = NEW.revision) THEN
            RAISE EXCEPTION 'candle % % changed without a recorded revision (V5.6)',
                TG_ARGV[0], NEW.open_time;
        END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER candles_1m_guard BEFORE UPDATE OR DELETE ON candles_1m
        FOR EACH ROW EXECUTE FUNCTION sp2l_candle_guard('1m');
    CREATE TRIGGER candles_5m_guard BEFORE UPDATE OR DELETE ON candles_5m
        FOR EACH ROW EXECUTE FUNCTION sp2l_candle_guard('5m');

    CREATE TABLE gap_repairs (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id       bigint      REFERENCES collector_runs(id),
        symbol       text        NOT NULL,
        gap_start    timestamptz NOT NULL,
        gap_end      timestamptz NOT NULL,
        reason       text        NOT NULL,
        status       text        NOT NULL CHECK (status IN ('REPAIRED','UNRECOVERED')),
        method       text        NOT NULL,
        failure      text,
        trades_recovered integer NOT NULL DEFAULT 0,
        detail       jsonb       NOT NULL DEFAULT '{}',
        resolved_at  timestamptz NOT NULL DEFAULT now(),
        CHECK (gap_end > gap_start)
    );
    CREATE INDEX gap_repairs_ts ON gap_repairs (symbol, gap_start);
    CREATE TRIGGER gap_repairs_append_only BEFORE UPDATE OR DELETE
        ON gap_repairs FOR EACH ROW EXECUTE FUNCTION sp2l_reject_mutation();

    ALTER TABLE data_gaps ADD COLUMN resolution text
        CHECK (resolution IN ('REPAIRED','UNRECOVERED'));
    ALTER TABLE raw_trades ADD COLUMN source text NOT NULL DEFAULT 'WS'
        CHECK (source IN ('WS','REPAIR_REST','RECOVERED_CONFLICT_LOG'));

    ALTER TABLE market_events DROP CONSTRAINT market_events_kind_check;
    ALTER TABLE market_events ADD CONSTRAINT market_events_kind_check
        CHECK (kind IN ('TRADE','M1','GAP','GAP_RESOLVED'));
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE market_events DROP CONSTRAINT market_events_kind_check;
    ALTER TABLE market_events ADD CONSTRAINT market_events_kind_check
        CHECK (kind IN ('TRADE','M1','GAP')) NOT VALID;
    ALTER TABLE raw_trades DROP COLUMN source;
    ALTER TABLE data_gaps DROP COLUMN resolution;
    DROP TABLE gap_repairs;
    DROP TRIGGER candles_1m_guard ON candles_1m;
    DROP TRIGGER candles_5m_guard ON candles_5m;
    DROP FUNCTION sp2l_candle_guard();
    DROP TABLE candle_revisions;
    ALTER TABLE candles_5m DROP COLUMN repaired_m1_count;
    """)
    for tf in ("1m", "5m"):
        op.execute(
            f"ALTER TABLE candles_{tf} DROP COLUMN quality, DROP COLUMN revision,"
            " DROP COLUMN reconciled_at"
        )
