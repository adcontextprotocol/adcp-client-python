"""Reporting subscriptions served by ReliableReportingService (AdCP 3.3).

The service plans every listed principal's frozen owed sets each turn,
executes them with the account generations, advertises the subscription
capability fields at the served release and adds ``subscription_coverage[]``
to status reads. Without the option it is unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

from adcp.reporting.fixtures import SNAPSHOT_OFFERING_ID, redacted_capabilities
from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    ReportingConfiguration,
    ReportingStatusCaller,
)
from adcp.reporting.service import (
    ReliableReportingConfigurationError,
    ReliableReportingService,
    ReportingAccountContext,
)
from adcp.reporting.service_subscriptions import ReportingSubscriptionServiceOptions
from adcp.reporting.subscriptions import (
    AuthorizedAccount,
    ReportingSubscriptionEngine,
    ReportingSubscriptionSupport,
    SubscriptionBuy,
    SubscriptionOffering,
)
from adcp.reporting.testing import DeterministicReportingClock
from adcp.server import PrincipalIdentity, PrincipalService
from tests.test_reliable_reporting_service import (
    DEFINITION,
    _capability_offering,
    _configuration,
)
from tests.test_reporting_subscription_engine import Clock, Host

NOW = datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc)
ACCOUNT = "account-redacted"
AGENCY = PrincipalIdentity("https://agency.example/agent", "buyer_agent")
OTHER = PrincipalIdentity("https://other.example/agent", "buyer_agent")
STRANGER = PrincipalIdentity("https://stranger.example/agent", "buyer_agent")
OFFERING_ID = "GAM_SNAPSHOT_V1"
HOURLY = SubscriptionOffering(
    offering_id=OFFERING_ID,
    report_definition_id="PAID_MEDIA_DAILY_V1",
    reporting_profile="paid_media_delivery",
    feed_purpose="analytics",
    period_duration="PT1H",
    alignment="utc",
    delivery_sla="PT10M",
    supported_finality=frozenset({"snapshot"}),
    reconciliation_mode="delivery_only",
    dimensions=frozenset({"package"}),
)
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


class _ByBuySource:
    """Answers every slice with one row per requested media buy."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    @property
    def capabilities(self):
        return redacted_capabilities()

    def fetch_slice(self, request):
        self.calls.append(request)
        return [
            {
                "media_buy_id": item.media_buy_id,
                "campaign_id": "campaign-redacted-1",
                "impressions": 10,
                "spend": "1.25",
            }
            for item in request.coverage.constituents
        ]


def _context(configuration: ReportingConfiguration) -> ReportingAccountContext:
    return ReportingAccountContext(
        account_id=configuration.account_id,
        adapter="gam",
        currency="USD",
        account_timezone="UTC",
        source_scope=redacted_capabilities().source_scope,
        snapshot_offering_id=SNAPSHOT_OFFERING_ID,
        requested_dimensions=("campaign_id",),
        capability_offering=_capability_offering("gam"),
        publication_namespace="reporting-source:fixture",
    )


def _buy(media_buy_id: str, start: str, end: str) -> SubscriptionBuy:
    def at(text: str) -> datetime:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))

    return SubscriptionBuy(
        media_buy_id=media_buy_id,
        confirmed_at=at(start),
        start_time=at(start),
        end_time=at(end),
        reporting_delivery_offering_ids=(OFFERING_ID,),
    )


