"""Regenerate ``required_row_storage_schema.json`` from a clean migration.

Usage: ADCP_PG_TEST_URL=postgresql://... python scripts/generate_row_storage_catalog.py

Applies ``reporting_row_storage.sql`` to a throwaway schema and records the
fingerprint of every table, column, constraint, index, trigger and guard
function it creates. The SDK checks those fingerprints at startup, so DDL drift
in an adopter's database is reported instead of discovered through a failed
write. The manifest ignores unrelated objects.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adcp.reporting.ledger.row_storage import (  # noqa: E402
    REPORTING_ROW_STORAGE_DDL_PATH,
    REPORTING_ROW_STORAGE_TABLES,
)
from adcp.reporting.outbox._schema import _catalog_objects  # noqa: E402

TARGET = ROOT / "src/adcp/reporting/ledger/required_row_storage_schema.json"


async def generate(url: str) -> dict[str, dict[str, object]]:
    from psycopg import AsyncConnection, sql

    schema = f"adcp_row_catalog_{secrets.token_hex(6)}"
    async with await AsyncConnection.connect(url, autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            await connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
            )
            await connection.execute(REPORTING_ROW_STORAGE_DDL_PATH.read_text())
            return await _catalog_objects(
                connection,
                table_names=REPORTING_ROW_STORAGE_TABLES,
                function_identity=("adcp_reporting_row_storage_immutable", ""),
                name_prefix="adcp_",
            )
        finally:
            await connection.execute("SET search_path TO public")
            await connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def main() -> None:
    url = os.environ.get("ADCP_PG_TEST_URL")
    if not url:
        raise SystemExit("set ADCP_PG_TEST_URL to a PostgreSQL 13+ database")
    objects = asyncio.run(generate(url))
    TARGET.write_text(json.dumps(objects, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(objects)} objects to {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
