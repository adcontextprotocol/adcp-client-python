"""The managed seller path: ``ReliableReportingService(subscriptions=SubscriptionOptions(...))``.

The block's presence enables reporting subscriptions. The service owns the
principal runtime, serves ``get_principal``/``sync_principal`` from the
installed platform, derives the roster from ``list_accounts`` and the buys from
``get_media_buys`` under the principal's identity, reconciles on served writes,
and sweeps period boundaries in each worker turn.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from adcp.reporting import PrincipalRef, SubscriptionOptions
from adcp.reporting.fixtures import SNAPSHOT_OFFERING_ID, redacted_capabilities
from adcp.reporting.ledger import ReportingConfiguration
from adcp.reporting.service import (
    ReliableReportingConfigurationError,
    ReliableReportingService,
    ReportingAccountContext,
)
from adcp.reporting.subscriptions import AccountDeliveryConfig
from adcp.reporting.testing import DeterministicReportingClock
from adcp.server import ADCPHandler
from adcp.server.base import AccountAwareToolContext
from adcp.server.mcp_tools import get_tools_for_handler
from tests.test_reliable_reporting_service import DEFINITION, _capability_offering
from tests.test_reporting_subscription_service import _ByBuySource

START = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
ACCOUNT = "account-redacted"
AGENCY = "https://agency.example/agent"
OTHER = "https://other.example/agent"
OFFERING = {**_capability_offering("gam"), "standard_dimensions": ["package"]}
SUBSCRIPTION = {
    "subscription_id": "analytics-hourly",
    "subscription_version": 1,
    "active": True,
    "scope": {"all_authorized_accounts": True},
    "selector": {
        "feed_purpose": "analytics",
        "period_duration": "PT1H",
        "required_finality": "snapshot",
        "dimensions": ["package"],
    },
    "reconciliation_mode": "delivery_only",
}


def _context(configuration: ReportingConfiguration) -> ReportingAccountContext:
    return ReportingAccountContext(
        account_id=configuration.account_id,
        adapter="gam",
        currency="USD",
        account_timezone="UTC",
        source_scope=redacted_capabilities().source_scope,
        snapshot_offering_id=SNAPSHOT_OFFERING_ID,
        requested_dimensions=("campaign_id",),
        capability_offering=OFFERING,
        publication_namespace="reporting-source:fixture",
    )


def _ctx(principal: str = AGENCY) -> AccountAwareToolContext:
    return AccountAwareToolContext(caller_identity=principal, account_id=ACCOUNT)


class Seller(ADCPHandler):
    """A seller that already serves list_accounts and get_media_buys."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, Any]] = {
            ACCOUNT: {
                "account_id": ACCOUNT,
                "name": "Redacted",
                "status": "active",
                "timezone": "UTC",
                "operator": "agency.example",
                "authorization": {
                    "allowed_tasks": ["get_reporting_status", "get_media_buys"],
                    "authorized_at": "2026-10-01T00:00:00Z",
                },
            }
        }
        self.buys: list[dict[str, Any]] = [
            {
                "media_buy_id": "media-buy-a1",
                "status": "active",
                "confirmed_at": "2026-11-01T01:00:00Z",
                "start_time": "2026-11-01T01:00:00Z",
                "end_time": "2026-11-01T03:00:00Z",
                "packages": [{"package_id": "pkg-1", "product_id": "prod-1"}],
            }
        ]
        self.listings: list[Any] = []
        self.buy_reads: list[Any] = []
        self.fail_listing = False

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> Any:
        return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

    async def list_accounts(self, params: Any, context: Any = None) -> Any:
        self.listings.append(context)
        if self.fail_listing:
            raise RuntimeError("the upstream token is only available inside a request")
        return {"accounts": list(self.accounts.values())}

    async def get_media_buys(self, params: Any, context: Any = None) -> Any:
        self.buy_reads.append((params, context))
        return {"media_buys": list(self.buys)}

    async def create_media_buy(self, params: Any, context: Any = None) -> Any:
        return {"media_buy_id": "media-buy-new", "status": "active", "packages": []}


