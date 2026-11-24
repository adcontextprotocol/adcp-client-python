"""Account offering pins survive configuration persistence and recovery."""

from dataclasses import replace

import pytest
from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

from adcp.reporting.ledger.store import LedgerConflictError

from ._generation_support import configuration, isolated_reporting_pool


async def test_postgres_retains_account_binding_and_rejects_a_changed_generation() -> None:
    accepted = replace(configuration(), account_binding_sha256="c" * 64)
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()
        await store.put_configuration(accepted)

        assert await store.list_all_configurations() == (accepted,)
        with pytest.raises(LedgerConflictError, match="different content"):
            await store.put_configuration(replace(accepted, account_binding_sha256="d" * 64))
