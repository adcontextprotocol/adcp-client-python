"""Caller ownership and stopped-worker retained-state recovery on real stores."""

from __future__ import annotations

import secrets
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from adcp.exceptions import ADCPTaskError
from adcp.reporting.ledger import (
    ConsumerStatusIngest,
    InMemoryReportingLedgerStore,
    LedgerConflictError,
    ReportingStatusCaller,
    ReportingStatusHandler,
)
from adcp.reporting.ledger.pg import PgReportingLedgerStore
from adcp.reporting.ledger.store import encode_cursor
from adcp.reporting.migration import (
    ReportingOwnershipBackfill,
    ReportingOwnershipMigrationError,
    backfill_legacy_reporting,
    legacy_generation_digest,
    migrate_legacy_reporting,
)

from ._generation_support import (
    NOW,
    configuration,
    isolated_reporting_pool,
    obligation_for,
    require_rolling_database,
    revision_for,
)


@pytest.fixture(params=["memory", "postgres"])
async def owned_store(request):
    if request.param == "memory":
        yield InMemoryReportingLedgerStore(clock=lambda: NOW)
    else:
        async with isolated_reporting_pool() as pool:
            store = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
            await store.create_schema()
            yield store


async def test_shared_account_generation_is_private_everywhere(owned_store):
    store = owned_store
    callers = [
        ReportingStatusCaller("account", "buyer-a"),
        ReportingStatusCaller("account", "buyer-b"),
        ReportingStatusCaller("other-account", "buyer-a"),
    ]
    configs = []
    for index, caller in enumerate(callers):
        config = configuration(caller.account_id, consumer_id=caller.consumer_id)
        config = replace(config, feed_purpose="analytics" if index != 1 else "billing")
        configs.append(config)
        await store.put_configuration(config)
        obligation = replace(obligation_for(config), reporting_obligation_id=f"obligation-{index}")
        await store.commit_obligation(obligation)
        revision, rows = revision_for(obligation, suffix=str(index))
        await store.commit_revision(revision, rows)
        statement = {
            "reporting_status_id": "shared-owned-status-2026",
            "delivery_config_id": config.delivery_config_id,
            "delivery_config_version": config.delivery_config_version,
            "report_definition_id": config.report_definition_id,
            "period": {
                "start": obligation.period.start.isoformat(),
                "end": obligation.period.end.isoformat(),
                "source_timezone": "UTC",
            },
            "consumer_status": "received",
            "status_as_of": NOW.isoformat(),
            "reporting_obligation_id": obligation.reporting_obligation_id,
            "reporting_revision_id": revision.reporting_revision_id,
            "observed_revision_content_sha256": revision.revision_content_sha256,
        }
        result = await ConsumerStatusIngest(store, enabled=True, clock=lambda: NOW).handle(
            {"statuses": [statement]},
            account_id=caller.account_id,
            consumer_id=caller.consumer_id,
        )
        assert result["results"][0]["result"] == "recorded", result

    # Another caller's commit must not alter this caller's durable boundary.
    boundary_before = await store.open_snapshot(caller=callers[0], filters_fingerprint="same")
    status_before = await store.read_status_snapshot(caller=callers[0])
    foreign_snapshot = await store.read_status_snapshot(caller=callers[1])
    await store.record_consumer_status(
        replace(
            foreign_snapshot.statuses[0],
            reporting_status_id="foreign-new-status-2026",
            supersedes_reporting_status_id=foreign_snapshot.statuses[0].reporting_status_id,
        )
    )
    boundary_after = await store.open_snapshot(caller=callers[0], filters_fingerprint="same")
    assert boundary_after.max_sequence == boundary_before.max_sequence
    assert boundary_after.snapshot_id == boundary_before.snapshot_id
    status_after = await store.read_status_snapshot(caller=callers[0])
    assert status_after.changes == status_before.changes
    assert status_after.max_sequence == status_before.max_sequence

    handler = ReportingStatusHandler(store, page_size=1, consumer_status_enabled=True)
    for index, (caller, config) in enumerate(zip(callers, configs)):
        assert await store.list_configurations(caller=caller) == (config,)
        snapshot = await store.read_status_snapshot(caller=caller)
        assert {o.consumer_id for o in snapshot.obligations} == {caller.consumer_id}
        assert len(snapshot.obligations) == len(snapshot.revisions) == 1
        assert len(snapshot.statuses) == (2 if index == 1 else 1)
        assert snapshot.statuses[0].consumer_id == caller.consumer_id
        boundary = await store.open_snapshot(caller=caller, filters_fingerprint="same")
        page = await store.read_page(
            snapshot=boundary,
            consumer_id=caller.consumer_id,
            delivery_config_ids=None,
            media_buy_ids=None,
            offset=0,
            limit=20,
            changes_after_sequence=None,
        )
        assert {record.reporting_obligation_id for record in page.obligations} == {
            f"obligation-{index}"
        }
        await store.put_configuration(
            replace(config, deactivated_at=config.deactivated_at + timedelta(hours=1))
        )
        payload = await handler.handle({"view": "periods"}, caller=caller)
        assert payload["periods"][0]["reporting_obligation_id"] == f"obligation-{index}"
        cursor = payload["pagination"]["cursor"]
        other = callers[(index + 1) % len(callers)]
        with pytest.raises(ADCPTaskError) as error:
            await handler.handle(
                {"view": "periods", "pagination": {"cursor": cursor}}, caller=other
            )
        assert error.value.error_info[0].recovery == "correctable"
    leases = []
    for i in range(3):
        lease = await store.lease_period_close(worker_id=str(i), now=NOW, lease_seconds=60)
        assert lease is not None
        leases.append(lease.generation_key)
    assert set(leases) == {c.generation_key for c in configs}
    # A request body cannot supply the owner, including sentinel-like identities.
    for owner in ("unknown", "__legacy__", "unassigned", "buyer-a"):
        account = "unowned" if owner == "buyer-a" else "account"
        empty = await handler.handle(
            {"view": "periods", "consumer_id": "buyer-a"},
            caller=ReportingStatusCaller(account, owner),
        )
        assert empty["periods"] == []


