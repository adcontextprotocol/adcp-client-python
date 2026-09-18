"""Actual route identity, positive schema proof and optional delivery lifecycle."""

import asyncio

import pytest

from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.server.a2a_server import create_a2a_server

from ._production_support import production_harness
from ._production_transport import MountedProduction


@pytest.mark.parametrize("transport", ["mcp", "a2a"])
@pytest.mark.parametrize("mutation", ["remove", "replace"])
async def test_warm_proof_rechecks_the_actual_mount(transport, mutation, tmp_path):
    async with production_harness("postgres", tmp_path / "destination.sqlite") as h:
        support = h.production
        app = create_a2a_server(support.handler) if transport == "a2a" else h.mount
        proof = next(p for p in support._mounts if p.mount() is app)
        dispatcher = proof.dispatcher()
        mapping = (
            dispatcher._tool_manager._tools if transport == "mcp" else dispatcher._tool_callers
        )
        original = mapping.pop("get_reporting_status")
        if mutation == "replace":
            mapping["get_reporting_status"] = mapping["get_adcp_capabilities"]
        try:
            assert await support.reporting_delivery() == {}
            with pytest.raises(ReportingNotificationError, match="component_unready"):
                await support.activate(account_id=h.item.config.account_id)
        finally:
            mapping["get_reporting_status"] = original
        assert (await support.reporting_delivery())["managed_delivery"] is True


async def test_shared_scan_cancellation_and_post_scan_dynamic_check(tmp_path, monkeypatch):
    import adcp.reporting.production.schema as schema

    async with production_harness("postgres", tmp_path / "destination.sqlite") as h:
        support = h.production
        original = schema.validate_production_schema
        entered, release = asyncio.Event(), asyncio.Event()
        scans = 0

        async def paused(*args, **kwargs):
            nonlocal scans
            scans += 1
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            await original(*args, **kwargs)

        support.invalidate_schema_validation()
        with monkeypatch.context() as patch:
            patch.setattr(schema, "validate_production_schema", paused)
            canceled = asyncio.create_task(support.reporting_delivery())
            await asyncio.wait_for(entered.wait(), 5)
            callers = [asyncio.create_task(support.reporting_delivery()) for _ in range(4)]
            canceled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await canceled
            removed = h.mount._tool_manager._tools.pop("get_reporting_status")
            try:
                release.set()
                assert await asyncio.wait_for(asyncio.gather(*callers), 10) == [{}] * 4
            finally:
                h.mount._tool_manager._tools["get_reporting_status"] = removed
            assert scans == 1
            assert (await support.reporting_delivery())["managed_delivery"] is True
            await h.store.materializer_ready()
            assert scans == 1


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("mutation", ["closed", "replaced"])
@pytest.mark.parametrize("reconciled", [False, True])
async def test_warm_mounted_capability_rechecks_pool_lifecycle_and_identity(
    notifications, mutation, reconciled, tmp_path, monkeypatch
):
    from contextlib import AsyncExitStack

    import adcp.reporting.production.schema as schema

    pool_type = pytest.importorskip("psycopg_pool").AsyncConnectionPool
    async with production_harness(
        "postgres",
        tmp_path / "destination.sqlite",
        notifications=notifications,
        notification_delivery=notifications,
        reconciled=reconciled,
    ) as h:
        support = h.production
        mounted = MountedProduction(h)
        mounted.authorize(h.item)
        async with mounted.client() as client, AsyncExitStack() as stack:
            before = {}
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                _, before[transport] = await mounted.call(
                    client, "get_adcp_capabilities", {}, transport=transport
                )
                assert before[transport]["media_buy"]["reporting_delivery"]["managed_delivery"]
                if reconciled:
                    assert before[transport]["media_buy"]["reporting_delivery"][
                        "reconciled_billing"
                    ]
            replacement = None
            if mutation == "replaced":
                replacement = await stack.enter_async_context(
                    pool_type(
                        h.pool.conninfo, kwargs=h.pool.kwargs, min_size=1, max_size=2, open=False
                    )
                )
                await replacement.wait(timeout=5)
                assert replacement.closed is False
            else:
                await h.pool.close()
                assert h.pool.closed is True
            assert not support._task.done()
            scans = 0

            async def no_scan(*args, **kwargs):
                nonlocal scans
                scans += 1
                raise AssertionError("component refusal must precede a catalog scan")

            with monkeypatch.context() as patch:
                patch.setattr(schema, "validate_production_schema", no_scan)
                if replacement is not None:
                    patch.setattr(h.store, "_pool", replacement)
                for transport in before:
                    _, refused = await mounted.call(
                        client, "get_adcp_capabilities", {}, transport=transport
                    )
                    assert "reporting_delivery" not in refused.get("media_buy", {})
                    assert "webhook_signing" not in refused
                    expected_code = (
                        "notification_chain_unready"
                        if replacement is not None and notifications
                        else "reporting_production_component_unready"
                    )
                    with pytest.raises(ReportingNotificationError, match=expected_code):
                        await support.activate(account_id="acct_a")
                assert scans == 0
            if mutation == "replaced":
                for transport in before:
                    _, restored = await mounted.call(
                        client, "get_adcp_capabilities", {}, transport=transport
                    )
                    assert restored == before[transport]


