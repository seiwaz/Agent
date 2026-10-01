from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from sp2l.persistence.db import database_url


def run_migrations_online() -> None:
    url = context.config.attributes.get("url") or database_url()
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("offline mode is not supported; run against a database")
run_migrations_online()
