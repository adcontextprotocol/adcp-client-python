"""The buyer write facade (``client.reporting``) and the raw ``sync_principal`` guard.

A real in-process seller (``PrincipalService`` plus the subscription engine) sits
behind the test client, so the buyer sees only wire responses.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from adcp.exceptions import is_pre_dispatch_error
from adcp.reporting.subscriptions import ReportingSubscriptionError
from adcp.reporting.subscriptions.facade import ReportingClient, guard_sync_principal
from adcp.types import SyncPrincipalRequest
from adcp.types.core import TaskResult, TaskStatus
from tests.test_reporting_subscription_consumer import SUBSCRIPTION, Client, Seller

pytestmark = pytest.mark.asyncio

DESTINATION = {
    "pattern": "warehouse_materialization",
    "destination_id": "warehouse",
    "active": True,
    "provider": {"domain": "fixture.example.test"},
    "transport": "fixture-sql",
    "location": "project.reporting",
    "accepted_verification_profiles": ["canonical_digest"],
}


def _subscription(subscription_id: str = "analytics", **changes: Any) -> dict[str, Any]:
    value = {k: v for k, v in SUBSCRIPTION.items() if k != "subscription_version"}
    value["subscription_id"] = subscription_id
    value.update(changes)
    return value


class Counting(Client):
    def __init__(self, seller: Seller) -> None:
        super().__init__(seller)
        self.writes = 0

    async def sync_principal(self, request):
        self.writes += 1
        return await super().sync_principal(request)


def _state(readback, subscription_id):
    state = readback.get(subscription_id)
    assert state is not None
    return state


async def test_subscribe_creates_auto_versions_and_keeps_the_others() -> None:
    client = Counting(Seller())
    reporting = ReportingClient(client)
    created = await reporting.subscribe(_subscription())
    assert (created.action, created.subscription_version) == ("created", 1)
    assert created.state.state == "active"
    # A second subscription leaves the first untouched.
    await reporting.subscribe(_subscription("pacing", scope={"account_ids": ["acct-1"]}))
    listed = await reporting.list_subscriptions()
    assert {s.subscription_id for s in listed.subscriptions} == {"analytics", "pacing"}
    assert all(s.state == "active" for s in listed.subscriptions)
    # The same content writes nothing.
    writes = client.writes
    again = await reporting.subscribe(_subscription())
    assert (again.action, again.subscription_version) == ("unchanged", 1)
    assert client.writes == writes
    # Changed content bumps the version.
    selector = {**SUBSCRIPTION["selector"], "required_finality": "official", "match": "all"}
    updated = await reporting.subscribe(_subscription(selector=selector))
    assert (updated.action, updated.subscription_version) == ("updated", 2)
    assert _state(await reporting.list_subscriptions(), "pacing").subscription_version == 1


async def test_a_supplied_version_must_exceed_the_current_one_when_content_changes() -> None:
    reporting = ReportingClient(Client(Seller()))
    await reporting.subscribe(_subscription())
    selector = {**SUBSCRIPTION["selector"], "match": "all"}
    with pytest.raises(ReportingSubscriptionError) as refused:
        await reporting.subscribe(_subscription(selector=selector, subscription_version=1))
    assert refused.value.code == "INVALID_REQUEST" and is_pre_dispatch_error(refused.value)


async def test_unsubscribe_is_the_deactivation_path_and_resubscribe_reactivates() -> None:
    reporting = ReportingClient(Client(Seller()))
    await reporting.subscribe(_subscription())
    gone = await reporting.unsubscribe("analytics")
    assert gone.action == "deactivated" and gone.state.state == "inactive"
    back = await reporting.subscribe(_subscription())
    assert (back.action, back.subscription_version) == ("reactivated", 1)
    assert back.state.state == "active"
    with pytest.raises(ReportingSubscriptionError, match="list_subscriptions"):
        await reporting.unsubscribe("missing")


async def test_pre_dispatch_checks_send_nothing() -> None:
    client = Counting(Seller())
    reporting = ReportingClient(client)
    with pytest.raises(ReportingSubscriptionError) as scope:
        await reporting.subscribe(_subscription(scope={}))
    assert is_pre_dispatch_error(scope.value) and "all_authorized_accounts" in str(scope.value)
    with pytest.raises(ReportingSubscriptionError, match="register_destination"):
        await reporting.subscribe(
            _subscription(
                destination={"destination_id": "warehouse", "path_template": "a/{account_id}"}
            )
        )
    with pytest.raises(ReportingSubscriptionError, match=r"\{account_id\}"):
        await reporting.subscribe(
            _subscription(destination={"destination_id": "warehouse", "path_template": "flat"}),
            register_destination=DESTINATION,
        )
    assert client.writes == 0


async def test_raw_writes_are_guarded_against_deactivation_and_revocation() -> None:
    client = Client(Seller())
    reporting = ReportingClient(client)
    await reporting.subscribe(_subscription())
    await reporting.subscribe(_subscription("pacing", scope={"account_ids": ["acct-1"]}))
    raw = SyncPrincipalRequest.model_validate(
        {
            "idempotency_key": "raw-write-0000000001",
            "configuration": {"reporting_subscriptions": [{**SUBSCRIPTION}]},
        }
    )
    with pytest.raises(ReportingSubscriptionError) as refused:
        await guard_sync_principal(client, raw)
    error = refused.value
    assert error.code == "SUBSCRIPTION_WOULD_DEACTIVATE" and is_pre_dispatch_error(error)
    assert error.would_deactivate == ("pacing",) and error.recovery == "correctable"
    await guard_sync_principal(client, raw, allow_deactivation=True)
    # An explicit ``active: false`` is a deliberate deactivation.
    pacing = (
        (await reporting.list_subscriptions())
        .get("pacing")
        .configuration.model_dump(mode="json", exclude_none=True)
    )
    explicit = SyncPrincipalRequest.model_validate(
        {
            "idempotency_key": "raw-write-0000000002",
            "configuration": {
                "reporting_subscriptions": [{**SUBSCRIPTION}, {**pacing, "active": False}]
            },
        }
    )
    await guard_sync_principal(client, explicit)


async def test_omitting_a_destination_is_refused_unless_allowed() -> None:
    client = Client(Seller())
    reporting = ReportingClient(client)
    await reporting.subscribe(
        _subscription(
            destination={"destination_id": "warehouse", "path_template": "a/{account_id}"}
        ),
        register_destination=DESTINATION,
        acknowledge_cross_operator=True,
    )
    raw = SyncPrincipalRequest.model_validate(
        {"idempotency_key": "raw-write-0000000003", "configuration": {"reporting_destinations": []}}
    )
    with pytest.raises(ReportingSubscriptionError) as refused:
        await guard_sync_principal(client, raw)
    assert refused.value.code == "DESTINATION_WOULD_REVOKE"
    assert refused.value.would_revoke == ("warehouse",)
    await guard_sync_principal(client, raw, allow_revocation=True)


async def test_the_adcp_client_guards_raw_sync_principal() -> None:
    from adcp.client import ADCPClient
    from adcp.types.core import AgentConfig, Protocol

    adcp_client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example/mcp", protocol=Protocol.MCP)
    )
    seller = Client(Seller())
    await ReportingClient(seller).subscribe(_subscription())
    await ReportingClient(seller).subscribe(
        _subscription("pacing", scope={"account_ids": ["acct-1"]})
    )
    adcp_client.get_principal = seller.get_principal  # type: ignore[method-assign]
    unguarded = AsyncMock()
    adcp_client._sync_principal_unguarded = unguarded  # type: ignore[method-assign]
    raw = SyncPrincipalRequest.model_validate(
        {
            "idempotency_key": "raw-write-0000000004",
            "configuration": {"reporting_subscriptions": [{**SUBSCRIPTION}]},
        }
    )
    with pytest.raises(ReportingSubscriptionError):
        await adcp_client.sync_principal(raw)
    unguarded.assert_not_awaited()
    await adcp_client.sync_principal(raw, allow_deactivation=True)
    unguarded.assert_awaited_once()
    assert isinstance(adcp_client.reporting, ReportingClient)


async def test_replace_subscriptions_needs_explicit_flags() -> None:
    reporting = ReportingClient(Client(Seller()))
    await reporting.subscribe(_subscription())
    with pytest.raises(ReportingSubscriptionError, match="deactivate_all"):
        await reporting.replace_subscriptions([])
    other = {
        **_subscription("pacing", scope={"account_ids": ["acct-1"]}),
        "subscription_version": 1,
    }
    with pytest.raises(ReportingSubscriptionError) as refused:
        await reporting.replace_subscriptions([other])
    assert refused.value.code == "SUBSCRIPTION_WOULD_DEACTIVATE"
    readback = await reporting.replace_subscriptions([other], deactivate_omitted=True)
    assert _state(readback, "analytics").state == "inactive"


class Interleaving(Client):
    """Another writer changes the principal before each of this client's writes."""

    def __init__(self, seller: Seller, times: int) -> None:
        super().__init__(seller)
        self.times, self.keys = times, []

    async def sync_principal(self, request):
        self.keys.append(request.idempotency_key)
        if self.times > 0:
            self.times -= 1
            other = Client(self.seller)
            await ReportingClient(other).subscribe(
                _subscription(f"other-{self.times}", scope={"account_ids": [f"acct-{self.times}"]})
            )
        return await super().sync_principal(request)


