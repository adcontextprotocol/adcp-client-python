"""The rc.4 forecast contract at producer, frozen store and public boundaries."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from jsonschema.validators import validator_for

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.feed.errors import ReportingFeedError
from adcp.reporting.ledger import ProducerOfferings, ReportingProducer, ReportingScheduleSpec
from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.types import GetReportingStatusRequest
from adcp.validation.schema_loader import get_named_validator, get_validator

from ._feed_support import MountedFeed, feed_harness, walk
from ._generation_support import START, UncalledSource, configuration, revision_for
from ._projection_support import projection_harness
from ._receipt_transport import error_code
from .test_reporting_schedule_schema import formats

RC3 = "3.2-rc.3"
RC4 = "3.2-rc.4"
CONSUMER = "https://buyer.example.test/agent"


@pytest.fixture(autouse=True)
def _a2a_compat_send_and_aggregate():
    # Use the real public async-generator transport instead of the unit mock shim.
    pass


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("version", [RC3, RC4])
async def test_production_discovery_advertises_its_usable_reporting_pin(backend, version, tmp_path):
    from ._production_support import production_harness
    from ._production_transport import MountedProduction

    async with production_harness(
        backend, tmp_path / "discovery.sqlite", adcp_version=version
    ) as h:
        mounted = MountedProduction(h)
        mounted.authorize(h.item)
        async with mounted.client() as client:
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                _, caps = await mounted.call(
                    client, "get_adcp_capabilities", {}, transport=transport
                )
                assert caps["adcp"]["supported_versions"] == [version]
                assert caps["adcp_version"] == version


@pytest.mark.parametrize("version", ["3.0", "3.1"])
async def test_production_mount_rejects_releases_without_reporting_schemas(version, tmp_path):
    from adcp.exceptions import ConfigurationError

    from ._production_support import production_harness

    with pytest.raises(ConfigurationError, match="reporting"):
        async with production_harness(
            "memory", tmp_path / "unsupported.sqlite", adcp_version=version
        ):
            pass


@pytest.mark.parametrize("mutation", ["value", "method"])
async def test_warm_production_proof_cannot_hide_a_changed_protocol_pin(
    mutation, tmp_path, monkeypatch
):
    from ._production_support import production_harness
    from ._production_transport import MountedProduction

    async with production_harness("postgres", tmp_path / "pin.sqlite", adcp_version=RC3) as h:
        mounted = MountedProduction(h)
        mounted.authorize(h.item)
        async with mounted.client() as client:
            _, before = await mounted.call(client, "get_adcp_capabilities", {})
            assert before["media_buy"]["reporting_delivery"]["managed_delivery"]
            if mutation == "value":
                monkeypatch.setattr(h.production.handler, "_adcp_version", RC4)
            else:
                monkeypatch.setattr(h.production.handler, "get_adcp_version", lambda: RC4)
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                _, after = await mounted.call(
                    client, "get_adcp_capabilities", {}, transport=transport
                )
                assert after["adcp_version"] == RC3
                assert after["adcp"]["supported_versions"] == [RC3]
                assert not after.get("media_buy", {}).get("reporting_delivery")


async def scheduled(h, *, now=0.5, complete=True):
    h.clock.now = START + timedelta(hours=now)
    h.store._clock = h.clock
    config = replace(configuration(), deactivated_at=None)
    await h.store.put_configuration(config)
    producer = ReportingProducer(
        source=UncalledSource(), offerings=ProducerOfferings(), store=h.store
    )
    obligations = await producer.close_elapsed_periods(config, now=h.clock())
    if complete:
        for obligation in obligations:
            revision, rows = revision_for(obligation, suffix=obligation.reporting_obligation_id)
            await h.store.commit_revision(revision, rows)
    if hasattr(h, "projection"):
        await h.projection.activate(account_id=config.account_id)
    return config, obligations


def mount(h, config, version):
    mounted = MountedFeed(h, version=version, hydrated=True, registry_kind="oauth")
    mounted.authorize(
        SimpleNamespace(obligation=config, binding=SimpleNamespace(consumer_id=CONSUMER))
    )
    return mounted


def request(version, view="summary"):
    return {"adcp_version": version, "account": {"account_id": "acct_a"}, "view": view}


def capture_clock(h, monkeypatch):
    if h.pool is not None:
        from adcp.reporting.feed import pg

        async def now(connection):
            # Same account-locked SQL read with an explicit conformance instant.
            # No projection, persisted snapshot or producer is substituted.
            return (
                await (await connection.execute("SELECT %s::timestamptz", (h.clock(),))).fetchone()
            )[0]

        monkeypatch.setattr(pg, "_now", now)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
async def test_complete_forecast_uses_period_start_without_creating_future_work(
    backend, notifications
):
    async with feed_harness(backend, notifications=notifications) as h:
        config, obligations = await scheduled(h)
        assert obligations == []
        before = await h.image()
        captured = await h.store.read_status_snapshot(account_id=config.account_id)
        handler = ReportingStatusHandler(h.store)
        caller = ReportingStatusCaller(config.account_id, CONSUMER)
        old = handler.render_snapshot(request(RC3), caller=caller, snapshot=captured)
        new = handler.render_snapshot(request(RC4), caller=caller, snapshot=captured)
        default = handler.render_snapshot({}, caller=caller, snapshot=captured)
        assert old["next_expected_at"] == "2026-09-01T02:00:00Z"
        assert new["next_expected_at"] == default["next_expected_at"] == "2026-09-01T01:00:00Z"
        for field in ("health", "scope", "coverage", "obligation_counts", "issues", "ledger_as_of"):
            assert old[field] == new[field] == default[field]
        assert new["health"] == "complete"
        assert new["obligation_counts"]["total"] == 0
        assert new["coverage"]["media_buy_ids"] == []
        assert new["ledger_snapshot_id"] != old["ledger_snapshot_id"]
        for relative in (
            "media-buy/get-reporting-status-response.json",
            "bundled/media-buy/get-reporting-status-response.json",
        ):
            raw = get_named_validator(relative, version=RC4)
            assert raw is not None
            raw.validate(new)
        assert await h.image() == before
        await h.store.put_configuration(replace(config, deactivated_at=START))
        h.clock.now += timedelta(hours=8)
        assert handler.render_snapshot(request(RC4), caller=caller, snapshot=captured) == new
        assert handler.render_snapshot(request(RC3), caller=caller, snapshot=captured) == old


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("mode", ["core", "projection"])
@pytest.mark.parametrize("version", [RC3, RC4])
@pytest.mark.parametrize("complete", [False, True])
async def test_mounted_summary_periods_schema_and_client_use_the_same_pin(
    backend, mode, version, complete, monkeypatch
):
    factory = feed_harness if mode == "core" else projection_harness
    async with factory(backend) as h:
        config, obligations = await scheduled(h, now=0.5 if complete else 1.5, complete=complete)
        capture_clock(h, monkeypatch)
        mounted = mount(h, config, version)
        before_work = await h.works()
        async with mounted.client() as client:
            _, inventory = await mounted.mcp(client, inventory=True)
            schema = next(
                t["outputSchema"] for t in inventory["tools"] if t["name"] == "get_reporting_status"
            )
            advertised = validator_for(schema)(schema, format_checker=formats())
            for view in ("summary", "periods"):
                for protocol in ("mcp", "a2a-0.3", "a2a-1.0"):
                    call = mounted.mcp if protocol == "mcp" else mounted.a2a
                    kwargs = {} if protocol == "mcp" else {"v1": protocol == "a2a-1.0"}
                    _, raw = await call(client, request(version, view), **kwargs)
                    assert "health" in raw, raw
                    assert (raw["health"] == "complete") == complete
                    advertised.validate(raw)
                    get_validator("get_reporting_status", "sync", version=version).validate(raw)
                    if complete and view == "summary":
                        assert raw["next_expected_at"] == (
                            "2026-09-01T01:00:00Z" if version == RC4 else "2026-09-01T02:00:00Z"
                        )
                        assert raw["obligation_counts"]["total"] == 0
                    elif complete and view == "periods":
                        assert raw["periods"] == []
                        assert ("next_expected_at" in raw) == (
                            version == RC3 and mode == "projection"
                        )
                    elif view == "summary" or mode == "projection":
                        assert raw["next_expected_at"] == "2026-09-01T02:00:00Z"
                    if view == "periods":
                        assert len(raw["periods"]) == len(obligations)
        # Actual SDK transports, generated response parsing and client validators.
        for a2a_version in ("0.3", "1.0"):
            async with mounted.sdk_clients(a2a_version) as (clients, observed):
                for client in clients.values():
                    result = await client.get_reporting_status(
                        GetReportingStatusRequest.model_validate(request(version))
                    )
                    assert result.success, result
                    data = result.data.model_dump(mode="json", exclude_none=True)
                    assert data["next_expected_at"] == (
                        "2026-09-01T01:00:00Z"
                        if complete and version == RC4
                        else "2026-09-01T02:00:00Z"
                    )
                assert observed and all(p[2]["adcp_version"] == version for p in observed)
        assert await h.works() == before_work


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("version", [RC3, RC4])
async def test_frozen_continuations_retain_bytes_and_reject_cross_version_positions(
    backend, version, monkeypatch
):
    async with projection_harness(backend) as h:
        config, _ = await scheduled(h, now=1.5)
        capture_clock(h, monkeypatch)
        caller = ReportingDeliveryPrincipal("acct_a", CONSUMER)
        req = {**request(version, "periods"), "pagination": {"max_results": 1}}
        first = await h.store.read_reporting_feed(req, caller=caller)
        assert first["pagination"]["has_more"]
        snapshot = await h.store.read_reporting_feed_snapshot(
            first["ledger_snapshot_id"], caller=caller
        )
        document = canonical_json_utf8_v1(snapshot.to_storage())
        assert ("next_expected_at" in first) == (version == RC3)
        other = RC4 if version == RC3 else RC3
        pagination = {"max_results": 1, "cursor": first["pagination"]["cursor"]}
        for position in (
            {"pagination": pagination},
            {"changes_after": first["changes_checkpoint"]},
        ):
            with pytest.raises(ReportingFeedError) as error:
                await h.store.read_reporting_feed(
                    {**req, **position, "adcp_version": other}, caller=caller
                )
            assert error.value.code == "REPORTING_FEED_VERSION_MISMATCH"
        original_pages, original_rows, checkpoint = await walk(h.store, req, caller, first=first)
        await h.store.put_configuration(replace(config, deactivated_at=START))
        h.clock.now += timedelta(hours=8)
        if h.pool is None:
            from adcp.reporting.projection.memory import InMemoryReportingProjectionStore

            store = InMemoryReportingProjectionStore(clock=h.clock)
            for key, value in vars(h.store).items():
                if key not in {"_clock", "_lock"}:
                    vars(store)[key] = deepcopy(value)
        else:
            from adcp.reporting.projection.pg import PgReportingProjectionStore

            store = PgReportingProjectionStore(pool=h.pool, clock=h.clock)
        h.store = store
        repeated, rows, repeated_checkpoint = await walk(store, req, caller, first=first)
        assert (
            repeated == original_pages
            and rows == original_rows
            and repeated_checkpoint == checkpoint
        )
        snapshot = await store.read_reporting_feed_snapshot(
            first["ledger_snapshot_id"], caller=caller
        )
        assert canonical_json_utf8_v1(snapshot.to_storage()) == document
        mounted = mount(h, config, version)
        continuation = {**req, "pagination": pagination}
        async with mounted.client() as client:
            for call in (mounted.mcp, mounted.a2a):
                _, raw = await call(client, continuation)
                assert raw == original_pages[1]
                _, crossed = await call(client, {**continuation, "adcp_version": other})
                assert error_code(crossed) == "VERSION_UNSUPPORTED", crossed
                assert "pin" in json.dumps(crossed).lower()


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("version", [RC3, RC4])
async def test_status_notification_mount_pin_controls_rendering_without_optional_validation(
    backend, version
):
    from adcp.reporting.ledger.status_server import ReportingStatusNotificationHandler
    from adcp.reporting.receipts.handler import _consumer

    async with feed_harness(backend) as h:
        config, _ = await scheduled(h)
        mounted = mount(h, config, version)

        async def resolve_caller(params, context):
            consumer = await _consumer(context, mounted.registry)
            account = await mounted.resolve_account(params["account"], context, consumer)
            return ReportingStatusCaller(account, consumer)

        mounted.handler = ReportingStatusNotificationHandler(
            ReportingStatusHandler(h.store),
            resolve_caller=resolve_caller,
            adcp_version=version,
        )
        async with mounted.client(validation=None) as client:
            _, inventory = await mounted.mcp(client, inventory=True)
            schema = next(
                t["outputSchema"] for t in inventory["tools"] if t["name"] == "get_reporting_status"
            )
            for call in (mounted.mcp, mounted.a2a):
                _, result = await call(client, request(version))
                assert result["next_expected_at"] == (
                    "2026-09-01T01:00:00Z" if version == RC4 else "2026-09-01T02:00:00Z"
                )
                validator_for(schema)(schema, format_checker=formats()).validate(result)
                _, rejected = await call(client, request(RC3 if version == RC4 else RC4))
                assert error_code(rejected) == "VERSION_UNSUPPORTED", rejected


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_rc4_nearest_captured_generation_and_historical_scope_filters(backend):
    async with feed_harness(backend) as h:
        h.clock.now = START + timedelta(minutes=30)
        h.store._clock = h.clock
        first = replace(
            configuration(),
            delivery_config_id="forecast-first",
            deactivated_at=None,
            schedule=ReportingScheduleSpec("PT2H", "PT1H", "utc", period_anchor=START),
        )
        anchor = START + timedelta(minutes=45)
        second = replace(
            first,
            delivery_config_id="forecast-second",
            activated_at=anchor,
            schedule=ReportingScheduleSpec("PT1H", "PT1H", "utc", period_anchor=anchor),
        )
        foreign = replace(
            second,
            account_id="acct_b",
            activated_at=START,
            schedule=replace(second.schedule, period_anchor=START + timedelta(minutes=40)),
        )
        producer = ReportingProducer(
            source=UncalledSource(), offerings=ProducerOfferings(), store=h.store
        )
        for config in (first, second, foreign):
            await h.store.put_configuration(config)
            assert await producer.close_elapsed_periods(config, now=h.clock()) == []
        captured = await h.store.read_status_snapshot(account_id="acct_a")
        handler = ReportingStatusHandler(h.store)
        caller = ReportingStatusCaller("acct_a", CONSUMER)
        before = await h.image()
        common = {"adcp_version": RC4, "view": "summary"}
        expected = {
            "next_expected_at": "2026-09-01T00:45:00Z",
            "health": "complete",
        }
        original = handler.render_snapshot(common, caller=caller, snapshot=captured)
        for filters, forecast in (
            ({}, expected["next_expected_at"]),
            ({"delivery_config_ids": [first.delivery_config_id]}, "2026-09-01T02:00:00Z"),
            ({"delivery_config_ids": [second.delivery_config_id]}, expected["next_expected_at"]),
            ({"delivery_config_ids": ["absent"]}, None),
            ({"feed_purposes": ["billing"]}, None),
            ({"media_buy_ids": ["foreign-buy"]}, None),
            (
                {
                    "delivery_config_ids": [first.delivery_config_id],
                    "period": {"start": START.isoformat(), "end": h.clock().isoformat()},
                },
                "2026-09-01T02:00:00Z",
            ),
        ):
            result = handler.render_snapshot(
                {**common, **filters}, caller=caller, snapshot=captured
            )
            assert result["health"] == "complete" and result["obligation_counts"]["total"] == 0
            assert result.get("next_expected_at") == forecast
            assert result["ledger_as_of"] == "2026-09-01T00:30:00Z"
            get_named_validator(
                "media-buy/get-reporting-status-response.json", version=RC4
            ).validate(result)
        assert all(original[key] == value for key, value in expected.items())
        assert await h.image() == before
        await h.store.put_configuration(replace(second, deactivated_at=anchor))
        h.clock.now += timedelta(days=1)
        assert handler.render_snapshot(common, caller=caller, snapshot=captured) == original


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize(
    "start,following",
    [
        ("2026-03-08T05:00:00+00:00", "2026-03-09T04:00:00Z"),
        ("2026-11-01T04:00:00+00:00", "2026-11-02T05:00:00Z"),
    ],
)
async def test_rc4_period_start_forecast_retains_civil_dst_and_offset_instants(
    backend, start, following
):
    start = datetime.fromisoformat(start)
    async with feed_harness(backend) as h:
        h.clock.now = (start + timedelta(minutes=30)).astimezone(
            timezone(timedelta(hours=5, minutes=30))
        )
        h.store._clock = h.clock
        config = replace(
            configuration(),
            activated_at=start,
            deactivated_at=None,
            account_timezone="America/New_York",
            schedule=ReportingScheduleSpec("P1D", "PT1H", "account_timezone", period_anchor=start),
        )
        await h.store.put_configuration(config)
        producer = ReportingProducer(
            source=UncalledSource(), offerings=ProducerOfferings(), store=h.store
        )
        assert await producer.close_elapsed_periods(config, now=h.clock()) == []
        caller = ReportingStatusCaller(config.account_id, CONSUMER)
        handler = ReportingStatusHandler(h.store)
        snapshot = await h.store.read_status_snapshot(account_id=config.account_id)
        result = handler.render_snapshot(request(RC4), caller=caller, snapshot=snapshot)
        assert result["next_expected_at"] == following
        assert result["obligation_counts"]["total"] == 0 and result["health"] == "complete"
        boundary = datetime.fromisoformat(following.replace("Z", "+00:00"))
        h.clock.now = boundary
        obligations = await producer.close_elapsed_periods(config, now=h.clock())
        assert len(obligations) == 1 and obligations[0].period.end == boundary
        assert obligations[0].period.expected_at == boundary + timedelta(hours=1)
        revision, rows = revision_for(obligations[0])
        await h.store.commit_revision(revision, rows)
        next_day = await handler.handle(request(RC4), caller=caller)
        assert next_day["next_expected_at"] == (boundary + timedelta(days=1)).isoformat().replace(
            "+00:00", "Z"
        )
        assert next_day["obligation_counts"]["total"] == 1
        assert handler.render_snapshot(request(RC4), caller=caller, snapshot=snapshot) == result


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("version", [RC3, RC4])
@pytest.mark.parametrize("reconciled", [False, True])
async def test_admitted_producer_verified_artifact_and_receipt_keep_the_future_summary(
    backend, version, reconciled, tmp_path
):
    from adcp.reporting.ledger import ReportingRevisionReceiptRecord
    from adcp.reporting.ledger.delivery import receipt_to_wire

    from ._production_support import production_harness
    from ._production_transport import MountedProduction
    from ._projection_support import drain
    from .test_reporting_production_lifecycle import observed_now
    from .test_reporting_production_lock_order import source_turn

    async with production_harness(
        backend,
        tmp_path / "rc4-provider.sqlite",
        count=3,
        source_publication=True,
        reconciled=reconciled,
        adcp_version=version,
    ) as h:
        support, item = h.production, h.item
        assert support.handler.get_adcp_version() == version
        await support.activate(account_id=item.config.account_id)
        turn = await source_turn(support)
        assert not turn.slices_failed and len(turn.revisions_committed) == 1
        revisions = await h.store.list_revisions(
            account_id=item.config.account_id,
            reporting_obligation_id=item.obligation.reporting_obligation_id,
        )
        assert len(revisions) == 1
        item.revision = revisions[0]
        materialized = await support.materializer.run_once()
        assert materialized.state == "verified" and item.writer.writes == 1
        mounted = MountedProduction(h)
        mounted.authorize(item)
        async with mounted.client() as client:
            if reconciled:
                receipt = ReportingRevisionReceiptRecord(
                    item.scope,
                    "rc4-production-accepted",
                    item.revision.reporting_revision_id,
                    materialized.reporting_materialization_id,
                    "accepted",
                    item.binding.verification_profile,
                    item.revision.row_count,
                    item.revision.managed_control_totals,
                    await observed_now(h),
                    observed_canonical_content_digest=item.revision.canonical_content_digest,
                )
                _, accepted = await mounted.call(
                    client,
                    "sync_reporting_receipts",
                    {
                        "account": {"account_id": item.config.account_id},
                        "idempotency_key": "rc4-production-receipt",
                        "receipts": [receipt_to_wire(receipt)],
                    },
                    transport="a2a-1.0",
                )
                assert accepted["results"][0]["result"] == "recorded", accepted
            await drain(h.projection, item.config.account_id)
            await h.store.put_configuration(
                replace(item.config, deactivated_at=item.obligation.period.end)
            )
            anchor = ((await observed_now(h)) + timedelta(days=1)).replace(
                minute=0, second=0, microsecond=0
            )
            future = replace(
                item.config,
                delivery_config_version=2,
                activated_at=anchor,
                deactivated_at=None,
                schedule=replace(item.config.schedule, period_anchor=anchor),
            )
            producer = support.offerings[0].producer
            producer._source.bind_generation(future)
            destination = replace(item.binding, generation_key=future.generation_key)
            item.writer.grant(destination)
            await h.store.admit_production_configuration(
                future, destination, offering_id=support.offerings[0].offering_id
            )
            idle = await source_turn(support)
            assert not idle.slices_failed and not idle.revisions_committed
            assert len(producer._source.requests) == 1
            before_work = await h.works()
            expectation = anchor if version == RC4 else anchor + timedelta(hours=2)
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                for view in ("summary", "periods"):
                    _, page = await mounted.call(
                        client, "get_reporting_status", request(version, view), transport=transport
                    )
                    assert page["health"] == "complete", page
                    get_validator("get_reporting_status", "sync", version=version).validate(page)
                    if version == RC4:
                        for relative in (
                            "media-buy/get-reporting-status-response.json",
                            "bundled/media-buy/get-reporting-status-response.json",
                        ):
                            get_named_validator(relative, version=version).validate(page)
                    if view == "summary":
                        assert page["next_expected_at"] == expectation.isoformat().replace(
                            "+00:00", "Z"
                        )
                        assert page["obligation_counts"]["total"] == 1
                    elif version == RC4:
                        assert "next_expected_at" not in page
            assert await h.works() == before_work
        async with MountedFeed.sdk_clients(mounted, "1.0") as (clients, observed):
            for client in clients.values():
                result = await client.get_reporting_status(
                    GetReportingStatusRequest.model_validate(request(version))
                )
                assert result.success and result.data.health == "complete", result
            assert observed and all(p[2]["adcp_version"] == version for p in observed)
