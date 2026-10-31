"""Seller-engine rules settled for AdCP 3.3.0-beta.3 (adcp#8619).

Admission under ``max_accounts``, several pacing or analytics subscriptions per
account and purpose, membership edits in place, and ``destination_overrides``.
The buyer computes the same owed periods from the wire, so each test checks
the seller against :func:`owed_subscription_periods` where it can.
"""

from __future__ import annotations

import pytest

from adcp.reporting.subscriptions import (
    ReportingSubscriptionSupport,
    SubscriptionBuyPackage,
    SubscriptionDestination,
    SubscriptionVersionWindow,
    owed_subscription_periods,
)
from tests.test_reporting_subscription_engine import (
    ALL_IDS,
    World,
    at,
    buy,
    offering,
    subscription,
)


def _packaged(media_buy_id: str, start: str, end: str):
    return buy(
        media_buy_id,
        start,
        end,
        offering_ids=(),
        packages=(SubscriptionBuyPackage("pkg-1", ALL_IDS),),
    )


def _starts(periods) -> list[str]:
    return [item.period_start for item in periods]


@pytest.mark.asyncio
async def test_admission_orders_by_authorized_at_then_account_id() -> None:
    world = World(support=ReportingSubscriptionSupport(max_accounts=1))
    # acct-a sorts first by id, but acct-b was authorized first.
    world.grant("acct-b", "2026-09-01T00:00:00Z")
    world.grant("acct-a", "2026-09-15T00:00:00Z")
    await world.sync(subscription())
    await world.reconcile("2026-10-01T00:00:00Z")
    admitted = await world.coverage("acct-b", "analytics")
    waiting = await world.coverage("acct-a", "analytics")
    assert admitted.status.value == "active"
    assert waiting.status.value == "excluded"
    assert [item.reason.value for item in waiting.excluded] == ["capacity"]
    # Sticky: a candidate sorting first never displaces the admitted account.
    world.grant("acct-0", "2026-08-01T00:00:00Z")
    await world.reconcile("2026-10-02T00:00:00Z")
    assert (await world.coverage("acct-b", "analytics")).status.value == "active"
    assert (await world.coverage("acct-0", "analytics")).status.value == "excluded"


@pytest.mark.asyncio
async def test_two_analytics_subscriptions_resolving_one_offering_owe_it_once() -> None:
    world = World()
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.host.buys["acct-1"] = [_packaged("mb-1", "2026-10-01T04:00:00Z", "2026-10-04T04:00:00Z")]
    hourly = subscription(
        "c-hourly",
        selector={
            "feed_purpose": "analytics",
            "period_duration": "PT1H",
            "required_finality": "official",
            "dimensions": ["package"],
        },
    )
    await world.sync(subscription("a-daily"), subscription("b-daily"), hourly)
    await world.reconcile("2026-10-01T00:00:00Z")
    # The lowest subscription_id owes the shared offering; the other records a
    # package-level overridden exception naming it.
    second = await world.coverage("acct-1", "b-daily")
    (entry,) = second.excluded
    wire = entry.model_dump(mode="json", exclude_none=True)
    assert wire["reason"] == "overridden"
    assert wire["media_buy_id"] == "mb-1"
    assert wire["package_ids"] == ["pkg-1"]
    assert wire["overridden_by"] == {"subscription_id": "a-daily"}
    assert second.status.value == "active"  # a package-level entry, not account-level
    now = "2026-10-05T00:00:00Z"
    assert _starts(await world.seller_owes("a-daily", "acct-1", now)) == [
        "2026-10-01T04:00:00Z",
        "2026-10-02T04:00:00Z",
        "2026-10-03T04:00:00Z",
    ]
    assert await world.seller_owes("b-daily", "acct-1", now) == []
    # A different offering of the same purpose is owed alongside.
    assert await world.coverage("acct-1", "c-hourly")
    assert len(await world.seller_owes("c-hourly", "acct-1", now)) == 72