@pytest.mark.parametrize("queue", ["projection", "core", "ready"])
@pytest.mark.parametrize("replacement_open", [False, True])
async def test_warm_mounted_capability_rechecks_each_queue_pool(
    queue, replacement_open, tmp_path, monkeypatch
):
    from contextlib import AsyncExitStack

    import adcp.reporting.production.schema as schema

    pool_type = pytest.importorskip("psycopg_pool").AsyncConnectionPool
    # The projection queue also participates without optional HTTP workers.
    delivery = queue != "projection"
    async with production_harness(
        "postgres",
        tmp_path / "destination.sqlite",
        notifications=True,
        notification_delivery=delivery,
        reconciled=True,
    ) as h:
        mount = MountedProduction(h)
        mount.authorize(h.item)
        outbox = (
            h.projection.outbox
            if queue == "projection"
            else h.production.notification_workers[0 if queue == "core" else 2].outbox
        )
        replacement = pool_type(
            h.pool.conninfo, kwargs=h.pool.kwargs, min_size=1, max_size=1, open=False
        )
        async with mount.client() as client, AsyncExitStack() as stack:
            if replacement_open:
                await stack.enter_async_context(replacement)
                await replacement.wait(timeout=5)
            assert replacement.closed is not replacement_open
            baseline = {}
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                _, baseline[transport] = await mount.call(
                    client, "get_adcp_capabilities", {}, transport=transport
                )
                assert baseline[transport]["media_buy"]["reporting_delivery"]["reconciled_billing"]
            scans = 0

            async def no_scan(*args, **kwargs):
                nonlocal scans
                scans += 1
                raise AssertionError("queue wiring checks must precede catalog proof")

            with monkeypatch.context() as patch:
                patch.setattr(schema, "validate_production_schema", no_scan)
                patch.setattr(outbox, "_pool", replacement)
                for transport in baseline:
                    _, refused = await mount.call(
                        client, "get_adcp_capabilities", {}, transport=transport
                    )
                    assert "reporting_delivery" not in refused.get("media_buy", {})
                    assert "webhook_signing" not in refused
                assert scans == 0
            for transport in baseline:
                _, restored = await mount.call(
                    client, "get_adcp_capabilities", {}, transport=transport
                )
                assert restored == baseline[transport]


@pytest.mark.parametrize("notifications", [False, True])
async def test_warm_mounted_discovery_does_not_checkout_a_saturated_valid_pool(
    notifications, tmp_path, monkeypatch
):
    import adcp.reporting.production.schema as schema

    async with production_harness(
        "postgres",
        tmp_path / "destination.sqlite",
        notifications=notifications,
        notification_delivery=notifications,
        reconciled=True,
    ) as h:
        mounted = MountedProduction(h)
        mounted.authorize(h.item)
        async with mounted.client() as client:
            expected = {}
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                _, expected[transport] = await mounted.call(
                    client, "get_adcp_capabilities", {}, transport=transport
                )
                assert expected[transport]["media_buy"]["reporting_delivery"]["reconciled_billing"]
            connections = []
            try:
                for _ in range(h.pool.max_size):
                    connections.append(await h.pool.getconn(timeout=5))
                assert h.pool.get_stats()["pool_available"] == 0
                assert h.pool.closed is False
                scans = checkouts = 0

                async def no_scan(*args, **kwargs):
                    nonlocal scans
                    scans += 1
                    raise AssertionError("warm discovery must reuse the catalog proof")

                def no_connection(*args, **kwargs):
                    nonlocal checkouts
                    checkouts += 1
                    raise AssertionError("warm discovery must not request a pool connection")

                with monkeypatch.context() as patch:
                    patch.setattr(schema, "validate_production_schema", no_scan)
                    patch.setattr(h.pool, "connection", no_connection)
                    for transport in expected:
                        _, result = await asyncio.wait_for(
                            mounted.call(client, "get_adcp_capabilities", {}, transport=transport),
                            5,
                        )
                        assert result == expected[transport]
                    assert scans == checkouts == 0
            finally:
                for connection in connections:
                    await h.pool.putconn(connection)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_optional_delivery_is_owned_and_broken_enabled_chain_fails_closed(backend, tmp_path):
    async with production_harness(
        backend, tmp_path / "destination.sqlite", notifications=True, notification_delivery=True
    ) as h:
        support = h.production
        assert support._notification_task is not None
        assert not support._notification_task.done()
        if backend == "postgres":
            fields = await support.reporting_delivery()
            assert fields["managed_delivery"] is True
            assert fields["ledger_notification"] == "reporting.ledger_changed"
            assert fields["readiness_notification"] == "reporting.delivery_ready"
            assert fields["status_notification"] == "reporting.status_changed"
        worker = support.notification_workers[-1]
        outbox = worker.outbox
        worker.outbox = support.notification_workers[0].outbox
        try:
            assert await support.reporting_delivery() == {}
            with pytest.raises(ReportingNotificationError, match="notification_chain_unready"):
                await support.activate(account_id=h.item.config.account_id)
        finally:
            worker.outbox = outbox
        await support.activate(account_id=h.item.config.account_id)
        assert h.subscriptions.lists
        assert {event for _, event in h.subscriptions.lists} == {
            "reporting.ledger_changed",
            "reporting.status_changed",
            "reporting.delivery_ready",
        }
        await support.aclose()
        assert support._notification_task is None
        assert await support.reporting_delivery() == {}