class _World:
    def __init__(self, **options: Any) -> None:
        self.clock = DeterministicReportingClock(START)
        self.failures: list[Any] = []
        self.source = _ByBuySource()
        options.setdefault("definitions", {OFFERING["offering_id"]: DEFINITION})
        options.setdefault("planning_error_handler", self.failures.append)
        self.service = ReliableReportingService.memory(
            account_context=_context,
            clock=self.clock,
            read_jitter_window=timedelta(0),
            subscriptions=SubscriptionOptions(max_accounts=10, **options),
        )
        self.service.sources.register("gam", self.source, capability_offerings=[OFFERING])
        self.seller = Seller()
        self.platform = self.service.install(self.seller)
        self._key = 0

    async def subscribe(self, principal: str = AGENCY, **changes: Any) -> Any:
        self._key += 1
        response = await self.platform.sync_principal(
            {
                "idempotency_key": f"managed-test-key-{self._key:06d}",
                "configuration": {"reporting_subscriptions": [{**SUBSCRIPTION, **changes}]},
            },
            _ctx(principal),
        )
        assert response.result.kind == "applied", response
        await self.service.drain_subscription_triggers()
        return response


@pytest.mark.asyncio
async def test_the_block_alone_serves_subscriptions_end_to_end() -> None:
    world = _World()
    tools = {item["name"] for item in get_tools_for_handler(world.platform, _include_schemas=False)}
    assert {"get_principal", "sync_principal", "get_reporting_status"} <= tools

    await world.subscribe()
    # sync_principal reconciled at once: the roster came from list_accounts,
    # read under the principal's identity.
    assert [context.caller_identity for context in world.seller.listings] == [AGENCY]
    principal = PrincipalRef(None, AGENCY)
    assert await world.service._managed.engine.account_ids(principal) == {ACCOUNT}

    read = await world.platform.get_principal({}, _ctx())
    assert read.result.configuration.reporting_subscriptions[0].subscription_id == (
        "analytics-hourly"
    )

    world.clock.set(START + timedelta(hours=3, minutes=30))
    turn = await world.service.run_worker()
    assert turn.subscription_errors == {}
    # The boundary sweep reconciled before planning; both ended hours are owed.
    assert turn.subscription_obligations_written == 2
    assert len(world.seller.listings) == 2
    status = await world.platform.get_reporting_status({"view": "periods"}, _ctx())
    assert status["subscription_coverage"][0]["subscription_id"] == "analytics-hourly"
    assert {tuple(p["media_buy_ids"]) for p in status["periods"]} == {("media-buy-a1",)}


@pytest.mark.asyncio
async def test_capabilities_advertise_the_principal_layer_the_service_serves() -> None:
    world = _World()
    capabilities = await world.platform.get_adcp_capabilities({}, _ctx())
    principal = capabilities["adcp"]["principal"]
    assert principal["sync_task"] == "sync_principal" and principal["read_task"] == "get_principal"
    assert {"reporting_destinations", "reporting_subscriptions"} <= set(
        principal["supported_sections"]
    )
    assert {"protocol.principal", "media_buy.reporting_subscriptions"} <= set(
        capabilities["experimental_features"]
    )
    block = capabilities["media_buy"]["reporting_delivery"]
    assert block["subscriptions"] == {
        "supported": True,
        "max_accounts": 10,
        "dimensions": ["package"],
    }
    assert block["configuration_tasks"] == ["sync_principal", "sync_accounts"]


def test_every_offering_must_publish_standard_dimensions() -> None:
    service = ReliableReportingService.memory(
        account_context=_context, subscriptions=SubscriptionOptions()
    )
    service.sources.register(
        "gam", _ByBuySource(), capability_offerings=[_capability_offering("gam")]
    )
    with pytest.raises(ReliableReportingConfigurationError) as refused:
        service.install(Seller())
    error = refused.value
    assert error.code == "SUBSCRIPTIONS_REQUIRE_STANDARD_DIMENSIONS"
    assert error.recovery == "correctable" and error.dispatched is None
    assert "capability_offerings" in str(error)


def test_a_platform_without_a_roster_is_refused_at_install() -> None:
    class NoRoster(ADCPHandler):
        async def get_media_buys(self, params: Any, context: Any = None) -> Any:
            return {"media_buys": []}

    service = ReliableReportingService.memory(
        account_context=_context, subscriptions=SubscriptionOptions()
    )
    service.sources.register("gam", _ByBuySource(), capability_offerings=[OFFERING])
    with pytest.raises(ReliableReportingConfigurationError) as refused:
        service.install(NoRoster())
    assert refused.value.code == "SUBSCRIPTION_ROSTER_UNAVAILABLE"
    assert "SubscriptionOptions.accounts" in str(refused.value)