class _Seller:
    def __init__(
        self,
        *,
        principals=(AGENCY, OTHER),
        service_kwargs: dict[str, Any] | None = None,
        **options: Any,
    ) -> None:
        self.clock = DeterministicReportingClock(NOW)
        # Subscriptions are accepted before the buys run; the engine owes
        # nothing before acceptance.
        self.engine_clock = Clock("2026-11-01T00:00:00Z")
        self.host = Host()
        self.principals = PrincipalService()
        self.engine = ReportingSubscriptionEngine(
            principals=self.principals,
            host=self.host,
            offerings=[HOURLY],
            support=ReportingSubscriptionSupport(dimensions=("package",)),
            clock=self.engine_clock,
        )
        self.principals.set_reporting_subscriptions(self.engine.principal_options())
        self.listed = list(principals)
        self.caller = AGENCY
        self.source = _ByBuySource()
        self.options = ReportingSubscriptionServiceOptions(
            engine=self.engine,
            list_principals=lambda: self.listed,
            resolve_principal=lambda _request, _context: self.caller,
            definitions={OFFERING_ID: DEFINITION},
            **options,
        )
        self.service = ReliableReportingService.memory(
            account_context=_context,
            caller_resolver=lambda _request, _context: ReportingStatusCaller(
                ACCOUNT, self.caller.subject
            ),
            clock=self.clock,
            read_jitter_window=timedelta(0),
            reporting_subscriptions=self.options,
            **(service_kwargs or {}),
        )
        self.service.sources.register("gam", self.source)
        self._key = 0

    async def subscribe(self, identity: PrincipalIdentity, buys, allowed_tasks=None) -> None:
        self.engine_clock.set("2026-11-01T00:00:00Z")
        self.host.accounts[ACCOUNT] = AuthorizedAccount(
            ACCOUNT,
            timezone="UTC",
            authorized_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
            allowed_tasks=allowed_tasks,
        )
        self.host.buys[ACCOUNT] = list(buys)
        self._key += 1
        response = await self.principals.sync_principal(
            identity,
            {
                "idempotency_key": f"service-test-key-{self._key:06d}",
                "configuration": {"reporting_subscriptions": [SUBSCRIPTION]},
            },
        )
        assert response.result.kind == "applied", response
        await self.engine.reconcile(identity)
        self.engine_clock.set("2026-11-01T03:30:00Z")

    async def status(self, identity: PrincipalIdentity, view: str = "periods", context=None):
        self.caller = identity
        return await self.service.get_reporting_status({"view": view}, context)


class _Buys:
    """Each principal sees only its own buys on the shared account."""

    def __init__(self, seller: _Seller, by_principal: dict[str, list[SubscriptionBuy]]) -> None:
        self.by_principal = by_principal
        seller.host.account_buys = self.account_buys  # type: ignore[method-assign]

    async def account_buys(self, principal, account_id):
        return list(self.by_principal.get(principal.subject, ()))


@pytest.mark.asyncio
async def test_a_turn_plans_and_executes_each_principals_owed_set() -> None:
    seller = _Seller()
    mine = [
        _buy("media-buy-a1", "2026-11-01T01:00:00Z", "2026-11-01T03:00:00Z"),
        _buy("media-buy-a2", "2026-11-01T02:00:00Z", "2026-11-01T03:00:00Z"),
    ]
    theirs = [_buy("media-buy-b1", "2026-11-01T02:00:00Z", "2026-11-01T03:00:00Z")]
    await seller.subscribe(AGENCY, mine)
    await seller.subscribe(OTHER, theirs)
    _Buys(seller, {AGENCY.subject: mine, OTHER.subject: theirs})

    turn = await seller.service.run_worker(now=NOW)

    assert turn.subscription_errors == {}
    assert turn.subscription_obligations_written == 3
    # Each slice carries exactly that period's frozen media buys.
    requested = sorted(
        (call.period.start.isoformat(), tuple(c.media_buy_id for c in call.coverage.constituents))
        for call in seller.source.calls
    )
    assert requested == [
        ("2026-11-01T01:00:00+00:00", ("media-buy-a1",)),
        ("2026-11-01T02:00:00+00:00", ("media-buy-a1", "media-buy-a2")),
        ("2026-11-01T02:00:00+00:00", ("media-buy-b1",)),
    ]
    assert sum(len(item.revisions_committed) for item in turn.configurations.values()) == 3

    # Per-principal visibility: one wire generation, its own periods only.
    mine_view = await seller.status(AGENCY)
    assert [p["offering_id"] for p in mine_view["periods"]] == [OFFERING_ID, OFFERING_ID]
    assert {b for p in mine_view["periods"] for b in p["media_buy_ids"]} == {
        "media-buy-a1",
        "media-buy-a2",
    }
    assert mine_view["scope"]["delivery_config_generations"] == [
        {
            "delivery_config_id": "analytics-hourly",
            "delivery_config_version": 1,
            "feed_purpose": "analytics",
        }
    ]
    assert mine_view["subscription_coverage"][0]["subscription_id"] == "analytics-hourly"
    theirs_view = await seller.status(OTHER)
    assert [p["media_buy_ids"] for p in theirs_view["periods"]] == [["media-buy-b1"]]
    stranger = await seller.status(STRANGER)
    assert stranger["periods"] == []
    assert "subscription_coverage" not in stranger

    # A second turn writes and reads nothing new.
    again = await seller.service.run_worker(now=NOW)
    assert again.subscription_obligations_written == 0
    assert len(seller.source.calls) == 3


