"""Reporting-subscription owed sets in the PostgreSQL ledger (AdCP 3.3).

The generation key is (account, consumer, delivery_config_id,
delivery_config_version, offering_id), with offering_id '' for account-level
generations. These run the in-memory owed-set scenarios against PostgreSQL and
cover the in-place widening of a schema still on the four-column key.
"""

from __future__ import annotations

from importlib.resources import files

import pytest
from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

from adcp.reporting.ledger import (
    LedgerConflictError,
    ReportingConfigurationGenerationKey,
    ReportingStatusCaller,
    ReportingStatusHandler,
    ReportingSubscriptionLedger,
)
from adcp.reporting.ledger.consumer_status import ConsumerStatusIngest
from tests.conformance.reporting._generation_support import isolated_reporting_pool
from tests.test_reporting_subscription_engine import at, buy
from tests.test_reporting_subscription_ledger import AGENCY, OTHER, _plan, _world_with

NOW = at("2026-10-08T00:00:00Z")


async def _store(pool) -> PgReportingLedgerStore:
    store = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
    await store.create_schema()
    return store


async def test_owed_sets_are_sparse_idempotent_and_per_offering() -> None:
    async with isolated_reporting_pool() as pool:
        store = await _store(pool)
        assert isinstance(store, ReportingSubscriptionLedger)
        world = await _world_with(
            AGENCY,
            [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-03T04:00:00Z")],
            selector={
                "feed_purpose": "analytics",
                "period_duration": "P1D",
                "required_finality": "official",
                "dimensions": ["package"],
                "match": "all",
            },
        )
        (first,) = await _plan(world, AGENCY, store, "2026-10-04T00:00:00Z")
        assert sorted(key.offering_id or "" for key in first.generations) == [
            "pkg",
            "pkg-country",
            "pkg-creative",
            "pkg-region-utc",
        ]
        (again,) = await _plan(world, AGENCY, store, "2026-10-04T00:00:00Z")
        assert again.obligations_written == ()
        for key in first.generations:
            obligations = await store.list_generation_obligations(key)
            assert obligations and {o.offering_id for o in obligations} == {key.offering_id}
            assert await store.owed_set_through(key) == at("2026-10-04T00:00:00Z")
        # The horizon never regresses.
        key = first.generations[0]
        await store.record_owed_set_progress(key, at("2026-10-02T00:00:00Z"))
        assert await store.owed_set_through(key) == at("2026-10-04T00:00:00Z")
        # An owed set is never leased for period close.
        assert await store.lease_period_close(worker_id="w", now=NOW, lease_seconds=30) is None
        # An account generation is not an owed set.
        account_key = ReportingConfigurationGenerationKey(
            key.account_id, key.consumer_id, key.delivery_config_id, key.delivery_config_version
        )
        with pytest.raises(LedgerConflictError):
            await store.record_owed_set_progress(account_key, NOW)


async def test_two_principals_scope_and_status_ingest_over_postgres() -> None:
    async with isolated_reporting_pool() as pool:
        store = await _store(pool)
        agency = await _world_with(
            AGENCY, [buy("mb-a", "2026-10-01T04:00:00Z", "2026-10-10T04:00:00Z")]
        )
        other = await _world_with(
            OTHER, [buy("mb-b", "2026-10-02T04:00:00Z", "2026-10-03T04:00:00Z")]
        )
        await _plan(agency, AGENCY, store, "2026-10-04T00:00:00Z")
        await _plan(other, OTHER, store, "2026-10-04T00:00:00Z")
        handler = ReportingStatusHandler(store)
        mine = ReportingStatusCaller("acct-1", AGENCY.subject)
        periods = await handler.handle({"view": "periods"}, caller=mine)
        assert {tuple(p["media_buy_ids"]) for p in periods["periods"]} == {("mb-a",)}
        assert {p["offering_id"] for p in periods["periods"]} == {"pkg"}
        theirs = await handler.handle(
            {"view": "periods"}, caller=ReportingStatusCaller("acct-1", OTHER.subject)
        )
        assert [p["media_buy_ids"] for p in theirs["periods"]] == [["mb-b"]]
        open_scope = await handler.handle(
            {
                "view": "periods",
                "period": {"start": "2026-10-01T04:00:00Z", "end": "2026-10-07T04:00:00Z"},
            },
            caller=mine,
        )
        assert open_scope["scope"]["scope_closed"] is False
        assert open_scope["scope"]["coverage_complete"] is True

        ingest = ConsumerStatusIngest(store, enabled=True, clock=lambda: NOW)
        response = await ingest.handle(
            {
                "statuses": [
                    {
                        "reporting_status_id": "rps_subscription_pg_0001",
                        "delivery_config_id": "analytics",
                        "delivery_config_version": 1,
                        "report_definition_id": "def-pkg",
                        "period": {
                            "start": "2026-10-01T04:00:00Z",
                            "end": "2026-10-02T04:00:00Z",
                            "source_timezone": "America/New_York",
                        },
                        "consumer_status": "obligation_missing",
                        "status_as_of": "2026-10-03T12:00:00Z",
                    }
                ]
            },
            account_id="acct-1",
            consumer_id=AGENCY.subject,
        )
        assert response["results"][0]["result"] in {"recorded", "unchanged"}, response
        (status,) = await store.list_consumer_statuses(
            account_id="acct-1", consumer_id=AGENCY.subject
        )
        assert status.offering_id == "pkg"


async def test_a_four_column_schema_is_widened_losslessly() -> None:
    async with isolated_reporting_pool(autocommit=True) as pool:
        ledger = files("adcp_reporting_storage.ledger")
        async with pool.connection() as connection:
            # The published layout: the base ledger DDL on the four-column key.
            await connection.execute(ledger.joinpath("reporting_ledger.sql").read_text())
            await connection.execute(
                "INSERT INTO reporting_configurations (delivery_config_id,"
                " delivery_config_version, account_id, consumer_id, report_definition_id,"
                " reporting_profile, feed_purpose, required_finality, schedule, content_sha256)"
                " VALUES ('daily', 1, 'acct-1', 'buyer', 'def', 'delivery', 'analytics',"
                " 'official', '{}'::jsonb, %s)",
                ("0" * 64,),
            )
        for _ in range(2):  # idempotent
            await PgReportingLedgerStore(pool=pool).create_schema()
        async with pool.connection() as connection:
            primary = await (
                await connection.execute(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE conrelid = 'reporting_configurations'::regclass AND contype = 'p'"
                )
            ).fetchone()
            rows = await (
                await connection.execute(
                    "SELECT delivery_config_id, offering_id FROM reporting_configurations"
                )
            ).fetchall()
        assert primary[0].endswith("delivery_config_version, offering_id)")
        assert rows == [("daily", "")]


async def test_a_foreign_reference_to_the_key_refuses_the_widening() -> None:
    async with isolated_reporting_pool(autocommit=True) as pool:
        from psycopg.errors import RaiseException

        ledger = files("adcp_reporting_storage.ledger")
        async with pool.connection() as connection:
            await connection.execute(ledger.joinpath("reporting_ledger.sql").read_text())
            await connection.execute(
                'CREATE TABLE adopter_reference (account_id TEXT COLLATE "C",'
                ' consumer_id TEXT COLLATE "C", delivery_config_id TEXT COLLATE "C",'
                " delivery_config_version INTEGER, FOREIGN KEY (account_id, consumer_id,"
                " delivery_config_id, delivery_config_version) REFERENCES"
                " reporting_configurations)"
            )
        with pytest.raises(RaiseException, match="stopped-worker maintenance"):
            await PgReportingLedgerStore(pool=pool).create_schema()


async def test_status_notifications_refuse_subscription_generations() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
        await store.create_schema()
        world = await _world_with(
            AGENCY, [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-03T04:00:00Z")]
        )
        # A store with status notifications on (its outbox schema aside).
        store._notifications_enabled = True
        with pytest.raises(LedgerConflictError) as refused:
            await _plan(world, AGENCY, store, "2026-10-04T00:00:00Z")
        assert refused.value.code == "UNSUPPORTED_FEATURE"