@pytest.mark.asyncio
async def test_a_failed_listing_is_reported_and_never_revokes() -> None:
    world = _World()
    await world.subscribe()
    principal = PrincipalRef(None, AGENCY)
    engine = world.service._managed.engine
    assert await engine.grants_task(principal, ACCOUNT, "get_reporting_status")

    world.seller.fail_listing = True
    world.clock.set(START + timedelta(hours=1, minutes=5))
    turn = await world.service.run_worker()
    [error] = turn.subscription_errors.values()
    assert isinstance(error, ReliableReportingConfigurationError)
    assert error.code == "SUBSCRIPTION_ROSTER_UNAVAILABLE"
    assert "SubscriptionOptions.accounts" in str(error)
    assert [failure.principal for failure in world.failures] == [principal]
    # The account was not read as revoked.
    assert await engine.grants_task(principal, ACCOUNT, "get_reporting_status")


@pytest.mark.asyncio
async def test_the_sweep_reconciles_only_after_a_boundary() -> None:
    world = _World()
    await world.subscribe()
    assert len(world.seller.listings) == 1
    world.clock.set(START + timedelta(minutes=30))
    await world.service.run_worker()
    assert len(world.seller.listings) == 1  # no boundary since the last reconcile
    world.clock.set(START + timedelta(hours=1))
    await world.service.run_worker()
    assert len(world.seller.listings) == 2
    await world.service.run_worker()
    assert len(world.seller.listings) == 2


@pytest.mark.asyncio
async def test_served_writes_reconcile_the_principals_concerned() -> None:
    world = _World()
    await world.subscribe()
    await world.subscribe(OTHER)
    calls = len(world.seller.listings)
    await world.platform.create_media_buy({"account": {"account_id": ACCOUNT}}, _ctx())
    await world.service.drain_subscription_triggers()
    # Both principals hold state for the account.
    assert len(world.seller.listings) == calls + 2
    await world.service.touch(account_id="unknown-account")
    assert len(world.seller.listings) == calls + 2


@pytest.mark.asyncio
async def test_unchanged_sync_principal_does_not_reconcile() -> None:
    world = _World()
    await world.subscribe()
    response = await world.platform.sync_principal(
        {
            "idempotency_key": "managed-test-key-unchanged",
            "configuration": {"reporting_subscriptions": [SUBSCRIPTION]},
        },
        _ctx(),
    )
    assert response.result.action == "unchanged"
    await world.service.drain_subscription_triggers()
    assert len(world.seller.listings) == 1


@pytest.mark.asyncio
async def test_overrides_replace_each_derivation() -> None:
    from adcp.reporting.subscriptions import AuthorizedAccount, AuthorizedAccountPage

    seen: list[PrincipalRef] = []

    async def accounts(principal: PrincipalRef, *, cursor: str | None) -> AuthorizedAccountPage:
        seen.append(principal)
        return AuthorizedAccountPage(
            (AuthorizedAccount(ACCOUNT, timezone="UTC", authorized_at=START),)
        )

    world = _World(accounts=accounts, buys=lambda principal, account_id: [])
    world.seller.fail_listing = True
    await world.subscribe()
    assert seen == [PrincipalRef(None, AGENCY)]
    assert world.seller.listings == [] and world.seller.buy_reads == []


@pytest.mark.asyncio
async def test_derived_buys_use_buy_offerings_per_package() -> None:
    world = _World(buy_offerings=lambda buy: {"pkg-1": ["SOME_OTHER_OFFERING"]})
    await world.subscribe()
    world.clock.set(START + timedelta(hours=3, minutes=30))
    turn = await world.service.run_worker()
    # The package resolves no registered offering, so nothing is owed.
    assert turn.subscription_obligations_written == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "owed"),
    [
        # The snapshot names the registered offering: the package is reported.
        ([OFFERING["offering_id"]], True),
        # 3.3.0-beta.3: [] means no offering applies, never "every offering".
        ([], False),
        # The snapshot fixed at confirmation names another offering.
        (["SOME_OTHER_OFFERING"], False),
    ],
)
async def test_derived_buys_read_each_package_offering_snapshot(
    snapshot: list[str], owed: bool
) -> None:
    world = _World()
    world.seller.buys[0]["packages"][0]["reporting_delivery_offering_ids"] = snapshot
    await world.subscribe()
    world.clock.set(START + timedelta(hours=3, minutes=30))
    turn = await world.service.run_worker()
    assert (turn.subscription_obligations_written > 0) is owed


@pytest.mark.asyncio
async def test_a_package_without_a_snapshot_falls_back_to_every_registered_offering() -> None:
    world = _World()
    assert "reporting_delivery_offering_ids" not in world.seller.buys[0]["packages"][0]
    await world.subscribe()
    world.clock.set(START + timedelta(hours=3, minutes=30))
    turn = await world.service.run_worker()
    assert turn.subscription_obligations_written > 0