async def test_a_conflict_retries_with_a_fresh_key_and_then_gives_up() -> None:
    seller = Seller()
    # A configured principal, so each write carries expected_configuration_version.
    await ReportingClient(Client(seller)).subscribe(
        _subscription("seed", scope={"account_ids": ["acct-seed"]})
    )
    client = Interleaving(seller, times=1)
    result = await ReportingClient(client).subscribe(_subscription())
    assert result.action == "created" and len(set(client.keys)) == 2
    # The interleaved writer's subscription survived the re-merge.
    assert _state(result.readback, "other-0").state == "active"
    stuck_seller = Seller()
    await ReportingClient(Client(stuck_seller)).subscribe(
        _subscription("seed", scope={"account_ids": ["acct-seed"]})
    )
    stuck = Interleaving(stuck_seller, times=10)
    with pytest.raises(ReportingSubscriptionError) as exhausted:
        await ReportingClient(stuck, max_attempts=2).subscribe(_subscription())
    assert exhausted.value.code == "SUBSCRIPTION_CONFLICT_EXHAUSTED"
    assert exhausted.value.recovery == "transient" and not is_pre_dispatch_error(exhausted.value)


class Altering(Client):
    async def sync_principal(self, request):
        result = await super().sync_principal(request)
        state = result.data.result.configuration.reporting_subscriptions[0]
        state.subscription_version = 99
        return result