@pytest.mark.asyncio
async def test_the_horizon_never_passes_the_turn_clock() -> None:
    seller = _Seller(principals=(AGENCY,))
    await seller.subscribe(
        AGENCY, [_buy("media-buy-a1", "2026-11-01T01:00:00Z", "2026-11-01T05:00:00Z")]
    )
    turn = await seller.service.run_worker(now=NOW)
    # Periods ending at 02:00 and 03:00 are frozen; 03:00-04:00 has not ended.
    assert turn.subscription_obligations_written == 2


@pytest.mark.asyncio
async def test_one_failing_subscription_never_stops_the_others() -> None:
    failures: list[Any] = []
    seller = _Seller(planning_error_handler=failures.append)
    mine = [_buy("media-buy-a1", "2026-11-01T02:00:00Z", "2026-11-01T03:00:00Z")]
    theirs = [_buy("media-buy-b1", "2026-11-01T02:00:00Z", "2026-11-01T03:00:00Z")]
    await seller.subscribe(AGENCY, mine)
    await seller.subscribe(OTHER, theirs)
    _Buys(seller, {AGENCY.subject: mine, OTHER.subject: theirs})
    original = seller.engine.frozen_owed_sets

    async def frozen_owed_sets(identity, **kwargs):
        if identity == AGENCY:
            raise RuntimeError("engine unavailable for this principal")
        return await original(identity, **kwargs)

    seller.engine.frozen_owed_sets = frozen_owed_sets  # type: ignore[method-assign]
    # An account configuration on the same service keeps running too.
    await seller.service.configure(_configuration())
    turn = await seller.service.run_worker(now=NOW)

    assert list(turn.subscription_errors) == [(AGENCY.subject, None, None)]
    assert [failure.principal for failure in failures] == [AGENCY]
    assert turn.subscription_obligations_written == 1
    assert {c.identity.delivery_config_id for c in seller.source.calls} == {
        "analytics-hourly",
        "gam-delivery",
    }


def _capabilities() -> dict[str, Any]:
    return {
        "adcp": {"major_versions": [3], "principal": {"supported_sections": ["destinations"]}},
        "supported_protocols": ["media_buy"],
    }


@pytest.mark.asyncio
async def test_capabilities_follow_the_served_release() -> None:
    seller = _Seller()
    await seller.service.configure(_configuration())
    beta_1 = seller.service.inject_capabilities(_capabilities(), adcp_version="3.3-beta.1")
    block = beta_1["media_buy"]["reporting_delivery"]
    assert block["configuration_tasks"] == ["sync_principal", "sync_accounts"]
    assert block["configuration_task"] == "sync_accounts"
    assert block["subscriptions"] == {"supported": True, "dimensions": ["package"]}
    # adcp#8585 offering fields reach only 3.3.0-beta.2 callers.
    assert "standard_dimensions" not in block["offerings"][0]
    assert "media_buy.reporting_subscriptions" in beta_1["experimental_features"]
    assert "reporting_subscriptions" in beta_1["adcp"]["principal"]["supported_sections"]
    beta_2 = seller.service.inject_capabilities(_capabilities(), adcp_version="3.3-beta.2")
    assert beta_2["media_buy"]["reporting_delivery"]["offerings"][0]["standard_dimensions"] == [
        "package"
    ]
    # A 3.2 caller sees the pre-3.3 block.
    older = seller.service.inject_capabilities(_capabilities(), adcp_version="3.2")
    assert "configuration_tasks" not in older["media_buy"]["reporting_delivery"]
    assert "subscriptions" not in older["media_buy"]["reporting_delivery"]
    assert "media_buy.reporting_subscriptions" not in older["experimental_features"]
    assert older["adcp"]["principal"]["supported_sections"] == ["destinations"]


