"""Provisional persistence cannot operate with a partial or unguarded catalog."""

import pytest

from adcp.reporting.ledger import LedgerConflictError, PgReportingLedgerStore

from ._generation_support import isolated_reporting_pool

DAMAGE = {
    "missing_trigger": (
        "DROP TRIGGER reporting_provisional_observation_immutable"
        " ON reporting_provisional_observations"
    ),
    "disabled_trigger": (
        "ALTER TABLE reporting_provisional_observations"
        " DISABLE TRIGGER reporting_provisional_observation_immutable"
    ),
    "changed_guard": (
        "CREATE OR REPLACE FUNCTION reporting_provisional_immutable()"
        " RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END; $$"
    ),
    "partial_extension": "DROP TABLE reporting_provisional_acquisitions CASCADE",
    "absent_extension": (
        "DROP TABLE reporting_provisional_observations,reporting_provisional_acquisitions;"
        " DROP FUNCTION reporting_provisional_immutable()"
    ),
}


@pytest.mark.parametrize("damage", DAMAGE)
@pytest.mark.parametrize("operation", ["read", "reserve", "commit"])
@pytest.mark.parametrize("notifications", [False, True])
async def test_provisional_operations_require_the_complete_immutable_extension(
    damage, operation, notifications
):
    from tests.test_reporting_provisional_observations import latest
    from tests.test_reporting_settling import _capabilities, _harness

    async with isolated_reporting_pool(autocommit=True) as pool:
        producer, store, _, _ = await _harness(
            _capabilities(restatement_window=None),
            store_factory=lambda clock: PgReportingLedgerStore(
                pool=pool, clock=clock, notifications=notifications
            ),
        )
        await producer.run_worker()
        observation = await latest(store)
        identity = {
            "account_id": observation.acquisition.account_id,
            "reporting_obligation_id": observation.acquisition.obligation_id,
        }
        (revision,) = await store.list_revisions(**identity)
        rows = await store.read_revision_rows(
            account_id=revision.account_id, reporting_revision_id=revision.reporting_revision_id
        )
        async with pool.connection() as connection:
            await connection.execute(DAMAGE[damage])
        with pytest.raises(LedgerConflictError) as failure:
            if operation == "read":
                await store.get_provisional_observation(**identity)
            elif operation == "reserve":
                await store.reserve_provisional_acquisition(observation.acquisition)
            else:
                await store.commit_provisional_observation(observation, revision, rows.rows)
        assert failure.value.code == "PROVISIONAL_SCHEMA_UNREADY"
        assert await store.list_revisions(**identity) == (revision,)
        assert (
            await store.read_revision_rows(
                account_id=revision.account_id,
                reporting_revision_id=revision.reporting_revision_id,
            )
            == rows
        )


@pytest.mark.parametrize("damage", DAMAGE)
async def test_notification_readiness_checks_a_present_known_extension(damage):
    from adcp.reporting.outbox import ReportingNotificationError
    from adcp.reporting.outbox._schema import validate_schema

    async with isolated_reporting_pool(autocommit=True) as pool:
        await PgReportingLedgerStore(pool=pool).create_schema()
        async with pool.connection() as connection:
            await connection.execute(DAMAGE[damage])
            if damage == "absent_extension":
                # Ordinary legacy notification readiness does not declare
                # provisional capability. Its original manifest remains valid.
                await validate_schema(connection)
            else:
                with pytest.raises(ReportingNotificationError, match="notification_schema_unready"):
                    await validate_schema(connection)


@pytest.mark.parametrize("notifications", [False, True])
async def test_bootstrap_rejects_an_existing_malformed_observation_column(notifications):
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingLedgerStore(pool=pool, notifications=notifications)
        await store.create_schema()
        async with pool.connection() as connection:
            await connection.execute(
                "ALTER TABLE reporting_provisional_acquisitions ALTER COLUMN payload DROP NOT NULL"
            )
        with pytest.raises(LedgerConflictError) as failure:
            await store.create_schema()
        assert failure.value.code == "PROVISIONAL_SCHEMA_UNREADY"