@pytest.mark.asyncio
async def test_replace_account_configs_is_refused_without_a_host_that_applies_it() -> None:
    world = _World()
    response = await world.platform.sync_principal(
        {
            "idempotency_key": "managed-test-key-replace",
            "configuration": {
                "reporting_subscriptions": [{**SUBSCRIPTION, "replace_account_configs": True}]
            },
        },
        _ctx(),
    )
    assert response.result.kind == "failed"
    assert [error.code for error in response.result.errors] == ["UNSUPPORTED_FEATURE"]


@pytest.mark.asyncio
async def test_replace_account_configs_applies_once_per_version_and_on_a_change_to_true() -> None:
    configs = {ACCOUNT: [AccountDeliveryConfig("analytics-daily", "analytics")]}
    ended: list[tuple[PrincipalRef, str, tuple[str, ...], datetime]] = []

    async def end(principal, account_id, ids, *, at):
        ended.append((principal, account_id, ids, at))

    world = _World(
        account_delivery_configs=lambda principal, account_id: configs.get(account_id, []),
        end_account_configs=end,
    )
    await world.subscribe(replace_account_configs=True)
    assert ended == [(PrincipalRef(None, AGENCY), ACCOUNT, ("analytics-daily",), START)]
    # Re-sending the same version and flag does nothing more.
    world.clock.set(START + timedelta(hours=1))
    await world.service.run_worker()
    assert len(ended) == 1
    # Clearing the flag, then setting it again, applies it again.
    await world.subscribe(replace_account_configs=False)
    await world.subscribe(replace_account_configs=True)
    assert len(ended) == 2
    # A new version applies it again.
    await world.subscribe(
        subscription_version=2,
        replace_account_configs=True,
        operator_domains=["agency.example"],
    )
    assert len(ended) == 3


def test_end_account_configs_needs_the_configurations_it_ends() -> None:
    async def end(principal, account_id, ids, *, at):
        return None

    with pytest.raises(ValueError, match="account_delivery_configs"):
        SubscriptionOptions(end_account_configs=end)


def test_managed_and_advanced_compositions_are_exclusive() -> None:
    from adcp.reporting.service_subscriptions import ReportingSubscriptionServiceOptions

    with pytest.raises(ReliableReportingConfigurationError, match="not both"):
        ReliableReportingService.memory(
            account_context=_context,
            subscriptions=SubscriptionOptions(),
            reporting_subscriptions=object.__new__(ReportingSubscriptionServiceOptions),
        )


@pytest.mark.asyncio
async def test_the_binding_keeps_no_credential_material() -> None:
    from adcp.decisioning import AuthInfo
    from adcp.decisioning.registry import ApiKeyCredential

    world = _World()
    context = _ctx()
    context.metadata["adcp.auth_info"] = AuthInfo(
        kind="api_key",
        key_id="kid-1",
        principal=AGENCY,
        credential=ApiKeyCredential(kind="api_key", key_id="kid-1"),
        extra={"session_secret": "do-not-store"},
    )
    await world.platform.sync_principal(
        {
            "idempotency_key": "managed-test-key-binding",
            "configuration": {"reporting_subscriptions": [SUBSCRIPTION]},
        },
        context,
    )
    await world.service.drain_subscription_triggers()
    binding = await world.service._managed.engine.binding(PrincipalRef(None, AGENCY))
    assert binding == {
        "tenant_id": None,
        "auth": {
            "kind": "api_key",
            "key_id": "kid-1",
            "principal": AGENCY,
            "credential": {"kind": "api_key", "key_id": "kid-1"},
        },
    }
    # The roster is read under the restored identity.
    restored = world.seller.listings[-1].metadata["adcp.auth_info"]
    assert (restored.principal, restored.credential.key_id) == (AGENCY, "kid-1")
    assert restored.extra == {}


# -- decisioning platform ------------------------------------------------------


class _DecisioningAccounts:
    resolution = "explicit"

    def __init__(self) -> None:
        self.listed: list[Any] = []

    def resolve(self, ref: Any, auth_info: Any = None) -> Any:
        from adcp.decisioning import Account

        return Account(id=ACCOUNT, timezone="UTC")

    def list(self, filter: Any, ctx: Any = None) -> Any:
        from adcp.decisioning import Account

        self.listed.append(ctx)
        return [
            Account(
                id=ACCOUNT,
                timezone="UTC",
                operator="agency.example",
                authorization={
                    "allowed_tasks": ["get_reporting_status", "get_media_buys"],
                    "authorized_at": "2026-10-01T00:00:00Z",
                },
            )
        ]


