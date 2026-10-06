"""Migration round trip on an empty database: head -> base -> head."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import text

pytestmark = pytest.mark.db


def test_downgrade_to_base_and_upgrade_again(engine, alembic_cfg):
    with engine.begin() as c:
        c.execute(
            text(
                "DROP SCHEMA IF EXISTS cf CASCADE; DROP SCHEMA public CASCADE;"
                " CREATE SCHEMA public;"
            )
        )
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    with engine.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0022"