async def test_a_readback_mismatch_is_terminal() -> None:
    with pytest.raises(ReportingSubscriptionError) as mismatch:
        await ReportingClient(Altering(Seller())).subscribe(_subscription())
    assert mismatch.value.code == "SUBSCRIPTION_READBACK_MISMATCH"
    assert mismatch.value.recovery == "terminal" and mismatch.value.readback is not None


class Sorting(Client):
    """A conformant seller that returns every set-valued array in sorted order."""

    async def sync_principal(self, request):
        result = await super().sync_principal(request)
        for state in result.data.result.configuration.reporting_subscriptions:
            configuration = state.configuration
            dumped = configuration.model_dump(mode="json", exclude_none=True)
            for name in ("exclude_account_ids", "operator_domains"):
                if name in dumped:
                    dumped[name] = sorted(dumped[name])
            if "account_ids" in dumped.get("scope", {}):
                dumped["scope"]["account_ids"] = sorted(dumped["scope"]["account_ids"])
            if "dimensions" in dumped.get("selector", {}):
                dumped["selector"]["dimensions"] = sorted(dumped["selector"]["dimensions"])
            state.configuration = type(configuration).model_validate(dumped)
        return result


async def test_the_readback_compares_set_valued_arrays_without_order() -> None:
    reporting = ReportingClient(Sorting(Seller()))
    selector = {**SUBSCRIPTION["selector"], "dimensions": ["package", "creative"]}
    result = await reporting.subscribe(
        _subscription(
            scope={"account_ids": ["acct-2", "acct-1"]},
            operator_domains=["z.example", "a.example"],
            selector=selector,
        )
    )
    assert result.action == "created"


