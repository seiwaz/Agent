"""Database URL resolution. Postgres only (DATA-01)."""

from __future__ import annotations

import os

DEFAULT_URL = "postgresql+psycopg:///sp2l_dev?host=/tmp"


def database_url() -> str:
    return os.environ.get("SP2L_DATABASE_URL", DEFAULT_URL)