def _decisioning_platform() -> Any:
    from tests.test_lazy_platform_router import _SyncSalesPlatform

    class Platform(_SyncSalesPlatform):
        def __init__(self) -> None:
            super().__init__("managed")
            self.accounts = _DecisioningAccounts()

        def get_media_buys(self, req: Any, ctx: Any) -> dict[str, Any]:
            self.calls.append(("get_media_buys", ctx.account.id))
            return {"media_buys": list(Seller().buys)}

    return Platform()


@pytest.mark.asyncio
async def test_a_decisioning_platform_is_served_through_its_own_handler() -> None:
    from adcp.decisioning.serve import create_adcp_server_from_platform
    from adcp.server.auth import current_principal
    from adcp.server.base import ToolContext

    clock = DeterministicReportingClock(START)
    service = ReliableReportingService.memory(
        account_context=_context,
        clock=clock,
        read_jitter_window=timedelta(0),
        subscriptions=SubscriptionOptions(definitions={OFFERING["offering_id"]: DEFINITION}),
    )
    service.sources.register("gam", _ByBuySource(), capability_offerings=[OFFERING])
    platform = _decisioning_platform()
    installed = service.install(platform)
    handler, _executor, _ = create_adcp_server_from_platform(installed, validate_at_init=False)
    tools = {item["name"] for item in get_tools_for_handler(handler, _include_schemas=False)}
    assert {"get_principal", "sync_principal"} <= tools

    token = current_principal.set(AGENCY)
    try:
        context = ToolContext(metadata={"adcp.auth_info": {"kind": "bearer", "principal": AGENCY}})
        response = await handler.sync_principal(
            {
                "idempotency_key": "managed-decisioning-key-1",
                "configuration": {"reporting_subscriptions": [SUBSCRIPTION]},
            },
            context,
        )
        assert response.result.kind == "applied"
        await service.drain_subscription_triggers()
        # The roster was read through accounts.list under the restored identity.
        [listing] = platform.accounts.listed
        assert listing.auth_info.principal == AGENCY
        assert ("get_media_buys", ACCOUNT) in platform.calls

        clock.set(START + timedelta(hours=3, minutes=30))
        turn = await service.run_worker()
        assert turn.subscription_errors == {}
        assert turn.subscription_obligations_written == 2

        from adcp.types import CreateMediaBuyRequest

        await handler.create_media_buy(
            CreateMediaBuyRequest(
                account={"account_id": ACCOUNT},
                brand={"domain": "brand.example"},
                idempotency_key="managed-decisioning-buy-1",
                start_time="2026-11-02T00:00:00Z",
                end_time="2026-12-01T00:00:00Z",
                packages=[
                    {"buyer_ref": "pkg_1", "product_id": "prod_1", "pricing_option_id": "cpm"}
                ],
            ),
            ToolContext(metadata={"adcp.auth_info": {"kind": "bearer", "principal": AGENCY}}),
        )
        await service.drain_subscription_triggers()
        assert len(platform.accounts.listed) == 3
    finally:
        current_principal.reset(token)


@pytest.mark.asyncio
async def test_a_served_write_never_waits_for_its_reconcile() -> None:
    import asyncio

    world = _World()
    await world.subscribe()
    release = asyncio.Event()
    listed = world.seller.list_accounts

    async def slow(params: Any, context: Any = None) -> Any:
        await release.wait()
        return await listed(params, context)

    world.seller.list_accounts = slow  # type: ignore[method-assign]
    result = await asyncio.wait_for(
        world.platform.create_media_buy({"account": {"account_id": ACCOUNT}}, _ctx()), 1
    )
    assert result["media_buy_id"] == "media-buy-new"
    release.set()
    await world.service.drain_subscription_triggers()
    await world.service.close()


@pytest.mark.asyncio
async def test_a_hung_platform_read_is_bounded_by_read_timeout() -> None:
    import asyncio

    world = _World(read_timeout=timedelta(milliseconds=50))

    async def hung(params: Any, context: Any = None) -> Any:
        await asyncio.sleep(30)

    world.seller.list_accounts = hung  # type: ignore[method-assign]
    await world.subscribe()
    [failure] = world.failures
    assert failure.error.code == "SUBSCRIPTION_ROSTER_UNAVAILABLE"
    assert "RuntimeError" in str(failure.error)