async def test_warm_catalog_proof_does_not_cache_signing_wiring(tmp_path, monkeypatch):
    import adcp.reporting.production.schema as schema

    async with production_harness(
        "postgres", tmp_path / "destination.sqlite", notifications=True, notification_delivery=True
    ) as h:
        support = h.production
        assert (await support.reporting_delivery())["managed_delivery"]
        mount = MountedProduction(h)
        mount.authorize(h.item)

        async def no_scan(*args, **kwargs):
            pytest.fail("warm immutable catalog proof was unnecessarily repeated")

        with monkeypatch.context() as patch:
            patch.setattr(schema, "validate_production_schema", no_scan)
            async with mount.client() as client:
                for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                    _, valid = await mount.call(
                        client, "get_adcp_capabilities", {}, transport=transport
                    )
                    assert valid["webhook_signing"]["algorithms"] == ["ed25519"]
                    # These are normative 3.2 obligations even though the
                    # additive schema fields remain optional for old agents.
                    assert valid["webhook_signing"]["delivery_retry_horizon_seconds"] == 86400
                    assert valid["identity"]["brand_json_url"] == (
                        "https://seller.example.test/brand.json"
                    )
                    # Each mutation follows a successful schema proof. Every
                    # request must inspect the current concrete participant.
                    for target, name, replacement in (
                        (support.notification_workers[0], "signing", h.signing),
                        (h.signing, "resolve", None),
                        (h.subscriptions, "get_active", None),
                    ):
                        with monkeypatch.context() as mutation:
                            mutation.setattr(target, name, replacement)
                            _, invalid = await mount.call(
                                client, "get_adcp_capabilities", {}, transport=transport
                            )
                            assert "webhook_signing" not in invalid
                            assert "reporting_delivery" not in invalid.get("media_buy", {})
                            with pytest.raises(ReportingNotificationError):
                                await support.activate(account_id="acct_a")
                    _, restored = await mount.call(
                        client, "get_adcp_capabilities", {}, transport=transport
                    )
                    assert restored == valid


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_current_key_contract_is_rechecked_before_account_activation(
    backend, tmp_path, monkeypatch, caplog
):
    from dataclasses import replace

    async with production_harness(
        backend, tmp_path / "destination.sqlite", notifications=True, notification_delivery=True
    ) as h:
        from ._reliable_support import notification_subscription

        h.subscriptions.put(notification_subscription(principal=h.item.binding.consumer_id))
        original = h.signing.resolve

        async def incompatible(**kwargs):
            material = await original(**kwargs)
            return replace(material, advertised_algorithms={"ed25519", "ecdsa-p256-sha256"})

        before = await h.image()
        with monkeypatch.context() as patch:
            patch.setattr(h.signing, "resolve", incompatible)
            with pytest.raises(ReportingNotificationError, match="notification_chain_unready"):
                await h.production.activate(account_id="acct_a")
            assert await h.image() == before
        assert not caplog.records
        assert await h.production.activate(account_id="acct_a")