def test_differing_path_orders_only_lists_that_carry_order() -> None:
    from adcp.reporting.subscriptions.facade import differing_path

    assert (
        differing_path({"operator_domains": ["b", "a"]}, {"operator_domains": ["a", "b"]}) is None
    )
    assert differing_path({"operator_domains": ["b", "a"]}, {"operator_domains": ["a", "c"]}) == (
        "/operator_domains"
    )
    overrides = [
        {"destination_id": "x", "account_ids": ["1", "2"]},
        {"destination_id": "y", "operator_domains": ["o.example"]},
    ]
    stored = [
        {"destination_id": "y", "operator_domains": ["o.example"], "added": True},
        {"destination_id": "x", "account_ids": ["2", "1"]},
    ]
    assert (
        differing_path({"destination_overrides": overrides}, {"destination_overrides": stored})
        is None
    )
    stored[0]["destination_id"] = "z"
    assert (
        differing_path({"destination_overrides": overrides}, {"destination_overrides": stored})
        == "/destination_overrides/1"
    )
    # Any other array still compares element for element.
    assert differing_path({"other": ["a", "b"]}, {"other": ["b", "a"]}) == "/other/0"


class Roster(Client):
    def __init__(self, seller: Seller, accounts: list[dict[str, Any]], offerings=None) -> None:
        super().__init__(seller)
        self.accounts, self.offerings = accounts, offerings

    async def list_accounts(self, request):
        from types import SimpleNamespace

        data = SimpleNamespace(accounts=self.accounts, pagination=None)
        return TaskResult.model_construct(status=TaskStatus.COMPLETED, data=data, success=True)

    async def fetch_capabilities(self):
        from types import SimpleNamespace

        media_buy = {"reporting_delivery": {"offerings": self.offerings or []}}
        return SimpleNamespace(
            model_dump=lambda **_: {"media_buy": media_buy} if self.offerings else {}
        )


def _account(account_id: str, operator: str, configs=()) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "name": account_id,
        "status": "active",
        "operator": operator,
        "reporting_delivery_configs": list(configs),
    }


async def test_roster_checks_refuse_an_id_clash() -> None:
    clash = Roster(
        Seller(),
        [
            _account(
                "acct-1", "op-a.example", [{"configuration": {"delivery_config_id": "analytics"}}]
            )
        ],
    )
    with pytest.raises(ReportingSubscriptionError) as refused:
        await ReportingClient(clash).subscribe(_subscription())
    error = refused.value
    assert error.code == "SUBSCRIPTION_ID_CLASH" and is_pre_dispatch_error(error)
    assert (error.delivery_config_id, error.account_id) == ("analytics", "acct-1")


async def test_an_all_accounts_subscription_naming_a_destination_needs_operator_domains() -> None:
    to_warehouse = _subscription(
        destination={"destination_id": "warehouse", "path_template": "a/{account_id}"}
    )
    reporting = ReportingClient(Client(Seller()))
    with pytest.raises(ReportingSubscriptionError) as scoped:
        await reporting.subscribe(to_warehouse, register_destination=DESTINATION)
    assert scoped.value.code == "SUBSCRIPTION_OPERATOR_SCOPE_REQUIRED"
    assert is_pre_dispatch_error(scoped.value)
    created = await reporting.subscribe(
        {**to_warehouse, "operator_domains": ["op-a.example"]}, register_destination=DESTINATION
    )
    assert created.action == "created"


def _offering(offering_id: str, period: str, dimensions: list[str]) -> dict[str, Any]:
    return {
        "offering_id": offering_id,
        "report_definition_id": "def-pkg",
        "reporting_profile": {"id": "delivery"},
        "feed_purpose": "analytics",
        "schedule": {"period_duration": period, "alignment": "utc", "delivery_sla": "PT6H"},
        "supported_finality": ["official"],
        "reconciliation_mode": "delivery_only",
        "standard_dimensions": dimensions,
    }


async def test_a_selector_no_advertised_offering_matches_is_refused_before_dispatch() -> None:
    hourly = Roster(Seller(), [], offerings=[_offering("hourly", "PT1H", ["package"])])
    with pytest.raises(ReportingSubscriptionError) as unmatched:
        await ReportingClient(hourly).subscribe(_subscription())
    error = unmatched.value
    assert error.code == "SUBSCRIPTION_SELECTOR_UNMATCHED" and is_pre_dispatch_error(error)
    assert error.nearest == (("hourly", ("period_duration",)),)
    assert "hourly (period_duration)" in str(error)
    daily = Roster(Seller(), [], offerings=[_offering("pkg", "P1D", ["package"])])
    preview = await ReportingClient(daily).preview(_subscription())
    assert (preview.would_be, preview.subscription_version) == ("created", 1)
    assert preview.offering_ids == ("pkg",)
