"""Buyer client helpers and tracker for reporting subscriptions (AdCP 3.3).

A real in-process seller (``PrincipalService`` plus ``ReportingSubscriptionEngine``)
sits behind a thin client adapter, so the buyer sees only wire responses.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from adcp.reporting import plan_consumer_statuses
from adcp.reporting.subscriptions import (
    AuthorizedAccount,
    AuthorizedAccountPage,
    ReportingSubscriptionClientError,
    ReportingSubscriptionEngine,
    ReportingSubscriptionTracker,
    SubscriptionBuy,
    SubscriptionOffering,
    is_subscription_change,
    load_subscription_coverage,
    read_reporting_subscriptions,
    sync_reporting_subscriptions,
)
from adcp.server import PrincipalChange, PrincipalIdentity, PrincipalService
from adcp.types import GetReportingStatusResponse
from adcp.types.core import TaskResult, TaskStatus

IDENTITY = PrincipalIdentity("https://agency.example/agent", "buyer_agent")
#: The in-process client is served 3.3.0-beta.2, which carries the adcp#8585 fields.
BETA_2 = SimpleNamespace(resolved_adcp_version="3.3-beta.2")
NY = "America/New_York"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


DAILY = SubscriptionOffering(
    offering_id="pkg",
    report_definition_id="def-pkg",
    reporting_profile="delivery",
    feed_purpose="analytics",
    period_duration="P1D",
    alignment="account_timezone",
    delivery_sla="PT6H",
    supported_finality=frozenset({"official"}),
    reconciliation_mode="delivery_only",
    dimensions=frozenset({"package"}),
)
SUBSCRIPTION = {
    "subscription_id": "analytics",
    "subscription_version": 1,
    "active": True,
    "scope": {"all_authorized_accounts": True},
    "selector": {
        "feed_purpose": "analytics",
        "period_duration": "P1D",
        "required_finality": "official",
        "dimensions": ["package"],
    },
    "reconciliation_mode": "delivery_only",
}
BUY = SubscriptionBuy(
    media_buy_id="mb-1",
    confirmed_at=at("2026-10-01T00:00:00Z"),
    start_time=at("2026-10-01T04:00:00Z"),
    end_time=at("2026-10-12T04:00:00Z"),
    reporting_delivery_offering_ids=("pkg",),
)


class Seller:
    def __init__(self) -> None:
        self.now = at("2026-10-01T00:00:00Z")
        self.accounts: dict[str, AuthorizedAccount] = {}
        self.notifications: list[dict] = []

        async def emit(change: PrincipalChange) -> None:
            self.notifications.append(
                {"notification_type": "principal.changed", "reason": change.reason}
            )

        self.principals = PrincipalService(emit_change=emit)
        self.engine = ReportingSubscriptionEngine(
            principals=self.principals, host=self, offerings=[DAILY], clock=lambda: self.now
        )
        self.principals.set_reporting_subscriptions(self.engine.principal_options())

    async def list_authorized_accounts(self, principal, *, cursor):
        return AuthorizedAccountPage(tuple(self.accounts.values()))

    async def account_buys(self, principal, account_id):
        return [BUY] if account_id in {"acct-1"} else []

    async def reconcile(self, when: str) -> None:
        self.now = at(when)
        await self.engine.reconcile(IDENTITY)


class Client:
    """What an ADCPClient returns, backed by the in-process seller (served 3.3.0-beta.2)."""

    def __init__(self, seller: Seller) -> None:
        self.seller = seller

    async def sync_principal(self, request):
        data = await self.seller.principals.sync_principal(IDENTITY, request, context=BETA_2)
        return TaskResult(status=TaskStatus.COMPLETED, data=data, success=True)

    async def get_principal(self, request):
        data = await self.seller.principals.get_principal(IDENTITY, request, context=BETA_2)
        return TaskResult(status=TaskStatus.COMPLETED, data=data, success=True)

    async def get_reporting_status(self, request):
        account_id = request.account.account_id
        coverage = await self.seller.engine.subscription_coverage(
            IDENTITY, account_id, adcp_version="3.3-beta.2"
        )
        data = GetReportingStatusResponse.model_construct(subscription_coverage=coverage or None)
        return TaskResult(status=TaskStatus.COMPLETED, data=data, success=True)


@pytest.mark.asyncio
async def test_sync_and_read_back() -> None:
    seller = Seller()
    client = Client(seller)
    assert (await read_reporting_subscriptions(client)).subscriptions == ()
    readback = await sync_reporting_subscriptions(client, [SUBSCRIPTION])
    state = readback.get("analytics")
    assert state is not None and state.state == "active"
    assert readback.configuration_version
    again = await read_reporting_subscriptions(client)
    assert again.get("analytics").accepted_at == at("2026-10-01T00:00:00Z")
    dry = await sync_reporting_subscriptions(
        client, [{**SUBSCRIPTION, "subscription_version": 2}], dry_run=True
    )
    assert dry.subscriptions == ()


@pytest.mark.asyncio
async def test_rejection_surfaces_the_seller_code() -> None:
    client = Client(Seller())
    with pytest.raises(ReportingSubscriptionClientError) as raised:
        await sync_reporting_subscriptions(client, [{**SUBSCRIPTION, "scope": {}}])
    assert raised.value.code == "INVALID_REQUEST"


def test_notification_filter() -> None:
    assert is_subscription_change(
        {"notification_type": "principal.changed", "reason": "reporting_subscription_changed"}
    )
    assert is_subscription_change({"reason": "destination_state_changed"})
    assert not is_subscription_change({"reason": "declarations_intersection_changed"})
    assert not is_subscription_change(
        {"notification_type": "account.status_changed", "reason": "other"}
    )


@pytest.mark.asyncio
async def test_tracker_follows_revocation_and_regrant_and_matches_the_seller() -> None:
    seller = Seller()
    client = Client(seller)
    tracker = ReportingSubscriptionTracker.from_capabilities(
        client,
        {
            "media_buy": {
                "reporting_delivery": {
                    "status_retention_days": 400,
                    "subscriptions": {"supported": True},
                }
            }
        },
        offerings=[DAILY],
    )
    seller.accounts["acct-1"] = AuthorizedAccount(
        "acct-1", timezone=NY, authorized_at=at("2026-09-01T00:00:00Z")
    )
    tracker.observe_account("acct-1", authorized_at=at("2026-09-01T00:00:00Z"), timezone=NY)
    await tracker.sync([SUBSCRIPTION])
    await seller.reconcile("2026-10-01T00:00:00Z")

    # Authority ends; the seller says so through principal.changed.
    del seller.accounts["acct-1"]
    await seller.reconcile("2026-10-04T08:00:00Z")
    notification = seller.notifications[-1]
    assert notification["reason"] == "reporting_subscription_changed"
    assert await tracker.handle_notification(notification)
    unauthorized = tracker.readback.get("analytics").accounts_unauthorized
    assert [entry.account_id for entry in unauthorized] == ["acct-1"]
    assert await load_subscription_coverage(client, account={"account_id": "acct-1"}) == ()

    now = at("2026-10-05T00:00:00Z")
    buyer = tracker.expected_periods("analytics", "acct-1", buys=[BUY], now=now)
    seller_owes = await seller.engine.owed_periods(IDENTITY, "analytics", "acct-1", now=now)
    assert buyer == seller_owes
    assert [p.withheld for p in buyer] == [False, False, True]

    # Re-grant: list_accounts shows the new authorized_at.
    regrant = at("2026-10-06T15:00:00Z")
    seller.accounts["acct-1"] = AuthorizedAccount("acct-1", timezone=NY, authorized_at=regrant)
    await seller.reconcile("2026-10-06T15:00:00Z")
    await tracker.handle_notification(seller.notifications[-1])
    tracker.observe_account("acct-1", authorized_at=regrant, timezone=NY)
    (coverage,) = await load_subscription_coverage(client, account={"account_id": "acct-1"})
    assert coverage.gaps
    now = at("2026-10-10T00:00:00Z")
    buyer = tracker.expected_periods("analytics", "acct-1", buys=[BUY], now=now, coverage=coverage)
    assert buyer == await seller.engine.owed_periods(IDENTITY, "analytics", "acct-1", now=now)

    # Subscription expectations feed the consumer-status planner unchanged:
    # missing periods past their deadline become obligation_missing, keyed by
    # the subscription, and a withheld period is never claimed.
    plan = plan_consumer_statuses(
        [],
        now=now + timedelta(days=1),
        automated_recovery_window=timedelta(hours=6),
        missing_expected_periods=buyer,
        account_id="acct-1",
    )
    assert {intent.consumer_status for intent in plan} == {"obligation_missing"}
    assert {intent.delivery_config_id for intent in plan} == {"analytics"}
    assert len(plan) == len(buyer)


@pytest.mark.asyncio
async def test_tracker_keeps_every_registered_version() -> None:
    seller = Seller()
    client = Client(seller)
    tracker = ReportingSubscriptionTracker(client, offerings=[DAILY])
    seller.accounts["acct-1"] = AuthorizedAccount("acct-1", timezone=NY)
    tracker.observe_account("acct-1", authorized_at=at("2026-09-01T00:00:00Z"), timezone=NY)
    await tracker.sync([SUBSCRIPTION])
    seller.now = at("2026-10-03T12:00:00Z")
    narrowed = {**SUBSCRIPTION, "subscription_version": 2, "scope": {"account_ids": ["acct-2"]}}
    await tracker.sync([narrowed])
    periods = tracker.expected_periods(
        "analytics", "acct-1", buys=[BUY], now=at("2026-10-08T00:00:00Z")
    )
    # Version 1 covered acct-1 until version 2 dropped it: Oct 1-3 local days.
    assert [p.period_start for p in periods] == [
        "2026-10-01T04:00:00Z",
        "2026-10-02T04:00:00Z",
        "2026-10-03T04:00:00Z",
    ]
    assert {p.delivery_config_version for p in periods} == {1}


@pytest.mark.asyncio
async def test_regrant_within_retention_keeps_authorized_at() -> None:
    # adcp#8585: a re-grant inside retention continues the chain, so
    # list_accounts shows the original authorized_at. The tracker records the
    # re-grant when it observes the account readable again.
    seller = Seller()
    client = Client(seller)
    tracker = ReportingSubscriptionTracker(client, offerings=[DAILY], status_retention_days=400)
    granted = at("2026-09-01T00:00:00Z")
    seller.accounts["acct-1"] = AuthorizedAccount("acct-1", timezone=NY, authorized_at=granted)
    tracker.observe_account("acct-1", authorized_at=granted, timezone=NY)
    await tracker.sync([SUBSCRIPTION])
    await seller.reconcile("2026-10-01T00:00:00Z")
    del seller.accounts["acct-1"]
    await seller.reconcile("2026-10-04T08:00:00Z")
    await tracker.handle_notification(seller.notifications[-1])
    seller.accounts["acct-1"] = AuthorizedAccount("acct-1", timezone=NY, authorized_at=granted)
    await seller.reconcile("2026-10-06T15:00:00Z")
    await tracker.handle_notification(seller.notifications[-1])
    with pytest.raises(ValueError):
        tracker.observe_account("acct-1", authorized_at=granted, timezone=NY)
    tracker.observe_account(
        "acct-1", authorized_at=granted, timezone=NY, observed_at=at("2026-10-06T15:00:00Z")
    )
    (coverage,) = await load_subscription_coverage(client, account={"account_id": "acct-1"})
    now = at("2026-10-10T00:00:00Z")
    buyer = tracker.expected_periods("analytics", "acct-1", buys=[BUY], now=now, coverage=coverage)
    assert buyer == await seller.engine.owed_periods(IDENTITY, "analytics", "acct-1", now=now)
