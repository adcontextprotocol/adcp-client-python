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


async def test_status_notifications_cover_subscription_generations() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
        await store.create_schema()
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
        # A store with status notifications on (its outbox schema aside):
        # status checkpoints are keyed by offering, so every offering of one
        # subscription version is admitted.
        store._notifications_enabled = True
        (plan,) = await _plan(world, AGENCY, store, "2026-10-04T00:00:00Z")
        assert len({key.offering_id for key in plan.generations}) == len(plan.generations) > 1


async def test_a_six_column_status_checkpoint_schema_is_keyed_by_offering() -> None:
    """Migration 0006 widens status checkpoints and events published on the version key."""
    import json
    from dataclasses import replace

    from adcp_reporting_storage.migrations import migrate, migrations, pending_migrations
    from adcp_reporting_storage.outbox.status_schema import validate_status_schema

    from adcp.reporting.ledger import ReportingSubscriptionGeneration
    from adcp.reporting.ledger.models import ledger_asdict
    from adcp.reporting.ledger.notification_models import (
        ReportingDomainEvent,
        ReportingStatusScope,
        StatusChanged,
        event_storage,
    )
    from tests.conformance.reporting._generation_support import configuration

    async def checkpoint(connection, scope: ReportingStatusScope) -> None:
        await connection.execute(
            "INSERT INTO reporting_status_scope_checkpoints (account_id, consumer_namespace,"
            " delivery_config_id, version, scope_kind, obligation_namespace, offering_id, scope,"
            " fingerprint, snapshot, generation, source_sequence, baseline, publishable,"
            " initialized) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,1,0,TRUE,TRUE,TRUE)",
            (
                *scope.checkpoint_key,
                json.dumps(ledger_asdict(scope)),
                "a" * 64,
                json.dumps({"health": "complete", "issues": []}),
            ),
        )

    async def event(connection, scope: ReportingStatusScope, *, widened: bool) -> None:
        value = ReportingDomainEvent(
            "acct_a",
            f"rpn_{scope.generation_key.offering_id or 'account'}",
            NOW,
            StatusChanged(scope, "complete", "a" * 64, 1),
        )
        key = scope.checkpoint_key if widened else scope.checkpoint_key[:6]
        await connection.execute(
            "INSERT INTO reporting_status_notification_events (account_id, consumer_namespace,"
            " delivery_config_id, version, scope_kind, obligation_namespace,"
            + (" offering_id," if widened else "")
            + " notification_id, notification_type, cause_kind, cause_id, cause_generation,"
            " fingerprint, fired_at, snapshot) VALUES ("
            + ",".join(["%s"] * (len(key) + 7))
            + ",%s::jsonb)",
            (
                *key,
                value.notification_id,
                value.notification_type,
                "status_changed",
                value.cause_id,
                1,
                "a" * 64,
                NOW,
                json.dumps(event_storage(value)),
            ),
        )

    def scope(config) -> ReportingStatusScope:
        return ReportingStatusScope("acct_a", config.generation_key, feed_purpose="analytics")

    (widening,) = [item for item in migrations() if item.version == "0006"]
    assert widening.stopped_worker
    assert widening.includes == ("ledger/reporting_status_checkpoint_offerings.sql",)
    async with isolated_reporting_pool(autocommit=True) as pool:
        await migrate(pool)  # 0006 changes nothing without status notifications
        store = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
        account = replace(configuration(), delivery_config_id="hourly")
        offerings = {
            offering: replace(
                configuration(), subscription=ReportingSubscriptionGeneration("daily", offering)
            )
            for offering in ("pkg-a", "pkg-b")
        }
        for config in (account, *offerings.values()):
            await store.put_configuration(config)
        ledger = files("adcp_reporting_storage.ledger")
        async with pool.connection() as connection:
            # The published status layout: checkpoints and their events keyed
            # per subscription version, holding at most one offering of it.
            for name in (
                "reporting_status_notifications.sql",
                "reporting_status_selector_version.sql",
            ):
                await connection.execute(ledger.joinpath(name).read_text())
            await connection.execute("INSERT INTO reporting_status_accounts VALUES ('acct_a')")
            for config in (account, offerings["pkg-a"]):
                await checkpoint(connection, scope(config))
                await event(connection, scope(config), widened=False)
            await connection.execute("DELETE FROM adcp_schema_migrations WHERE version = '0006'")
        assert [item.version for item in await pending_migrations(pool)] == ["0006"]
        assert [item.version for item in await migrate(pool)] == ["0006"]
        assert await migrate(pool) == []
        async with pool.connection() as connection:
            for statement in widening.statements():  # idempotent
                await connection.execute(statement)
            await validate_status_schema(connection)
            primary = await (
                await connection.execute(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid ="
                    " 'reporting_status_scope_checkpoints'::regclass AND contype = 'p'"
                )
            ).fetchone()
            assert primary[0].endswith("obligation_namespace, offering_id)")
            # Each event takes its checkpoint's offering.
            rows = await (
                await connection.execute(
                    "SELECT notification_id, offering_id FROM reporting_status_notification_events"
                    " ORDER BY notification_id"
                )
            ).fetchall()
            assert rows == [("rpn_account", ""), ("rpn_pkg-a", "pkg-a")]
            # A second offering of the version keeps its own checkpoint and events.
            await checkpoint(connection, scope(offerings["pkg-b"]))
            await event(connection, scope(offerings["pkg-b"]), widened=True)
            keys = await (
                await connection.execute(
                    "SELECT delivery_config_id, scope_kind, offering_id"
                    " FROM reporting_status_scope_checkpoints ORDER BY 1, 3"
                )
            ).fetchall()
        assert keys == [
            ("daily", "configuration", "pkg-a"),
            ("daily", "configuration", "pkg-b"),
            ("hourly", "configuration", ""),
        ]


async def test_an_account_only_managed_delivery_schema_is_upgraded() -> None:
    """A schema whose managed-delivery tables still refuse subscription generations."""
    from adcp_reporting_storage.production.pg import PgReportingProductionStore

    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingProductionStore(pool=pool, notifications=True)
        await store.create_schema()
        async with pool.connection() as connection:
            for table in (
                "reporting_reconciliation_records",
                "reporting_materializer_discovery",
                "reporting_status_scope_checkpoints",
            ):
                await connection.execute(
                    f"ALTER TABLE {table} ADD CONSTRAINT {table}_account_generation"  # nosec B608
                    " CHECK (offering_id = '')"
                )
            await connection.execute(
                "ALTER TABLE reporting_materializer_discovery"
                " DROP CONSTRAINT reporting_materializer_discovery_pkey,"
                " ADD CONSTRAINT reporting_materializer_discovery_pkey PRIMARY KEY"
                " (account_id, consumer_id, delivery_config_id, delivery_config_version)"
            )
        for _ in range(2):  # idempotent
            await store.create_schema()
        async with pool.connection() as connection:
            checks = await (
                await connection.execute(
                    "SELECT conname FROM pg_constraint WHERE conname LIKE '%account_generation'"
                    " ORDER BY conname"
                )
            ).fetchall()
            primary = await (
                await connection.execute(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE conname = 'reporting_materializer_discovery_pkey'"
                )
            ).fetchone()
        # Production generations still serve account-level generations only.
        assert checks == [("reporting_production_generations_account_generation",)]
        assert primary[0].endswith("delivery_config_version, offering_id)")
