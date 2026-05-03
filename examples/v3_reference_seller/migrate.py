"""Migration helper for the v3 reference seller.

Runs ``alembic upgrade head`` programmatically using the DATABASE_URL
environment variable.  Intended as a standalone script for production
deployments and CI — not the default app boot path.

Usage::

    DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp python -m migrate

The app itself still uses ``Base.metadata.create_all`` for fast local
iteration (see src/app.py).  Switch to this script when you have
production data you need to preserve across schema changes.

Requires ``alembic`` to be installed::

    pip install alembic
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print(
            "ERROR: DATABASE_URL is not set.\n"
            "Example: DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp python -m migrate",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        from alembic import command
        from alembic.config import Config
    except ImportError:
        print(
            "ERROR: alembic is not installed. Run: pip install alembic",
            file=sys.stderr,
        )
        sys.exit(1)

    ini_path = Path(__file__).parent / "alembic.ini"
    alembic_cfg = Config(str(ini_path))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    print(f"Running alembic upgrade head against {db_url!r}...")
    command.upgrade(alembic_cfg, "head")
    print("Migrations complete.")


if __name__ == "__main__":
    main()
