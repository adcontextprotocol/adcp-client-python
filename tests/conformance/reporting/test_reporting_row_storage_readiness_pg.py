"""Readiness failures supply a usable remedy for cross-SDK foreign keys."""

from __future__ import annotations

import pytest
from adcp_reporting_storage import PgReportingLedgerStore
from adcp_reporting_storage.ledger.row_storage import ReportingRowStorageV1, ReportingRowStoreError

from ._generation_support import isolated_reporting_pool


async def test_foreign_core_key_refusal_supplies_an_executable_quoted_remedy() -> None:
    async with isolated_reporting_pool() as pool:
        from psycopg import sql

        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        async with pool.connection() as connection:
            await connection.execute("CREATE TABLE other_sdk_obligations (id TEXT PRIMARY KEY)")
            await connection.execute(
                sql.SQL(
                    "ALTER TABLE adcp_reporting_row_sets ADD CONSTRAINT {} "
                    "FOREIGN KEY (obligation_id) REFERENCES other_sdk_obligations (id)"
                ).format(sql.Identifier('other SDK "obligation" key'))
            )
            with pytest.raises(ReportingRowStoreError) as caught:
                await ReportingRowStorageV1._refuse_foreign_core_keys(connection)
            assert caught.value.code == "STATE_UNAVAILABLE"
            assert caught.value.detail is not None
            remedy = caught.value.detail.split("with ", 1)[1]
            assert 'DROP CONSTRAINT "other SDK ""obligation"" key";' in remedy
            await connection.execute(remedy)
            await ReportingRowStorageV1._refuse_foreign_core_keys(connection)