@pytest.mark.asyncio
async def test_coverage_is_served_from_3_3_only() -> None:
    seller = _Seller(principals=(AGENCY,))
    await seller.subscribe(
        AGENCY, [_buy("media-buy-a1", "2026-11-01T01:00:00Z", "2026-11-01T03:00:00Z")]
    )
    await seller.service.run_worker(now=NOW)
    served = await seller.status(
        AGENCY, "summary", SimpleNamespace(resolved_adcp_version="3.3-beta.1")
    )
    assert served["subscription_coverage"][0]["status"] == "active"
    older = await seller.status(AGENCY, "summary", SimpleNamespace(resolved_adcp_version="3.2"))
    assert "subscription_coverage" not in older


@pytest.mark.asyncio
async def test_resolve_principal_must_agree_with_the_caller() -> None:
    seller = _Seller(principals=(AGENCY,))
    await seller.service.run_worker(now=NOW)
    seller.service._caller_resolver = lambda _r, _c: ReportingStatusCaller(  # type: ignore[assignment]
        ACCOUNT, OTHER.subject
    )
    with pytest.raises(ReliableReportingConfigurationError, match="disagree"):
        await seller.service.get_reporting_status({"view": "summary"})


@pytest.mark.asyncio
async def test_construction_refuses_what_it_cannot_serve() -> None:
    seller = _Seller()
    with pytest.raises(ReliableReportingConfigurationError, match="production"):
        ReliableReportingService(
            store=InMemoryReportingLedgerStore(),
            account_context=_context,
            production=cast(Any, object()),
            reporting_subscriptions=seller.options,
        )
    mismatched = ReliableReportingService.memory(
        account_context=_context,
        status_retention_days=30,
        reporting_subscriptions=seller.options,
    )
    with pytest.raises(ReliableReportingConfigurationError, match="status_retention_days"):
        mismatched.validate()
    unmapped = ReliableReportingService.memory(
        account_context=_context, reporting_subscriptions=seller.options
    )
    other = dict(_capability_offering("gam"), offering_id="FREEWHEEL_SNAPSHOT_V1")
    unmapped.sources.register("freewheel", _ByBuySource(), capability_offerings=[other])
    with pytest.raises(ReliableReportingConfigurationError, match="dimension mapping"):
        unmapped.validate()
    with pytest.raises(TypeError, match="list_principals"):
        ReportingSubscriptionServiceOptions(
            engine=seller.engine, list_principals=None, resolve_principal=lambda r, c: AGENCY  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_without_the_option_nothing_changes() -> None:
    clock = DeterministicReportingClock(NOW)
    service = ReliableReportingService.memory(
        account_context=_context,
        caller_resolver=lambda _r, _c: ReportingStatusCaller(ACCOUNT, "buyer-1"),
        clock=clock,
        read_jitter_window=timedelta(0),
    )
    service.sources.register("gam", _ByBuySource())
    await service.configure(_configuration())
    turn = await service.run_worker(now=NOW)
    assert turn.subscription_obligations_written == 0 and turn.subscription_errors == {}
    capabilities = service.inject_capabilities(_capabilities(), adcp_version="3.3-beta.2")
    block = capabilities["media_buy"]["reporting_delivery"]
    assert "configuration_tasks" not in block and "subscriptions" not in block
    assert "standard_dimensions" not in block["offerings"][0]
    assert capabilities["adcp"]["principal"]["supported_sections"] == ["destinations"]
    assert "media_buy.reporting_subscriptions" not in capabilities["experimental_features"]
    status = await service.get_reporting_status({"view": "summary"})
    assert "subscription_coverage" not in status
