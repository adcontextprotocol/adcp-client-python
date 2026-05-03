"""Alembic environment for the v3 reference seller.

Uses SQLAlchemy's async engine (asyncpg) via the standard Alembic
async pattern.  Run from the examples/v3_reference_seller/ directory:

    DATABASE_URL=postgresql+asyncpg://... alembic upgrade head

For autogenerate to capture every table, both src.models and src.audit
must be imported before target_metadata is read.  Missing either import
silently omits that module's tables from the generated migration.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# ---------------------------------------------------------------------------
# Path wiring — make ``src.*`` importable when env.py is executed from the
# examples/v3_reference_seller/ directory by the ``alembic`` CLI.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent.parent  # examples/v3_reference_seller/
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Import all ORM modules so their tables appear in Base.metadata.
# Adding a new model file?  Import it here or autogenerate will miss it.
import src.audit  # noqa: E402, F401 — registers AuditEventRow on Base.metadata
from src.models import Base  # noqa: E402

target_metadata = Base.metadata

# ---------------------------------------------------------------------------
# Alembic config
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DATABASE_URL comes from the environment; never hardcode it here.
try:
    _db_url: str = os.environ["DATABASE_URL"]
except KeyError:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set. "
        "Example: DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp alembic upgrade head"
    ) from None
config.set_main_option("sqlalchemy.url", _db_url)


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    """Emit SQL to stdout rather than connecting to the DB.

    Useful for generating a migration script to review or apply manually.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Create an async engine and run migrations inside a sync wrapper.

    Alembic's migration functions are synchronous; ``run_sync`` bridges
    the gap so we can use an asyncpg engine end-to-end.
    """
    connectable = create_async_engine(_db_url, echo=False)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