@pytest.mark.asyncio
async def test_removing_an_account_in_place_keeps_earlier_periods_owed() -> None:
    world = World()
    for account_id in ("acct-1", "acct-2"):
        world.grant(account_id, "2026-09-01T00:00:00Z")
        world.host.buys[account_id] = [
            buy(f"mb-{account_id}", "2026-10-01T04:00:00Z", "2026-10-06T04:00:00Z")
        ]
    both = subscription(scope={"account_ids": ["acct-1", "acct-2"]})
    await world.sync(both)
    await world.reconcile("2026-10-01T00:00:00Z")
    world.clock.set("2026-10-02T12:00:00Z")
    # Same version: the membership edit applies in place.
    await world.sync(subscription(scope={"account_ids": ["acct-1"]}))
    await world.reconcile("2026-10-02T12:00:00Z")
    state = await world.state("analytics")
    assert state.subscription_version == 1 and state.prior_versions is None
    removed = await world.coverage("acct-2", "analytics")
    assert removed is not None and removed.status.value == "excluded"
    (entry,) = removed.excluded
    wire = entry.model_dump(mode="json", exclude_none=True)
    # From E: the Oct 2 New York day started before the edit and stays owed.
    assert wire == {"reason": "excluded_by_buyer", "from": "2026-10-03T04:00:00Z"}
    owed = await world.seller_owes("analytics", "acct-2", "2026-10-07T00:00:00Z")
    assert _starts(owed) == ["2026-10-01T04:00:00Z", "2026-10-02T04:00:00Z"]
    # The buyer computes the same from the readback and the coverage entry.
    assert _starts(
        await world.buyer_expects("analytics", "acct-2", "2026-10-07T00:00:00Z", removed)
    ) == _starts(owed)


@pytest.mark.asyncio
async def test_adding_an_account_in_place_owes_only_from_when_it_joined() -> None:
    world = World()
    for account_id in ("acct-1", "acct-2"):
        world.grant(account_id, "2026-09-01T00:00:00Z")
        world.host.buys[account_id] = [
            buy(f"mb-{account_id}", "2026-10-01T04:00:00Z", "2026-10-06T04:00:00Z")
        ]
    await world.sync(subscription(scope={"account_ids": ["acct-1"]}))
    await world.reconcile("2026-10-01T00:00:00Z")
    world.clock.set("2026-10-03T12:00:00Z")
    await world.sync(subscription(scope={"account_ids": ["acct-1", "acct-2"]}))
    await world.reconcile("2026-10-03T12:00:00Z")
    added = await world.coverage("acct-2", "analytics")
    assert added.status.value == "active"
    (entry,) = added.excluded
    wire = entry.model_dump(mode="json", exclude_none=True)
    # One closed entry from the first boundary at or after accepted_at to the
    # first boundary at or after the edit.
    assert wire == {
        "reason": "excluded_by_buyer",
        "from": "2026-10-01T04:00:00Z",
        "until": "2026-10-04T04:00:00Z",
    }
    owed = await world.seller_owes("analytics", "acct-2", "2026-10-07T00:00:00Z")
    assert _starts(owed) == ["2026-10-04T04:00:00Z", "2026-10-05T04:00:00Z"]
    assert _starts(
        await world.buyer_expects("analytics", "acct-2", "2026-10-07T00:00:00Z", added)
    ) == _starts(owed)


def test_an_override_routes_one_account_to_another_destination_method() -> None:
    to_s3 = offering("pkg-s3", ("package",), method_pattern="file_transfer", method_transport="s3")
    to_gcs = offering(
        "pkg-gcs", ("package",), method_pattern="file_transfer", method_transport="gcs"
    )
    configuration = subscription(
        destination={"destination_id": "lake", "path_template": "l/{account_id}"},
        destination_overrides=[{"account_ids": ["acct-1"], "destination_id": "warehouse"}],
    )
    windows = [SubscriptionVersionWindow(1, at("2026-10-01T00:00:00Z"), None, configuration)]
    destinations = {
        "lake": SubscriptionDestination("file_transfer", "s3"),
        "warehouse": SubscriptionDestination("file_transfer", "gcs"),
    }
    kwargs = dict(
        subscription=windows,
        buys=[
            buy(
                "mb-1",
                "2026-10-01T04:00:00Z",
                "2026-10-02T04:00:00Z",
                offering_ids=("pkg-s3", "pkg-gcs"),
            )
        ],
        offerings=[to_s3, to_gcs],
        account_timezone="America/New_York",
        now=at("2026-10-03T00:00:00Z"),
        destinations=destinations,
    )
    routed = owed_subscription_periods(account_id="acct-1", **kwargs)
    default = owed_subscription_periods(account_id="acct-2", **kwargs)
    assert {item.offering_id for item in routed} == {"pkg-gcs"}
    assert {item.offering_id for item in default} == {"pkg-s3"}


