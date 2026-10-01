"""0005 shadow sessions/ledger and the isolated counterfactual schema (SHD-01, SHD-06).

Counterfactual outcomes live in schema `cf`, reference candidates read-only, and are
always labelled COUNTERFACTUAL. If a role named sp2l_runtime exists it is denied write
access to `cf`; this migration never creates cluster-wide roles itself.

Revision ID: 0005
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE shadow_sessions (
        id                  uuid PRIMARY KEY,
        symbol              text           NOT NULL,
        spec_sha256         text           NOT NULL REFERENCES spec_versions(sha256),
        initial_wallet_usdt numeric(38,18) NOT NULL DEFAULT 100 CHECK (initial_wallet_usdt = 100),
        started_at          timestamptz    NOT NULL,
        ended_at            timestamptz
    );
    CREATE TABLE shadow_wallet_ledger (
        id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        session_id     uuid           NOT NULL REFERENCES shadow_sessions(id),
        candidate_id   uuid           REFERENCES candidates(id),
        ts             timestamptz    NOT NULL,
        kind           text           NOT NULL CHECK (kind IN ('INITIAL','REALIZED_PNL','FEE','FUNDING','ADJUSTMENT')),
        amount         numeric(38,18) NOT NULL,
        balance_after  numeric(38,18) NOT NULL
    );
    CREATE TRIGGER shadow_wallet_ledger_append_only BEFORE UPDATE OR DELETE ON shadow_wallet_ledger
        FOR EACH ROW EXECUTE FUNCTION sp2l_reject_mutation();

    CREATE SCHEMA cf;
    CREATE TABLE cf.counterfactual_outcomes (
        id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        candidate_id      uuid        NOT NULL REFERENCES public.candidates(id),
        label             text        NOT NULL DEFAULT 'COUNTERFACTUAL' CHECK (label = 'COUNTERFACTUAL'),
        rejection_stage   text        NOT NULL CHECK (rejection_stage IN
            ('CONTEXT','EXHAUSTION','CONTEXT_AFTER_ARM','EXHAUSTION_AFTER_ARM')),
        simulator_version text        NOT NULL,
        spec_sha256       text        NOT NULL REFERENCES public.spec_versions(sha256),
        outcome           text        NOT NULL CHECK (outcome IN
            ('TP','SL','EXPIRED_NO_FILL','AMBIGUOUS','OPEN','NO_SIMULATION')),
        e1_filled_at      timestamptz,
        e2_filled         boolean,
        exit_at           timestamptz,
        result_r          numeric,
        mfe_r             numeric,
        mae_r             numeric,
        detail            jsonb       NOT NULL DEFAULT '{}',
        created_at        timestamptz NOT NULL DEFAULT now()
    );
    CREATE TRIGGER counterfactual_outcomes_append_only BEFORE UPDATE OR DELETE
        ON cf.counterfactual_outcomes FOR EACH ROW EXECUTE FUNCTION public.sp2l_reject_mutation();

    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sp2l_runtime') THEN
            REVOKE ALL ON SCHEMA cf FROM sp2l_runtime;
            REVOKE ALL ON ALL TABLES IN SCHEMA cf FROM sp2l_runtime;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("DROP SCHEMA cf CASCADE")
    op.execute("DROP TABLE shadow_wallet_ledger, shadow_sessions")
