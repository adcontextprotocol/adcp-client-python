"""A ledger upgraded to storage contract 2 is refused clearly by this release (#1831).

adcp-reporting-storage 0.2.0b1+ upgrades a 0.1.x ledger in place: migration
0002 adds ``offering_id`` and widens the configuration key and the obligation
period key with it. If a process of this release keeps running during a
rolling deploy, or restarts afterwards, it must name the actual problem and
remedy rather than fail with a raw driver error or point at the unrelated
caller-ownership migration.

``_widen`` applies the same key changes as 0002 on ``next``
(``reporting_ledger_account_generations.sql``).
"""

from __future__ import annotations

import pytest

from adcp.reporting.ledger import LedgerConflictError

from ._generation_support import configuration, isolated_reporting_pool, obligation_for

_WIDEN = """
ALTER TABLE reporting_configurations ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT '';
ALTER TABLE reporting_configurations DROP CONSTRAINT reporting_configurations_pkey CASCADE;
ALTER TABLE reporting_configurations ADD CONSTRAINT reporting_configurations_pkey
    PRIMARY KEY (account_id, consumer_id, delivery_config_id, delivery_config_version, offering_id);
ALTER TABLE reporting_obligations ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT '';
DROP INDEX reporting_obligations_period_key;
CREATE UNIQUE INDEX reporting_obligations_period_key
    ON reporting_obligations
       (account_id, consumer_id, delivery_config_id, delivery_config_version, offering_id,
        period_start, period_end);
ALTER TABLE reporting_consumer_statuses ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT '';
"""


async def _upgraded_store(pool):
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

    store = PgReportingLedgerStore(pool=pool)
    await store.create_schema()
    await store.put_configuration(configuration("acct_a"))
    async with pool.connection() as connection:
        await connection.execute(_WIDEN)
    return store


def _assert_remedy(message: str) -> None:
    assert "storage contract 2" in message
    assert "adcp>=10.0.0b7 with adcp-reporting-storage>=0.2.0b1" in message
    assert "restore the backup" in message
    assert "do not run migrate_legacy_reporting" in message


def test_only_a_missing_conflict_key_is_a_contract_2_refusal() -> None:
    from adcp_reporting_storage.migration import schema_ahead_error

    class _Error(Exception):
        def __init__(self, sqlstate: str) -> None:
            super().__init__(sqlstate)
            self.sqlstate = sqlstate

    assert schema_ahead_error(_Error("42P10")) is not None
    assert schema_ahead_error(_Error("23505")) is None  # unique violation
    assert schema_ahead_error(ValueError("no sqlstate")) is None


@pytest.mark.asyncio
async def test_create_schema_refuses_an_upgraded_ledger_and_changes_nothing() -> None:
    from adcp_reporting_storage import ReportingSchemaAheadError

    async with isolated_reporting_pool(autocommit=True) as pool:
        store = await _upgraded_store(pool)
        async with pool.connection() as connection:
            before = await (
                await connection.execute(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE conname = 'reporting_configurations_pkey'"
                    " AND connamespace = current_schema()::regnamespace"
                )
            ).fetchall()
        with pytest.raises(ReportingSchemaAheadError) as refused:
            await store.create_schema()
        _assert_remedy(str(refused.value))
        async with pool.connection() as connection:
            after = await (
                await connection.execute(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE conname = 'reporting_configurations_pkey'"
                    " AND connamespace = current_schema()::regnamespace"
                )
            ).fetchall()
        assert after == before and "offering_id" in before[0][0]


@pytest.mark.asyncio
async def test_the_ddl_alone_names_contract_2_not_the_ownership_migration() -> None:
    """For operators who apply the packaged SQL themselves."""
    from importlib.resources import files

    from psycopg import errors

    ddl = (
        files("adcp_reporting_storage.ledger")
        .joinpath("reporting_ledger_account_generations.sql")
        .read_text()
    )
    async with isolated_reporting_pool(autocommit=True) as pool:
        await _upgraded_store(pool)
        async with pool.connection() as connection:
            with pytest.raises(errors.RaiseException) as raised:
                await connection.execute(ddl)
    diagnostic = raised.value.diag
    assert "storage contract 2" in (diagnostic.message_primary or "")
    assert "Do not run migrate_legacy_reporting" in (diagnostic.message_hint or "")


@pytest.mark.asyncio
async def test_a_running_process_names_the_upgrade_on_every_widened_write() -> None:
    from adcp_reporting_storage import ReportingSchemaAheadError

    async with isolated_reporting_pool(autocommit=True) as pool:
        store = await _upgraded_store(pool)
        with pytest.raises(ReportingSchemaAheadError) as refused:
            await store.put_configuration(configuration("acct_b"))
        _assert_remedy(str(refused.value))
        with pytest.raises(LedgerConflictError) as conflict:
            await store.commit_obligation(obligation_for(configuration("acct_a")))
        assert conflict.value.code == "REPORTING_SCHEMA_AHEAD"
        _assert_remedy(str(conflict.value))
