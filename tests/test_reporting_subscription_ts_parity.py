"""Names and error shapes that mirror the TypeScript SDK (adcp-client#3542-#3546)."""

from __future__ import annotations

import pytest

from adcp.reporting.subscriptions import AuthorizedAccountPage
from tests.test_reporting_subscription_engine import IDENTITY, World


def test_the_advanced_module_is_the_home_of_the_building_blocks() -> None:
    import adcp.reporting as reporting
    import adcp.reporting.advanced as advanced

    for name in (
        "ReportingSubscriptionEngine",
        "InMemoryReportingSubscriptionStore",
        "owed_subscription_periods",
        "resolve_subscription_offerings",
        "subscription_capabilities",
        "ReportingSubscriptionServiceOptions",
        "SubscriptionObligationPlanner",
        "ReportingSubscriptionEngineError",
    ):
        assert name in advanced.__all__ and getattr(advanced, name) is not None, name
    # The facade surface keeps only the managed path's names.
    assert "ReportingSubscriptionServiceOptions" not in reporting.__all__
    assert {"SubscriptionOptions", "PrincipalRef"} <= set(reporting.__all__)


def test_engine_errors_carry_code_and_recovery() -> None:
    from adcp.reporting.advanced import (
        ReportingSubscriptionEngineError,
        ReportingSubscriptionStateConflictError,
        decode_principal_state,
    )

    conflict = ReportingSubscriptionStateConflictError("kept changing")
    assert isinstance(conflict, ReportingSubscriptionEngineError)
    assert (conflict.code, conflict.recovery, conflict.dispatched) == (
        "SUBSCRIPTION_STATE_CONFLICT",
        "transient",
        None,
    )
    with pytest.raises(ValueError) as corrupt:
        decode_principal_state({"document_version": 99})
    assert isinstance(corrupt.value, ReportingSubscriptionEngineError)
    assert (corrupt.value.code, corrupt.value.recovery) == (
        "SUBSCRIPTION_STATE_CORRUPT",
        "terminal",
    )


@pytest.mark.asyncio
async def test_a_roster_that_never_finishes_is_refused(monkeypatch) -> None:
    from adcp.reporting.advanced import ReportingSubscriptionEngineError

    world = World()

    async def endless(principal, *, cursor):
        return AuthorizedAccountPage((), next_cursor=str(int(cursor or 0) + 1))

    world.host.list_authorized_accounts = endless  # type: ignore[method-assign]
    monkeypatch.setattr("adcp.reporting.subscriptions.engine._MAX_ROSTER_PAGES", 3)
    with pytest.raises(ReportingSubscriptionEngineError) as refused:
        await world.engine.reconcile(IDENTITY)
    assert (refused.value.code, refused.value.recovery) == (
        "SUBSCRIPTION_ROSTER_INCOMPLETE",
        "transient",
    )


@pytest.mark.asyncio
async def test_a_host_fact_of_the_wrong_shape_is_refused() -> None:
    from adcp.reporting.advanced import ReportingSubscriptionEngineError

    world = World()

    async def wrong(principal, *, cursor):
        return {"accounts": []}

    world.host.list_authorized_accounts = wrong  # type: ignore[method-assign]
    with pytest.raises(ReportingSubscriptionEngineError) as refused:
        await world.engine.reconcile(IDENTITY)
    assert refused.value.code == "SUBSCRIPTION_HOST_FACT_INVALID"


@pytest.mark.asyncio
async def test_update_subscription_status_is_renamed_record_subscription_readback() -> None:
    from adcp.server import PrincipalService

    principals = PrincipalService()
    with pytest.warns(DeprecationWarning, match="record_subscription_readback"):
        with pytest.raises(KeyError):  # delegated: no such subscription
            await principals.update_subscription_status(IDENTITY, "missing")


def test_configuration_errors_name_the_key_to_change() -> None:
    from adcp.reporting import ReliableReportingConfigurationError

    error = ReliableReportingConfigurationError(
        "x", code="SUBSCRIPTION_ROSTER_UNAVAILABLE", config_field="SubscriptionOptions.accounts"
    )
    assert error.config_field == "SubscriptionOptions.accounts"
    assert ReliableReportingConfigurationError("y").config_field is None


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", [None, 7, "mb-1", ({"media_buy_id": "x"},)])
async def test_account_buys_of_the_wrong_shape_are_refused(returned) -> None:
    from datetime import datetime, timezone

    from adcp.reporting.advanced import ReportingSubscriptionEngineError
    from adcp.reporting.subscriptions import AuthorizedAccount

    world = World()
    world.host.accounts["acct-1"] = AuthorizedAccount(
        "acct-1", timezone="UTC", authorized_at=datetime(2026, 9, 1, tzinfo=timezone.utc)
    )

    async def wrong(principal, account_id):
        return returned

    world.host.account_buys = wrong  # type: ignore[method-assign]
    with pytest.raises(ReportingSubscriptionEngineError) as refused:
        await world.engine._account_buys(IDENTITY, "acct-1")
    assert refused.value.code == "SUBSCRIPTION_HOST_FACT_INVALID"


@pytest.mark.asyncio
async def test_a_regression_is_counted_once_under_write_conflicts() -> None:
    from datetime import datetime, timezone

    from adcp.reporting.subscriptions import AuthorizedAccount
    from tests.test_reporting_subscription_state_store import _ConflictingStore

    world = World()
    world.host.accounts["acct-1"] = AuthorizedAccount(
        "acct-1", timezone="UTC", authorized_at=datetime(2026, 9, 1, tzinfo=timezone.utc)
    )
    await world.engine.reconcile(IDENTITY)
    store = _ConflictingStore(conflicts=2)
    store._documents = world.engine._store._documents  # same state, conflicting writes
    world.engine._store = store
    world.host.accounts["acct-1"] = AuthorizedAccount(
        "acct-1", timezone="UTC", authorized_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    await world.engine.observe_grant(
        IDENTITY,
        "acct-1",
        allowed_tasks=["get_reporting_status"],  # a change, so the write is attempted
        authorized_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert store.attempts == 3  # the change ran three times
    assert world.engine.authorized_at_regressions == 1