async def test_legacy_positions_require_explicit_correctable_restart(owned_store):
    handler = ReportingStatusHandler(owned_store)
    caller = ReportingStatusCaller("account", "buyer")
    for request in (
        {"pagination": {"cursor": encode_cursor({"snapshot": "legacy", "offset": 1})}},
        {"changes_after": encode_cursor({"seq": 1})},
    ):
        with pytest.raises(ADCPTaskError) as caught:
            await handler.handle(request, caller=caller)
        assert caught.value.error_codes == ["INVALID_CHECKPOINT"]
        assert caught.value.error_info[0].recovery == "correctable"
        assert "Restart" in str(caught.value)


async def test_maintenance_archive_backfill_preserves_evidence_and_never_replays():
    import json

    require_rolling_database()
    from psycopg import sql

    from adcp.reporting.ledger.pg import _configuration_payload, _fingerprint

    archive = "adcp_reporting_quarantine_" + secrets.token_hex(6)
    async with isolated_reporting_pool(autocommit=True) as pool:
        async with pool.connection() as c:
            for name in (
                "reporting_ledger.sql",
                "reporting_ledger_obligation_currency.sql",
                "reporting_ledger_reconciliation.sql",
            ):
                await c.execute(
                    (Path(__file__).parents[2] / "fixtures/reporting_ownership" / name).read_text()
                )
            config = configuration("account", consumer_id="buyer-a")
            payload = _configuration_payload(config)
            await c.execute(
                "INSERT INTO "
                "reporting_configurations(delivery_config_id,delivery_config_version,account_id,report_definition_id,reporting_profile,feed_purpose,required_finality,schedule,media_buy_ids,definition,authoritative_party,content_sha256)"
                " VALUES "
                "('daily',1,'account',%s,%s,'analytics','snapshot',%s::jsonb,%s::jsonb,%s::jsonb,'seller',%s)",
                (
                    config.report_definition_id,
                    config.reporting_profile,
                    json.dumps(payload["schedule"]),
                    json.dumps(config.media_buy_ids),
                    json.dumps(config.definition.to_storage()),
                    _fingerprint(payload),
                ),
            )
            await c.execute(
                "INSERT INTO reporting_configurations SELECT "
                "delivery_config_id,delivery_config_version,'unknown-account',report_definition_id,reporting_profile,feed_purpose,required_finality,account_timezone,schedule,media_buy_ids,activated_at,deactivated_at,automated_recovery_seconds,status_retention_days,definition,content_sha256,lease_worker_id,lease_expires_at,authoritative_party"
                " FROM reporting_configurations"
            )
            from psycopg.types.json import Jsonb

            obligation = replace(
                obligation_for(config), reporting_obligation_id="retained-obligation"
            )
            revision, rows = revision_for(obligation, suffix="retained")
            period = obligation.period
            fields = dict(
                reporting_obligation_id=obligation.reporting_obligation_id,
                account_id=config.account_id,
                delivery_config_id=config.delivery_config_id,
                delivery_config_version=config.delivery_config_version,
                report_definition_id=config.report_definition_id,
                reporting_profile=config.reporting_profile,
                feed_purpose=config.feed_purpose,
                period_key=period.period_key,
                period_start=period.start,
                period_end=period.end,
                source_timezone=period.source_timezone,
                expected_at=period.expected_at,
                scope_resolved_at=obligation.scope_resolved_at,
                automated_recovery_deadline_at=obligation.automated_recovery_deadline_at,
                required_finality=config.required_finality,
                media_buy_ids=Jsonb(list(config.media_buy_ids)),
                schedule=Jsonb(payload["schedule"]),
                definition=Jsonb(config.definition.to_storage()),
                created_at=obligation.created_at,
                currency="USD",
            )
            await c.execute(
                sql.SQL("INSERT INTO reporting_obligations ({}) VALUES ({})").format(
                    sql.SQL(",").join(map(sql.Identifier, fields)),
                    sql.SQL(",").join(sql.Placeholder() for _ in fields),
                ),
                tuple(fields.values()),
            )
            await c.execute(
                "INSERT INTO "
                "reporting_revisions(reporting_revision_id,account_id,reporting_obligation_id,finality,revision_content_sha256,row_count,control_totals,observed_at,data_through,created_at,content_sha256)"
                " VALUES (%s,%s,%s,'snapshot',%s,1,%s::jsonb,%s,%s,%s,%s)",
                (
                    revision.reporting_revision_id,
                    config.account_id,
                    obligation.reporting_obligation_id,
                    revision.revision_content_sha256,
                    json.dumps(revision.control_totals),
                    revision.observed_at,
                    revision.data_through,
                    revision.created_at,
                    "c" * 64,
                ),
            )
            await c.execute(
                "INSERT INTO reporting_revision_rows VALUES (%s,0,%s::jsonb)",
                (revision.reporting_revision_id, json.dumps(rows[0])),
            )
            await c.execute(
                "INSERT INTO reporting_ledger_changes(account_id,record_kind,record_id) "
                "VALUES ('account','obligation',%s),('account','revision',%s)",
                (obligation.reporting_obligation_id, revision.reporting_revision_id),
            )
            original_revision = (
                await (await c.execute("SELECT to_jsonb(t) FROM reporting_revisions t")).fetchone()
            )[0]
            await c.execute(
                "CREATE TABLE reporting_legacy_artifacts(id text primary key, body bytea); "
                "INSERT INTO reporting_legacy_artifacts VALUES "
                "('immutable-object',decode('00ff010203','hex'))"
            )
            await c.execute(
                "CREATE TABLE reporting_legacy_pending(id text primary key,state text,body "
                "bytea); INSERT INTO reporting_legacy_pending VALUES "
                "('old-pending','pending',decode('deadbeef','hex'))"
            )
            original = (
                await (
                    await c.execute(
                        "SELECT to_jsonb(t) FROM reporting_configurations t WHERE "
                        "account_id='account'"
                    )
                ).fetchone()
            )[0]
            store = PgReportingLedgerStore(pool=pool)
            with pytest.raises(ReportingOwnershipMigrationError):
                await store.create_schema()
            with pytest.raises(ReportingOwnershipMigrationError):
                await migrate_legacy_reporting(c, archive_schema=archive, workers_stopped=False)
            await migrate_legacy_reporting(c, archive_schema=archive, workers_stopped=True)
            try:
                await store.create_schema()
                for consumer in ("buyer-a", "__legacy__", "unassigned", "unknown"):
                    assert (
                        await store.list_configurations(
                            caller=ReportingStatusCaller("account", consumer)
                        )
                        == ()
                    )
                digest = await legacy_generation_digest(
                    c,
                    archive_schema=archive,
                    account_id="account",
                    delivery_config_id="daily",
                    delivery_config_version=1,
                )
                mapping = ReportingOwnershipBackfill(
                    "account",
                    "daily",
                    1,
                    "buyer-a",
                    "ownership-register-42",
                    digest,
                    "retain_without_replay",
                )
                with pytest.raises(ReportingOwnershipMigrationError):
                    await backfill_legacy_reporting(
                        c,
                        archive_schema=archive,
                        mapping=replace(mapping, evidence_sha256="0" * 64),
                        workers_stopped=True,
                    )
                await backfill_legacy_reporting(
                    c, archive_schema=archive, mapping=mapping, workers_stopped=True
                )
                await backfill_legacy_reporting(
                    c, archive_schema=archive, mapping=mapping, workers_stopped=True
                )
                with pytest.raises(ReportingOwnershipMigrationError):
                    await backfill_legacy_reporting(
                        c,
                        archive_schema=archive,
                        mapping=replace(mapping, consumer_id="buyer-b"),
                        workers_stopped=True,
                    )
                (restored,) = await store.list_configurations(
                    caller=ReportingStatusCaller("account", "buyer-a")
                )
                retained_snapshot = await store.read_status_snapshot(
                    caller=ReportingStatusCaller("account", "buyer-a")
                )
                assert (
                    retained_snapshot.obligations[0].reporting_obligation_id
                    == obligation.reporting_obligation_id
                )
                assert (
                    retained_snapshot.revisions[0].reporting_revision_id
                    == revision.reporting_revision_id
                )
                assert (
                    await store.read_revision_rows(
                        account_id="account", reporting_revision_id=revision.reporting_revision_id
                    )
                ).rows == tuple(rows)
                active_revision = (
                    await (
                        await c.execute("SELECT to_jsonb(t) FROM reporting_revisions t")
                    ).fetchone()
                )[0]
                assert active_revision == original_revision
                assert await store.list_all_configurations() == ()
                # A new service process starts over retained data without asking
                # for source authority or constructing workers for the old work.
                from adcp.reporting.service import ReliableReportingService

                def no_legacy_dispatch(configuration):
                    raise AssertionError("quarantined generation cannot acquire source authority")

                restarted = ReliableReportingService.postgres(
                    pool=pool, account_context=no_legacy_dispatch
                )
                await restarted.start()
                try:
                    assert await restarted.store.list_all_configurations() == ()
                    assert (
                        await restarted.store.read_status_snapshot(
                            caller=ReportingStatusCaller("account", "buyer-a")
                        )
                    ).revisions == retained_snapshot.revisions
                finally:
                    await restarted.close()

                assert restored.quarantined
                assert (
                    await store.list_configurations(
                        caller=ReportingStatusCaller("account", "buyer-b")
                    )
                    == ()
                )
                assert (
                    await store.list_configurations(
                        caller=ReportingStatusCaller("unknown-account", "__legacy__")
                    )
                    == ()
                )
                assert (
                    await store.lease_period_close(worker_id="restarted", now=NOW, lease_seconds=60)
                    is None
                )
                with pytest.raises(LedgerConflictError):
                    await store.put_configuration(replace(restored, quarantined=False))
                retained = (
                    await (
                        await c.execute(
                            sql.SQL(
                                "SELECT to_jsonb(t) FROM {}.reporting_configurations t WHERE "
                                "account_id='account'"
                            ).format(sql.Identifier(archive))
                        )
                    ).fetchone()
                )[0]
                assert retained == original
                assert bytes(
                    (
                        await (
                            await c.execute(
                                sql.SQL("SELECT body FROM {}.reporting_legacy_artifacts").format(
                                    sql.Identifier(archive)
                                )
                            )
                        ).fetchone()
                    )[0]
                ) == bytes.fromhex("00ff010203")
                assert bytes(
                    (
                        await (
                            await c.execute(
                                sql.SQL("SELECT body FROM {}.reporting_legacy_pending").format(
                                    sql.Identifier(archive)
                                )
                            )
                        ).fetchone()
                    )[0]
                ) == bytes.fromhex("deadbeef")
                assert (
                    await (
                        await c.execute("SELECT count(*) FROM reporting_production_work")
                    ).fetchone()
                )[0] == 0
                # A new owner-bound generation requires ordinary fresh admission.
                await store.put_configuration(replace(config, delivery_config_version=2))
                lease = await store.lease_period_close(
                    worker_id="new-worker", now=NOW, lease_seconds=60
                )
                assert lease.generation_key.delivery_config_version == 2
            finally:
                await c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(archive)))


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_installed_core_composition_uses_transport_owner(backend):
    from types import SimpleNamespace

    from adcp.reporting.service import ReliableReportingService
    from adcp.server import ADCPHandler

    def resolve(request, context):
        return ReportingStatusCaller(request["account"]["account_id"], context.caller_identity)

    async def check(service):
        await service.start()
        try:
            handler = service.install(ADCPHandler())
            for owner in ("buyer-a", "buyer-b"):
                config = configuration("account", consumer_id=owner)
                await service.store.put_configuration(config)
                await service.store.commit_obligation(obligation_for(config))
            for owner in ("buyer-a", "buyer-b"):
                response = await handler.get_reporting_status(
                    {"account": {"account_id": "account"}, "view": "periods"},
                    SimpleNamespace(caller_identity=owner),
                )
                expected = obligation_for(configuration("account", consumer_id=owner))
                assert [p["reporting_obligation_id"] for p in response["periods"]] == [
                    expected.reporting_obligation_id
                ]
        finally:
            await service.close()

    if backend == "memory":
        await check(
            ReliableReportingService.memory(account_context=lambda _: None, caller_resolver=resolve)
        )
    else:
        async with isolated_reporting_pool() as pool:
            await check(
                ReliableReportingService.postgres(
                    pool=pool, account_context=lambda _: None, caller_resolver=resolve
                )
            )


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_installed_managed_admission_reuses_daily_for_two_callers(backend, tmp_path):
    from types import SimpleNamespace

    from ._production_transport import MountedProduction
    from .test_reporting_production_configuration import wire_configuration
    from .test_reporting_production_service_factory import factory_harness

    async with factory_harness(backend, tmp_path / "destination.sqlite") as h:
        first = h.item
        second_config = replace(first.config, consumer_id="https://buyer.example.test/second")
        second_binding = replace(
            first.binding,
            generation_key=second_config.generation_key,
            consumer_id=second_config.consumer_id,
            destination_ref="second-destination",
        )
        first.writer.grant(second_binding)
        h.source.bind_generation(second_config)
        second = SimpleNamespace(
            config=second_config,
            binding=second_binding,
            writer=first.writer,
            obligation=obligation_for(second_config),
        )
        mounted = MountedProduction(h)
        for item, token in ((first, "first"), (second, "second")):
            mounted.authorize(item, token=token)
        async with mounted.client() as client:
            for item, token in ((first, "first"), (second, "second"), (first, "first")):
                h.item = item
                _, response = await mounted.call(
                    client,
                    "sync_accounts",
                    {
                        "idempotency_key": "owned-admission-" + token,
                        "accounts": [
                            {
                                "account": {"account_id": item.config.account_id},
                                "reporting_delivery_configs": [wire_configuration(h)],
                            }
                        ],
                    },
                    token=token,
                )
                assert response["status"] == "completed", response
                await h.store.commit_obligation(obligation_for(item.config))
            for item, token in ((first, "first"), (second, "second")):
                caller = ReportingStatusCaller(item.config.account_id, item.config.consumer_id)
                assert await h.store.list_configurations(caller=caller) == (item.config,)
                _, response = await mounted.call(
                    client,
                    "get_reporting_status",
                    {
                        "account": {"account_id": item.config.account_id},
                        "view": "periods",
                    },
                    token=token,
                )
                assert [p["reporting_obligation_id"] for p in response["periods"]] == [
                    item.obligation.reporting_obligation_id
                ], response
                _, restarted = await mounted.call(
                    client,
                    "get_reporting_status",
                    {
                        "account": {"account_id": item.config.account_id},
                        "view": "periods",
                        "pagination": {
                            "cursor": encode_cursor({"snapshot": "legacy", "offset": 1})
                        },
                    },
                    token=token,
                )
                from ._receipt_transport import error_code

                assert error_code(restarted) == "INVALID_CHECKPOINT", restarted
                from adcp.server.base import ToolContext

                with pytest.raises(ADCPTaskError) as caught:
                    await h.production.handler.get_reporting_status(
                        {
                            "account": {"account_id": item.config.account_id},
                            "view": "periods",
                            "pagination": {
                                "cursor": encode_cursor({"snapshot": "legacy", "offset": 1})
                            },
                        },
                        ToolContext(caller_identity=item.config.consumer_id),
                    )
                assert caught.value.error_info[0].recovery == "correctable"

            await h.store.put_configuration(replace(first.config, deactivated_at=h.clock()))
            assert (
                await h.store.list_configurations(
                    caller=ReportingStatusCaller(
                        second_config.account_id, second_config.consumer_id
                    )
                )
            )[0] == second_config