def test_the_engine_routes_a_version_to_the_override_for_managed_binding() -> None:
    from adcp.reporting.subscriptions.engine import _Account, _routed_version

    configuration = subscription(
        destination={"destination_id": "lake", "path_template": "l/{account_id}"},
        destination_overrides=[
            {
                "operator_domains": ["agency.example"],
                "destination_id": "warehouse",
                "path_template": "w/{account_id}",
            }
        ],
    )
    agency = _Account(timezone="UTC", operator="agency.example")
    direct = _Account(timezone="UTC", operator="brand.example")
    assert _routed_version(configuration, "acct-1", agency)["destination"] == {
        "destination_id": "warehouse",
        "path_template": "w/{account_id}",
    }
    assert _routed_version(configuration, "acct-1", direct) is configuration


async def _page(world: World, subscription_id: str, list_name: str, **pagination):
    from tests.test_reporting_subscription_engine import IDENTITY

    request = {
        "reporting_subscription_page": {"subscription_id": subscription_id, "list": list_name}
    }
    if pagination:
        request["reporting_subscription_page"]["pagination"] = pagination
    response = await world.principals.get_principal(IDENTITY, request)
    return response.model_dump(mode="json", exclude_none=True)


@pytest.mark.asyncio
async def test_prior_versions_list_sixteen_inline_and_page_the_rest() -> None:
    world = World()
    for version in range(1, 19):
        world.clock.set(f"2026-10-{version:02d}T00:00:00Z")
        await world.sync(subscription(subscription_version=version))
    state = await world.state("analytics")
    assert [item.subscription_version for item in state.prior_versions] == list(range(17, 1, -1))
    assert state.prior_versions_count == 17
    first = await _page(world, "analytics", "prior_versions", max_results=10)
    page = first["reporting_subscription_page"]
    assert [item["subscription_version"] for item in page["prior_versions"]] == list(
        range(17, 7, -1)
    )
    assert page["pagination"] == {"has_more": True, "cursor": "10", "total_count": 17}
    rest = await _page(world, "analytics", "prior_versions", max_results=10, cursor="10")
    page = rest["reporting_subscription_page"]
    assert [item["subscription_version"] for item in page["prior_versions"]] == list(
        range(7, 0, -1)
    )
    assert page["pagination"] == {"has_more": False, "total_count": 17}
    # The usual result rides along.
    assert rest["result"]["kind"] == "current"


@pytest.mark.asyncio
async def test_an_account_list_pages_in_account_id_order_with_its_count() -> None:
    world = World()
    for account_id in ("acct-c", "acct-a", "acct-b"):
        world.grant(account_id, "2026-09-01T00:00:00Z")
        world.host.buys[account_id] = [
            buy(f"mb-{account_id}", "2026-10-01T04:00:00Z", "2026-10-03T04:00:00Z")
        ]
    await world.sync(subscription())
    await world.reconcile("2026-10-01T00:00:00Z")
    for account_id in ("acct-c", "acct-a", "acct-b"):
        world.revoke(account_id, "2026-10-02T00:00:00Z")
    await world.reconcile("2026-10-02T00:00:00Z")
    state = await world.state("analytics")
    assert state.accounts_unauthorized_count == 3
    first = await _page(world, "analytics", "accounts_unauthorized", max_results=2)
    page = first["reporting_subscription_page"]
    assert [item["account_id"] for item in page["accounts_unauthorized"]] == ["acct-a", "acct-b"]
    assert page["pagination"] == {"has_more": True, "cursor": "2", "total_count": 3}


@pytest.mark.asyncio
async def test_paging_an_unknown_subscription_is_invalid() -> None:
    world = World()
    await world.sync(subscription())
    response = await _page(world, "missing", "prior_versions")
    assert response["result"]["kind"] == "failed"
    assert response["result"]["errors"][0]["code"] == "INVALID_REQUEST"
