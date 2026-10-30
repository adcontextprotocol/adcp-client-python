"""The grant journal: the one source of ``authorization.authorized_at`` (AdCP 3.3).

The engine journals every grant it learns of (listed, pushed), applies the
chain rule, serves the chain start on ``list_accounts``/``sync_accounts``, and
feeds the same history to coverage. Grant checks read the journal, which
request-path observations and pushed changes keep current. Revocation means no
access (adcp#8607); ``wind_down_grant`` keeps reconciliation.

``grant-cases.json`` is shared with the TypeScript SDK (adcp-client#3543).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from adcp.exceptions import ADCPTaskError
from adcp.reporting import PrincipalRef
from adcp.reporting.subscriptions import (
    AuthorizedAccount,
    ReportingSubscriptionEngine,
    SubscriptionAuthorization,
    SubscriptionBuy,
    SubscriptionBuyPackage,
    SubscriptionOffering,
    authorization_chain_start,
)
from adcp.server import (
    REPORTING_WIND_DOWN_TASKS,
    PrincipalIdentity,
    PrincipalService,
    wind_down_grant,
)
from tests.test_reporting_subscription_engine import Clock, Host
from tests.test_reporting_subscription_managed import (
    ACCOUNT,
    AGENCY,
    START,
    _ctx,
    _World,
)

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures/reporting_subscriptions/grant-cases.json").read_text()
)
IDENTITY = PrincipalIdentity("https://agency.example/agent", "buyer_agent")
DAILY = SubscriptionOffering(
    offering_id="daily",
    report_definition_id="def-daily",
    reporting_profile="delivery",
    feed_purpose="analytics",
    period_duration="P1D",
    alignment="utc",
    delivery_sla="PT0S",
    supported_finality=frozenset({"snapshot"}),
    reconciliation_mode="delivery_only",
    dimensions=frozenset({"package"}),
)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# -- shared vectors ------------------------------------------------------------


def test_wind_down_grant_matches_the_shared_vector() -> None:
    assert list(REPORTING_WIND_DOWN_TASKS) == VECTORS["wind_down_grant"]["allowed_tasks"]
    assert (
        wind_down_grant().model_dump(mode="json", exclude_none=True) == VECTORS["wind_down_grant"]
    )
    kept = wind_down_grant(
        {"allowed_tasks": ["create_media_buy"], "authorized_at": "2026-08-01T00:00:00Z"}
    ).model_dump(mode="json", exclude_none=True)
    assert kept["authorized_at"] == "2026-08-01T00:00:00Z"
    assert kept["allowed_tasks"] == list(REPORTING_WIND_DOWN_TASKS)


@pytest.mark.parametrize("case", VECTORS["grant_cases"], ids=lambda case: case["name"])
async def test_coverage_follows_the_grant(case: dict[str, Any]) -> None:
    engine, host, principals = await _engine()
    authorization = case["authorization"]
    tasks = None if authorization is None else authorization["allowed_tasks"]
    host.accounts["account-a"] = AuthorizedAccount(
        "account-a",
        timezone="UTC",
        authorized_at=at("2026-08-01T00:00:00Z"),
        allowed_tasks=None if tasks is None else frozenset(tasks),
    )
    await engine.reconcile(IDENTITY)
    assert (
        await engine.grants_task(IDENTITY, "account-a", "get_reporting_status") is case["covered"]
    )


async def _engine() -> tuple[ReportingSubscriptionEngine, Host, PrincipalService]:
    clock = Clock("2026-09-01T12:00:00Z")
    host = Host()
    principals = PrincipalService()
    engine = ReportingSubscriptionEngine(
        principals=principals, host=host, offerings=[DAILY], clock=clock
    )
    principals.set_reporting_subscriptions(engine.principal_options())
    response = await principals.sync_principal(
        IDENTITY,
        {
            "idempotency_key": "grant-journal-key-000001",
            "configuration": {
                "reporting_subscriptions": [
                    {
                        "subscription_id": "analytics-daily",
                        "subscription_version": 1,
                        "active": True,
                        "scope": {"all_authorized_accounts": True},
                        "selector": {
                            "feed_purpose": "analytics",
                            "period_duration": "P1D",
                            "required_finality": "snapshot",
                            "dimensions": ["package"],
                        },
                        "reconciliation_mode": "delivery_only",
                    }
                ]
            },
        },
    )
    assert response.result.kind == "applied"
    host.buys["account-a"] = [
        SubscriptionBuy(
            media_buy_id="mb-1",
            confirmed_at=at("2026-09-01T06:00:00Z"),
            start_time=at("2026-09-02T00:00:00Z"),
            end_time=at("2026-09-11T00:00:00Z"),
            packages=(SubscriptionBuyPackage("pkg-1", ("daily",)),),
        )
    ]
    return engine, host, principals


@pytest.mark.parametrize("case", VECTORS["transition_cases"], ids=lambda case: case["name"])
async def test_grant_transitions_through_the_engine(case: dict[str, Any]) -> None:
    engine, host, principals = await _engine()
    authorized_at = at("2026-08-01T00:00:00Z")
    for step in case["steps"]:
        tasks = step["allowed_tasks"]
        if tasks == "revoked":
            host.accounts.pop("account-a", None)
        else:
            host.accounts["account-a"] = AuthorizedAccount(
                "account-a",
                timezone="UTC",
                authorized_at=at(step.get("granted_at", "2026-08-01T00:00:00Z"))
                if "granted_at" in step
                else authorized_at,
                allowed_tasks=None if tasks is None else frozenset(tasks),
            )
        await engine.reconcile(IDENTITY, now=at(step["at"]))
    expected = case["expected"]
    now = at(case["evaluate_at"])
    await engine.reconcile(IDENTITY, now=now)
    assert (
        await engine.grants_task(IDENTITY, "account-a", "get_reporting_status")
        is expected["authorized"]
    )
    owed = await engine.owed_periods(IDENTITY, "analytics-daily", "account-a", now=now)
    assert [at(period.period_start) for period in owed] == [at(item) for item in expected["owed"]]
    [state] = await principals.reporting_subscriptions(IDENTITY)
    unauthorized = [
        item.unauthorized_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        for item in state.accounts_unauthorized or []
        if item.account_id == "account-a"
    ]
    assert unauthorized == expected["unauthorized_at"]


# -- the chain rule ------------------------------------------------------------


def test_the_chain_start_holds_inside_retention_and_restarts_after_it() -> None:
    first = SubscriptionAuthorization(at("2026-01-01T00:00:00Z"), at("2026-03-01T00:00:00Z"))
    inside = [first, SubscriptionAuthorization(at("2026-03-20T00:00:00Z"))]
    assert authorization_chain_start(inside, status_retention_days=30) == at("2026-01-01T00:00:00Z")
    after = [first, SubscriptionAuthorization(at("2026-04-15T00:00:00Z"))]
    assert authorization_chain_start(after, status_retention_days=30) == at("2026-04-15T00:00:00Z")
    assert authorization_chain_start([first], status_retention_days=30) is None
    assert authorization_chain_start([], status_retention_days=30) is None


# -- the managed path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_accounts_serves_the_journals_authorized_at() -> None:
    world = _World()
    del world.seller.accounts[ACCOUNT]["authorization"]["authorized_at"]
    await world.subscribe()
    listed = await world.platform.list_accounts({}, _ctx())
    served = listed["accounts"][0]["authorization"]["authorized_at"]
    # The host omitted it: the journal dated the grant at its first observation.
    assert served == "2026-11-01T00:00:00Z"
    engine = world.service._managed.engine
    assert await engine.authorized_at(PrincipalRef(None, AGENCY), ACCOUNT) == START


@pytest.mark.asyncio
async def test_a_host_value_that_moves_backwards_is_rejected() -> None:
    world = _World()
    await world.subscribe()
    engine = world.service._managed.engine
    world.seller.accounts[ACCOUNT]["authorization"]["authorized_at"] = "2026-09-01T00:00:00Z"
    listed = await world.platform.list_accounts({}, _ctx())
    assert listed["accounts"][0]["authorization"]["authorized_at"] == "2026-10-01T00:00:00Z"
    assert engine.authorized_at_regressions == 1


@pytest.mark.asyncio
async def test_a_regrant_inside_retention_keeps_authorized_at() -> None:
    world = _World()
    await world.subscribe()
    principal = PrincipalRef(None, AGENCY)
    grants = world.service.grants
    await grants.changed(principal, ACCOUNT, at=START + timedelta(days=2), authorized=False)
    await grants.changed(principal, ACCOUNT, at=START + timedelta(days=3), authorized=True)
    engine = world.service._managed.engine
    assert await engine.authorized_at(principal, ACCOUNT) == datetime(
        2026, 10, 1, tzinfo=timezone.utc
    )
    world.clock.set(START + timedelta(days=3))
    listed = await world.platform.list_accounts({}, _ctx())
    assert listed["accounts"][0]["authorization"]["authorized_at"] == "2026-10-01T00:00:00Z"


@pytest.mark.asyncio
async def test_a_pushed_revocation_takes_effect_at_once() -> None:
    world = _World()
    await world.subscribe()
    principal = PrincipalRef(None, AGENCY)
    revoked_at = START + timedelta(minutes=10)
    world.clock.set(revoked_at)
    await world.service.grants.changed(principal, ACCOUNT, at=revoked_at, authorized=False)
    engine = world.service._managed.engine
    assert not await engine.grants_task(principal, ACCOUNT, "sync_reporting_receipts")
    assert await engine.authorized_at(principal, ACCOUNT) is None
    # The readback records the loss without waiting for a listing.
    [state] = await world.service.principals.reporting_subscriptions(principal)
    assert [item.account_id for item in state.accounts_unauthorized] == [ACCOUNT]


@pytest.mark.asyncio
async def test_a_wind_down_keeps_reconciliation_open() -> None:
    world = _World()
    await world.subscribe()
    principal = PrincipalRef(None, AGENCY)
    await world.service.grants.changed(
        principal,
        ACCOUNT,
        at=START + timedelta(minutes=10),
        authorized=True,
        allowed_tasks=REPORTING_WIND_DOWN_TASKS,
    )
    engine = world.service._managed.engine
    for task in REPORTING_WIND_DOWN_TASKS:
        assert await engine.grants_task(principal, ACCOUNT, task)
    assert not await engine.grants_task(principal, ACCOUNT, "get_media_buys")
    assert await engine.authorized_at(principal, ACCOUNT) == datetime(
        2026, 10, 1, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_a_served_reduction_is_read_by_the_next_grant_check() -> None:
    """#1728: grant checks no longer wait for the next reconcile."""
    world = _World()
    await world.subscribe()
    world.clock.set(START + timedelta(hours=3, minutes=30))
    await world.service.run_worker()
    world.seller.accounts[ACCOUNT]["authorization"]["allowed_tasks"] = ["get_media_buys"]
    await world.platform.list_accounts({}, _ctx())
    with pytest.raises(ADCPTaskError) as refused:
        await world.platform.get_reporting_status({"view": "periods"}, _ctx())
    assert refused.value.errors[0].code == "ACCOUNT_NOT_FOUND"


