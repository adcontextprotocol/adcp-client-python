"""Real catalog accounting through the supported activity/capability composition."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import FrozenInstanceError, replace

import pytest

from adcp.decisioning import (
    create_adcp_server_from_platform,
    validate_capabilities_response_shape,
    validate_capabilities_response_shape_async,
)
from adcp.decisioning.capabilities import Account, MediaBuy, WebhookSigning
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.outbox import (
    ReportingActivityProjector,
    ReportingActivitySupport,
    ReportingNotificationWorker,
    ReportingStatusSupport,
)
from adcp.types import ReportingDeliveryCapabilities
from tests.test_decisioning_capabilities_projection import _SalesPlatform
from tests.test_reporting_ledger import _OFFERING

from ._reliable_support import NotificationHarness, reliable_factory
from .test_reporting_webhook_activity import worker_for


@contextmanager
def mounted_activity(support):
    reporting = ReportingDeliveryCapabilities.model_validate(
        {
            "supported": True,
            "configuration_task": "sync_accounts",
            "status_task": "get_reporting_status",
            "offerings": [_OFFERING],
            "automated_recovery_window_seconds": 3600,
            "status_retention_days": 30,
        }
    ).model_copy(update={"readiness_notification": None, "status_notification": None})

    class Listing:
        def list(self, filter=None):
            return []

    class Platform(_SalesPlatform):
        capabilities = replace(
            _SalesPlatform.capabilities, specialisms=[], supported_protocols=["media_buy"]
        )
        accounts = Listing()
        claim = True
        claim_account = False
        calls = 0

        def get_adcp_capabilities_for_request(self, params=None, context=None):
            self.calls += 1
            return replace(
                self.capabilities,
                experimental_features=["media_buy.reporting_delivery"],
                media_buy=MediaBuy(
                    supported_pricing_models=["cpm"],
                    reporting_delivery=reporting.model_copy(
                        update={"supports_webhook_activity": self.claim}
                    ),
                ),
                account=Account.model_validate(
                    {
                        "supported_billing": ["operator"],
                        "notifications": {
                            "supported": True,
                            "registration_task": "sync_accounts",
                            "read_task": "list_accounts",
                            "event_types": ["account.status_changed"],
                            "supports_webhook_activity": self.claim_account,
                        },
                    }
                ),
                webhook_signing=WebhookSigning(
                    supported=True,
                    profile="adcp/webhook-signing/v1",
                    algorithms=["ed25519"],
                    delivery_retry_horizon_seconds=86400,
                ),
                webhook_signing_managed_externally=True,
            )

    handler, executor, _ = create_adcp_server_from_platform(
        Platform(),
        reporting_activity=support,
        account_activity=support.projector,
        auto_emit_task_webhooks=False,
        validate_at_init=False,
    )
    try:
        yield handler
    finally:
        executor.shutdown(wait=True)


async def compose_activity(reliable, composite):
    n = NotificationHarness(reliable)
    outbox = n.outbox
    worker = worker_for(n, outbox)
    status = None
    reader = outbox
    if composite:
        from adcp.reporting.outbox.status_activity_pg import PgReportingActivityUnionStore
        from adcp.reporting.outbox.status_pg import PgStatusNotificationStore

        store = PgStatusNotificationStore(reliable.store)
        await store.create_schema()
        c_worker = ReportingNotificationWorker(
            outbox=store.outbox,
            activity=store.outbox,
            subscriptions=n.subscriptions,
            signing=n.signing,
            cipher=n.cipher,
        )
        status = ReportingStatusSupport(store, c_worker, scheduled=True)
        reader = PgReportingActivityUnionStore(outbox, store.outbox)
    return ReportingActivitySupport(
        worker, reliable.store, ReportingActivityProjector(reader), status
    )


class CatalogAccounting:
    """Instrument real psycopg execution and acquisition, never replace a validator."""

    def __init__(self, monkeypatch, pool):
        from psycopg import AsyncConnection

        self.checkouts = 0
        self.scans = 0
        self.catalog_queries = 0
        self.pause = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        connection = pool.connection
        execute = AsyncConnection.execute

        @asynccontextmanager
        async def checkout(*args, **kwargs):
            async with connection(*args, **kwargs) as conn:
                self.checkouts += 1
                yield conn

        async def query(conn, command, *args, **kwargs):
            if isinstance(command, str):
                if command.startswith("SELECT c.oid, c.relname, c.relkind"):
                    self.scans += 1
                if any(name in command for name in ("pg_class", "pg_attribute", "pg_proc")):
                    self.catalog_queries += 1
            result = await execute(conn, command, *args, **kwargs)
            # Pause after the final catalog result is captured. An invalidated
            # old scan can still be positive, so epoch publication is exercised.
            if self.pause and isinstance(command, str) and "FROM pg_proc p" in command:
                self.pause = False
                self.entered.set()
                await self.release.wait()
            return result

        monkeypatch.setattr(pool, "connection", checkout)
        monkeypatch.setattr(AsyncConnection, "execute", query)


@pytest.fixture(params=[False, True], ids=["b", "b+c"])
async def activity_proof(request, monkeypatch):
    async with reliable_factory("postgres", notifications=True, autocommit=True) as reliable:
        support = await compose_activity(reliable, request.param)
        accounting = CatalogAccounting(monkeypatch, reliable.blobs.pool)
        with mounted_activity(support) as handler:
            yield support, handler, accounting, reliable.blobs.pool


def assert_primitive_cache(support):
    def primitive(value):
        if type(value) in (str, int, bool, type(None)):
            return True
        return type(value) is tuple and all(primitive(v) for v in value)

    assert all(primitive(value) for value in vars(support._schema_validation).values())


async def test_startup_then_sequential_discovery_checks_catalog_and_pool_once(activity_proof):
    support, handler, accounting, _ = activity_proof
    await validate_capabilities_response_shape_async(handler)
    queries = accounting.catalog_queries
    for _ in range(8):
        response = await handler.get_adcp_capabilities()
        assert response["media_buy"]["reporting_delivery"]["supports_webhook_activity"] is True
    assert handler._platform.calls == 9
    assert (accounting.scans, accounting.checkouts) == (1, 1)
    assert accounting.catalog_queries == queries and queries > 10
    assert_primitive_cache(support)


async def test_concurrent_cold_discovery_single_flights_real_catalog(activity_proof):
    support, handler, accounting, _ = activity_proof
    responses = await asyncio.gather(*(handler.get_adcp_capabilities() for _ in range(12)))
    assert len(responses) == handler._platform.calls == 12
    assert (accounting.scans, accounting.checkouts) == (1, 1)
    assert_primitive_cache(support)


async def test_request_false_claim_does_not_warm_schema_proof(activity_proof):
    support, handler, accounting, _ = activity_proof
    handler._platform.claim = False
    for _ in range(3):
        await handler.get_adcp_capabilities()
    assert (accounting.scans, accounting.checkouts) == (0, 0)
    handler._platform.claim = True
    await handler.get_adcp_capabilities()
    handler._platform.claim = False
    await handler.get_adcp_capabilities()
    handler._platform.claim = True
    await handler.get_adcp_capabilities()
    assert (accounting.scans, accounting.checkouts) == (1, 1)
    assert_primitive_cache(support)


async def test_warm_core_discovery_does_not_queue_behind_saturated_pool(activity_proof):
    _, handler, accounting, pool = activity_proof
    await validate_capabilities_response_shape_async(handler)
    async with AsyncExitStack() as stack:
        for _ in range(pool.max_size):
            await stack.enter_async_context(pool.connection())
        acquired = accounting.checkouts
        await asyncio.wait_for(handler.get_adcp_capabilities(), timeout=1)
        assert accounting.checkouts == acquired
    assert accounting.scans == 1


async def test_failed_schema_is_retried_after_repair(activity_proof):
    support, handler, accounting, pool = activity_proof
    async with pool.connection() as connection:
        await connection.execute(
            "ALTER TABLE reporting_webhook_attempts DISABLE TRIGGER reporting_webhook_attempt_guard"
        )
    for _ in range(2):
        with pytest.raises(ReportingNotificationError, match="notification_schema_unready"):
            await handler.get_adcp_capabilities()
        assert_primitive_cache(support)
    assert accounting.scans == 2
    async with pool.connection() as connection:
        await connection.execute(
            "ALTER TABLE reporting_webhook_attempts ENABLE TRIGGER reporting_webhook_attempt_guard"
        )
    await handler.get_adcp_capabilities()
    await handler.get_adcp_capabilities()
    assert accounting.scans == 3


async def test_explicit_invalidation_rechecks_broken_required_object(activity_proof):
    support, handler, accounting, pool = activity_proof
    await handler.get_adcp_capabilities()
    support.invalidate_schema_validation()
    async with pool.connection() as connection:
        await connection.execute(
            "ALTER TABLE reporting_webhook_attempts DISABLE TRIGGER reporting_webhook_attempt_guard"
        )
    with pytest.raises(ReportingNotificationError, match="notification_schema_unready"):
        await handler.get_adcp_capabilities()
    assert accounting.scans == 2
    assert_primitive_cache(support)


async def test_old_positive_scan_cannot_publish_after_epoch_invalidation(activity_proof):
    support, handler, accounting, pool = activity_proof
    accounting.pause = True
    old = asyncio.create_task(handler.get_adcp_capabilities())
    try:
        await asyncio.wait_for(accounting.entered.wait(), timeout=10)
        support.invalidate_schema_validation()
        async with pool.connection() as connection:
            await connection.execute(
                "ALTER TABLE reporting_webhook_attempts DISABLE TRIGGER"
                " reporting_webhook_attempt_guard"
            )
        accounting.release.set()
        with pytest.raises(ReportingNotificationError):
            await old
        assert_primitive_cache(support)
        with pytest.raises(ReportingNotificationError, match="notification_schema_unready"):
            await handler.get_adcp_capabilities()
        assert accounting.scans == 2
    finally:
        accounting.release.set()
        await asyncio.gather(old, return_exceptions=True)


@pytest.mark.parametrize("mutation", ["recorder", "reader", "pool", "notifications", "listing"])
async def test_warm_proof_does_not_bypass_dynamic_topology(activity_proof, mutation):
    support, handler, accounting, _ = activity_proof
    handler._platform.claim_account = True
    await handler.get_adcp_capabilities()
    if mutation == "recorder":
        support.worker.activity = None
    elif mutation == "reader":
        handler._account_activity = ReportingActivityProjector(support.projector.store)
    elif mutation == "pool":
        support.worker.outbox._pool = object()
    elif mutation == "notifications":
        support.ledger._notifications_enabled = False
    else:
        handler._platform.accounts.list = None
    with pytest.raises(ReportingNotificationError):
        await handler.get_adcp_capabilities()
    assert accounting.scans == 1


async def test_equal_frozen_support_instances_have_independent_proof(activity_proof):
    support, handler, accounting, _ = activity_proof
    equal = replace(support)
    assert equal == support
    with pytest.raises(FrozenInstanceError):
        support.projector = None
    await handler.get_adcp_capabilities()
    with mounted_activity(equal) as other:
        await other.get_adcp_capabilities()
        await other.get_adcp_capabilities()
    assert equal == support
    assert (accounting.scans, accounting.checkouts) == (2, 2)
    assert equal._schema_validation is not support._schema_validation


async def test_synchronous_startup_then_runtime_loop_has_no_loop_bound_cache(activity_proof):
    support, handler, accounting, _ = activity_proof
    # The supported synchronous validator really calls asyncio.run in a
    # separate thread. The runtime loop continues to own the live PG pool.
    with ThreadPoolExecutor(max_workers=1) as startup:
        await asyncio.wrap_future(startup.submit(validate_capabilities_response_shape, handler))
    assert_primitive_cache(support)
    await handler.get_adcp_capabilities()
    assert (accounting.scans, accounting.checkouts) == (1, 1)


async def test_memory_only_claims_and_frozen_constructor_remain_unchanged():
    async with reliable_factory("memory", notifications=True) as reliable:
        support = await compose_activity(reliable, False)
        assert not await support.durable()
        assert not await support.durable()
        with mounted_activity(support) as handler:
            handler._platform.claim = False
            await handler.get_adcp_capabilities()
            handler._platform.claim = True
            with pytest.raises(ReportingNotificationError, match="requires_durable_reporting"):
                await handler.get_adcp_capabilities()


async def test_cancelled_waiter_leaves_other_cold_callers_single_flight(activity_proof):
    support, handler, accounting, _ = activity_proof
    accounting.pause = True
    first = asyncio.create_task(handler.get_adcp_capabilities())
    second = asyncio.create_task(handler.get_adcp_capabilities())
    try:
        await asyncio.wait_for(accounting.entered.wait(), 10)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert support._schema_validation.positive is None
        accounting.release.set()
        await second
        assert (accounting.scans, accounting.checkouts) == (1, 1)
        assert_primitive_cache(support)
    finally:
        accounting.release.set()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_cancelled_scan_and_driver_failure_never_become_positive(activity_proof, monkeypatch):
    from psycopg import AsyncConnection, OperationalError

    support, handler, accounting, _ = activity_proof
    accounting.pause = True
    call = asyncio.create_task(handler.get_adcp_capabilities())
    try:
        await asyncio.wait_for(accounting.entered.wait(), 10)
        support._schema_validation.flight.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
    finally:
        accounting.release.set()
        await asyncio.gather(call, return_exceptions=True)
    assert support._schema_validation.positive is None
    assert_primitive_cache(support)
    original = AsyncConnection.execute
    fired = []

    async def fail_after_catalog_query(connection, command, *args, **kwargs):
        result = await original(connection, command, *args, **kwargs)
        if isinstance(command, str) and command.startswith("SELECT c.oid, c.relname, c.relkind"):
            fired.append(True)
            raise OperationalError("not retained by schema validation")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(AsyncConnection, "execute", fail_after_catalog_query)
        with pytest.raises(ReportingNotificationError, match="catalog_unavailable"):
            await handler.get_adcp_capabilities()
    assert fired == [True] and support._schema_validation.positive is None
    assert_primitive_cache(support)
    await handler.get_adcp_capabilities()
    await handler.get_adcp_capabilities()
    assert (accounting.scans, accounting.checkouts) == (3, 3)


@pytest.mark.parametrize("mutation", ["status-pool", "status-clock", "status-writer", "scheduling"])
async def test_composite_warm_proof_rechecks_closed_union_wiring(monkeypatch, mutation):
    async with reliable_factory("postgres", notifications=True, autocommit=True) as reliable:
        support = await compose_activity(reliable, True)
        accounting = CatalogAccounting(monkeypatch, reliable.blobs.pool)
        with mounted_activity(support) as handler:
            await handler.get_adcp_capabilities()
            if mutation == "status-pool":
                support.status.store.outbox._pool = object()
            elif mutation == "status-clock":
                support.status.store.outbox._clock = object()
            elif mutation == "status-writer":
                support.status.worker.activity = None
            else:
                # The public dataclass remains frozen; adversarial private
                # mutation must still not turn an old proof into scheduling.
                object.__setattr__(support.status, "scheduled", False)
            with pytest.raises(ReportingNotificationError):
                await handler.get_adcp_capabilities()
        assert accounting.scans == 1


async def test_separate_search_paths_and_instances_do_not_share_positive_proof(monkeypatch):
    async with (
        reliable_factory("postgres", notifications=True, autocommit=True) as first,
        reliable_factory("postgres", notifications=True, autocommit=True) as second,
    ):
        one = await compose_activity(first, False)
        two = await compose_activity(second, False)
        assert first.blobs.pool.conninfo == second.blobs.pool.conninfo
        assert first.blobs.pool.kwargs["options"] != second.blobs.pool.kwargs["options"]
        accounting = CatalogAccounting(monkeypatch, first.blobs.pool)
        with mounted_activity(one) as a, mounted_activity(two) as b:
            await a.get_adcp_capabilities()
            async with second.blobs.pool.connection() as connection:
                await connection.execute(
                    "ALTER TABLE reporting_webhook_attempts DISABLE TRIGGER"
                    " reporting_webhook_attempt_guard"
                )
            with pytest.raises(ReportingNotificationError, match="notification_schema_unready"):
                await b.get_adcp_capabilities()
            async with second.blobs.pool.connection() as connection:
                await connection.execute(
                    "ALTER TABLE reporting_webhook_attempts ENABLE TRIGGER"
                    " reporting_webhook_attempt_guard"
                )
            await b.get_adcp_capabilities()
            await a.get_adcp_capabilities()
            await b.get_adcp_capabilities()
        assert accounting.scans == 3 and accounting.checkouts == 1


async def test_b_and_composite_prove_each_packaged_contract_once(monkeypatch):
    from adcp.reporting.outbox import _schema, status_schema

    async with reliable_factory("postgres", notifications=True, autocommit=True) as reliable:
        composite = await compose_activity(reliable, True)
        b_only = ReportingActivitySupport(
            composite.worker,
            composite.ledger,
            ReportingActivityProjector(composite.worker.outbox),
        )
        accounting = CatalogAccounting(monkeypatch, reliable.blobs.pool)
        calls = []
        b_validator, c_validator = (
            _schema._validate_schema_objects,
            status_schema._validate_status_objects,
        )

        def b_contract(installed, **options):
            calls.append(("B", options))
            b_validator(installed, **options)

        def c_contract(installed, **options):
            calls.append(("C", options))
            c_validator(installed, **options)

        monkeypatch.setattr(_schema, "_validate_schema_objects", b_contract)
        monkeypatch.setattr(status_schema, "_validate_status_objects", c_contract)
        for support in (b_only, composite):
            assert await support.durable()
            assert await support.durable()
        assert calls == [
            ("B", {"activity": True}),
            ("B", {"activity": True}),
            ("C", {"activity": True, "status": False}),
        ]
        assert (accounting.scans, accounting.checkouts) == (2, 2)
        assert b_only._schema_validation.positive != composite._schema_validation.positive
        async with reliable.blobs.pool.connection() as connection:
            await connection.execute(
                "ALTER TABLE reporting_status_webhook_attempts DISABLE TRIGGER"
                " reporting_status_webhook_attempt_guard"
            )
        assert await b_only.durable()
        # A new server instance is the normal migration/restart boundary.
        restarted = replace(composite)
        with pytest.raises(ReportingNotificationError, match="status_schema_unready"):
            await restarted.durable()
        assert accounting.scans == 3


async def test_positive_proof_is_bound_to_packaged_manifest_contract(activity_proof, monkeypatch):
    from adcp.reporting.outbox import _schema

    support, handler, accounting, _ = activity_proof
    await handler.get_adcp_capabilities()
    key = "table:reporting_webhook_attempts"
    with monkeypatch.context() as patch:
        patch.setitem(_schema.REQUIRED_OBJECTS, key, {"fingerprint": "0" * 64, "enabled": True})
        with pytest.raises(ReportingNotificationError, match="notification_schema_unready:changed"):
            await handler.get_adcp_capabilities()
    assert accounting.scans == 2
    assert_primitive_cache(support)
