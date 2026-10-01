"""0006 runtime validation evidence and the derived Live gate (LIVE-01, RV checklist).

live_automation_status reports LIVE_AUTOMATION_DISABLED unless the latest run of every
mandatory checklist item passed.

Revision ID: 0006
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

CHECKLIST_ITEMS = (
    "SYMBOL_FILTERS",
    "SERVER_TIME_DRIFT",
    "WS_TRADE_COMPLETENESS",
    "CROSS_10X",
    "CANCEL_RACE_ZERO_FILL",
    "CANCEL_RACE_FILL",
    "NO_DUPLICATE_E1",
    "E1_E2_AGGREGATION_REDUCE_ONLY",
    "NATIVE_SL_SINGLE_TP",
    "CONTRACT_PRICE_SEMANTICS",
    "PARTIAL_FILLS",
    "LIQUIDATION_AFTER_E2",
    "RESTART_RECOVERY",
    "DISABLE_LIVE",
)


def upgrade() -> None:
    items = ",".join(f"('{i}')" for i in CHECKLIST_ITEMS)
    op.execute(f"""
    CREATE TABLE runtime_validation_items (item text PRIMARY KEY);
    INSERT INTO runtime_validation_items (item) VALUES {items};

    CREATE TABLE runtime_validation_runs (
        id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        item          text        NOT NULL REFERENCES runtime_validation_items(item),
        started_at    timestamptz NOT NULL,
        finished_at   timestamptz NOT NULL,
        passed        boolean     NOT NULL,
        api_version   text        NOT NULL,
        evidence      jsonb       NOT NULL,
        notes         text,
        approved_by   text
    );
    CREATE TRIGGER runtime_validation_runs_append_only BEFORE UPDATE OR DELETE
        ON runtime_validation_runs FOR EACH ROW EXECUTE FUNCTION sp2l_reject_mutation();

    CREATE VIEW live_automation_status AS
    WITH latest AS (
        SELECT DISTINCT ON (item) item, passed
        FROM runtime_validation_runs ORDER BY item, finished_at DESC, id DESC
    )
    SELECT CASE WHEN count(*) FILTER (WHERE l.passed IS TRUE) = count(*)
                THEN 'LIVE_AUTOMATION_ELIGIBLE' ELSE 'LIVE_AUTOMATION_DISABLED' END AS status,
           array_agg(i.item ORDER BY i.item) FILTER (WHERE l.passed IS NOT TRUE) AS failing_items
    FROM runtime_validation_items i LEFT JOIN latest l USING (item);
    """)


def downgrade() -> None:
    op.execute(
        "DROP VIEW live_automation_status; "
        "DROP TABLE runtime_validation_runs, runtime_validation_items"
    )