@pytest.mark.asyncio
async def test_callers_without_subscriptions_are_served_unchanged() -> None:
    world = _World()
    listed = await world.platform.list_accounts({}, _ctx("https://stranger.example/agent"))
    assert listed["accounts"][0]["authorization"]["authorized_at"] == "2026-10-01T00:00:00Z"
    assert await world.service._managed.engine.list_subscription_principals() == []


@pytest.mark.asyncio
async def test_a_served_page_is_observed_in_one_journal_write() -> None:
    world = _World()
    for index in range(3):
        world.seller.accounts[f"account-{index}"] = {
            **world.seller.accounts[ACCOUNT],
            "account_id": f"account-{index}",
        }
    await world.subscribe()
    store = world.service._managed.engine.store
    loads = 0
    original = store.load

    async def counting(subject: str) -> Any:
        nonlocal loads
        loads += 1
        return await original(subject)

    store.load = counting  # type: ignore[method-assign]
    listed = await world.platform.list_accounts({}, _ctx())
    assert len(listed["accounts"]) == 4
    assert loads == 1


@pytest.mark.asyncio
async def test_an_authorization_without_allowed_tasks_is_observed_as_a_full_grant() -> None:
    world = _World()
    await world.subscribe()
    authorization = world.seller.accounts[ACCOUNT]["authorization"]
    del authorization["allowed_tasks"]
    authorization["authorized_at"] = "2026-09-01T00:00:00Z"  # regressed: rejected
    listed = await world.platform.list_accounts({}, _ctx())
    assert listed["accounts"][0]["authorization"]["authorized_at"] == "2026-10-01T00:00:00Z"
    assert world.service._managed.engine.authorized_at_regressions == 1
