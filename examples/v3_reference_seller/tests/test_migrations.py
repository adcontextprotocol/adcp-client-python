"""Integration tests for Alembic migrations.

Requires a real Postgres instance.  Skipped unless DATABASE_URL is set.

    DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp_test \\
      pytest examples/v3_reference_seller/tests/test_migrations.py -m integration -v

Use a throw-away database (``adcp_test``, not ``adcp``) so the migration
run starts from a clean slate without touching the dev database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


@pytest.fixture(scope="module")
def db_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — skipping migration integration tests")
    return url


@pytest.mark.integration
def test_upgrade_head_creates_all_tables(db_url: str) -> None:
    """Running ``alembic upgrade head`` on a clean database creates every table."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    # Use the sync driver for inspection (swap asyncpg → psycopg2 / pg8000).
    sync_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    ini_path = _HERE / "alembic.ini"
    alembic_cfg = Config(str(ini_path))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # Wipe schema so we always start clean.
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    # Run all migrations.
    command.upgrade(alembic_cfg, "head")

    # Spot-check: all five tables must exist.
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    expected = {"tenants", "buyer_agents", "accounts", "media_buys", "audit_events"}
    assert expected <= table_names, f"Missing tables: {expected - table_names}"

    engine.dispose()


@pytest.mark.integration
def test_downgrade_base_removes_all_tables(db_url: str) -> None:
    """Running ``alembic downgrade base`` drops every table cleanly."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    sync_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    ini_path = _HERE / "alembic.ini"
    alembic_cfg = Config(str(ini_path))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    command.downgrade(alembic_cfg, "base")

    engine = create_engine(sync_url)
    inspector = inspect(engine)
    remaining = set(inspector.get_table_names())
    # Only the alembic_version bookkeeping table may remain.
    assert remaining <= {"alembic_version"}, f"Unexpected tables remain: {remaining}"
    engine.dispose()
