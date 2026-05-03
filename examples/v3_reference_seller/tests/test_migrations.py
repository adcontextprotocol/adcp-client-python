"""Integration tests for Alembic migrations.

Requires a real Postgres instance.  Skipped unless DATABASE_URL is set.

    DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp_test \\
      pytest examples/v3_reference_seller/tests/test_migrations.py -m integration -v

Use a throw-away database (``adcp_test``, not ``adcp``) so the migration
run starts from a clean slate without touching the dev database.

The integration marker is declared in pyproject.toml; no extra marker
registration is needed for a fresh fork.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


@pytest.fixture(scope="module")
def db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — skipping migration integration tests")
    return url  # type: ignore[return-value]  # pytest.skip() never returns


# ---------------------------------------------------------------------------
# Async helpers — all DB access goes through asyncpg (no psycopg2 needed).
# ---------------------------------------------------------------------------

async def _wipe_schema(db_url: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


async def _get_table_names(db_url: str) -> set[str]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        tables = {row[0] for row in result}
    await engine.dispose()
    return tables


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_upgrade_head_creates_all_tables(db_url: str) -> None:
    """Running ``alembic upgrade head`` on a clean database creates all eight tables."""
    from alembic import command
    from alembic.config import Config

    ini_path = _HERE / "alembic.ini"
    alembic_cfg = Config(str(ini_path))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # Wipe schema so we always start clean.
    asyncio.run(_wipe_schema(db_url))

    # Run all migrations.
    command.upgrade(alembic_cfg, "head")

    # Spot-check: all eight tables must exist.
    table_names = asyncio.run(_get_table_names(db_url))
    expected = {
        "tenants",
        "buyer_agents",
        "accounts",
        "media_buys",
        "audit_events",
        "creatives",
        "performance_feedback",
    }
    assert expected <= table_names, f"Missing tables: {expected - table_names}"


@pytest.mark.integration
def test_downgrade_base_removes_all_tables(db_url: str) -> None:
    """Running ``alembic downgrade base`` drops every table cleanly."""
    from alembic import command
    from alembic.config import Config

    ini_path = _HERE / "alembic.ini"
    alembic_cfg = Config(str(ini_path))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # Start from head so downgrade has something to remove.
    asyncio.run(_wipe_schema(db_url))
    command.upgrade(alembic_cfg, "head")

    command.downgrade(alembic_cfg, "base")

    remaining = asyncio.run(_get_table_names(db_url))
    # Only the alembic_version bookkeeping table may remain.
    assert remaining <= {"alembic_version"}, f"Unexpected tables remain: {remaining}"
