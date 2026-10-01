from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

URL = os.environ.get("SP2L_TEST_DATABASE_URL", "postgresql+psycopg:///sp2l_test?host=/tmp")


@pytest.fixture(scope="module")
def engine():
    cfg = Config("alembic.ini")
    cfg.attributes["url"] = URL
    eng = create_engine(URL)
    try:
        eng.connect().close()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"test database unavailable: {exc}")
    with eng.begin() as c:  # fresh schema per module (downgrades with data are lossy)
        c.execute(
            text(
                "DROP SCHEMA IF EXISTS cf CASCADE; DROP SCHEMA public CASCADE;"
                " CREATE SCHEMA public;"
            )
        )
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture()
def conn(engine):
    with engine.connect() as c:
        tx = c.begin()
        yield c
        tx.rollback()


@pytest.fixture()
def alembic_cfg():
    cfg = Config("alembic.ini")
    cfg.attributes["url"] = URL
    return cfg
