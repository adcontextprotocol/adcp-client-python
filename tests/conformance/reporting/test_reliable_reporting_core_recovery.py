"""Core restart discovery and SDK model publication against memory and real PG."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from adcp.reporting.fixtures import redacted_capabilities
from adcp.reporting.ledger import InMemoryReportingLedgerStore, ReportingStatusCaller
from adcp.reporting.service import ReliableReportingConfigurationError, ReliableReportingService
from adcp.reporting.testing import ScriptedReportingAdapter
from adcp.types import GetMediaBuyDeliveryResponse
from tests.test_reliable_reporting_service import (
    NOW,
    _account_context,
    _capability_offering,
    _configuration,
)

from ._generation_support import isolated_reporting_pool

ServiceFactory = Callable[..., ReliableReportingService]


@pytest.fixture(params=["memory", "pg", "pg-autocommit"])
async def core_factory(request: Any) -> AsyncIterator[tuple[ServiceFactory, datetime]]:
    if request.param == "memory":
        ledger = InMemoryReportingLedgerStore(clock=lambda: NOW)

        def memory(**kwargs: Any) -> ReliableReportingService:
            return ReliableReportingService(store=ledger, clock=lambda: NOW, **kwargs)

        yield memory, NOW
    else:
        async with isolated_reporting_pool(autocommit=request.param == "pg-autocommit") as pool:
            async with pool.connection() as connection:
                row = await (await connection.execute("SELECT clock_timestamp()")).fetchone()
            now = row[0]

            def postgres(**kwargs: Any) -> ReliableReportingService:
                return ReliableReportingService.postgres(pool=pool, clock=lambda: now, **kwargs)

            yield postgres, now


def _sdk_delivery(start: datetime, end: datetime, *, packages: bool) -> GetMediaBuyDeliveryResponse:
    return GetMediaBuyDeliveryResponse.model_validate(
        {
            "reporting_period": {"start": start, "end": end},
            "media_buy_deliveries": [
                {
                    "media_buy_id": "media-buy-redacted",
                    "status": "active",
                    "totals": {"impressions": 10.0, "spend": 1.25},
                    "by_package": (
                        [
                            {
                                "package_id": "pkg-1",
                                "pricing_model": "cpm",
                                "rate": 1.0,
                                "currency": "USD",
                                "impressions": 10.0,
                                "spend": 1.25,
                            }
                        ]
                        if packages
                        else []
                    ),
                }
            ],
        }
    )


@pytest.mark.parametrize("packages", [False, True])
async def test_restart_discovers_all_accounts_and_generations_without_reconfigure(
    core_factory: tuple[ServiceFactory, datetime], packages: bool
) -> None:
    make_service, now = core_factory
    boundary = now.replace(minute=0, second=0, microsecond=0)
    first = replace(
        _configuration(account_id="account-a"),
        activated_at=boundary - timedelta(hours=3) + timedelta(minutes=20),
        deactivated_at=boundary - timedelta(hours=1),
    )
    next_generation = replace(
        first,
        delivery_config_version=2,
        activated_at=boundary - timedelta(minutes=20),
        deactivated_at=None,
    )
    other = replace(first, account_id="account-b", delivery_config_id="freewheel-delivery")
    configurations = (first, next_generation, other)
    initial = make_service(account_context=_account_context)
    for route in ("gam", "freewheel"):
        initial.sources.register(route, ScriptedReportingAdapter(redacted_capabilities(), []))
    try:
        # Deliberately persist out of order; enumeration retains both retired
        # and current generations and separates identical IDs across accounts.
        for configuration in reversed(configurations):
            await initial.configure(configuration)
        await initial.start()
        assert await initial.store.list_all_configurations() == configurations
    finally:
        await initial.close()

    resolved: list[Any] = []

    async def resolve(configuration: Any) -> Any:
        resolved.append(configuration)
        return _account_context(configuration)

    recovered = make_service(account_context=resolve)
    response = _sdk_delivery(
        boundary - timedelta(hours=2), boundary - timedelta(hours=1), packages=packages
    )
    adapters = {}
    for route in ("gam", "freewheel"):
        adapters[route] = ScriptedReportingAdapter(redacted_capabilities(), [response])
        recovered.sources.register(
            route, adapters[route], capability_offerings=[_capability_offering(route)]
        )
    revisions = {}
    try:
        await recovered.start()  # no configure(), no adopter account enumeration
        assert resolved == list(configurations)
        assert await recovered.store.list_all_configurations() == configurations
        turn = await recovered.run_worker(now=now)
        assert not turn.configuration_errors
        assert set(turn.configurations) == {item.generation_key for item in configurations}
        assert not turn.configurations[next_generation.generation_key].revisions_committed
        for configuration in (first, other):
            (revision_id,) = turn.configurations[configuration.generation_key].revisions_committed
            revision = await recovered.store.get_revision(
                account_id=configuration.account_id, reporting_revision_id=revision_id
            )
            assert revision is not None
            revisions[configuration.account_id] = revision
            page = await recovered.store.read_revision_rows(
                account_id=configuration.account_id, reporting_revision_id=revision_id
            )
            assert page.rows[0]["spend"] == "1.25"
            assert page.rows[0]["impressions"] in (10, "10")
        assert len(adapters["gam"].calls) == len(adapters["freewheel"].calls) == 1
    finally:
        await recovered.close()

    # Discovering completed generations must not refetch or rewrite published
    # bytes, checkpoints, activation dates, or their retirement boundary.
    second = make_service(
        account_context=resolve,
        caller_resolver=lambda req, ctx: ReportingStatusCaller(ctx, "buyer-1"),
    )
    for route in ("gam", "freewheel"):
        second.sources.register(route, ScriptedReportingAdapter(redacted_capabilities(), []))
    try:
        turn = await second.run_worker(now=now)
        assert not turn.configuration_errors
        assert all(not item.revisions_committed for item in turn.configurations.values())
        assert await second.store.list_all_configurations() == configurations
        for account_id, revision in revisions.items():
            assert (
                await second.store.get_revision(
                    account_id=account_id, reporting_revision_id=revision.reporting_revision_id
                )
                == revision
            )
            content = await second.get_revision_content(
                {"reporting_revision_id": revision.reporting_revision_id}, account_id
            )
            assert content["reporting_revision_binding"]["content_sha256"] == (
                revision.revision_content_sha256
            )
    finally:
        await second.close()


async def test_restart_rejects_unavailable_adapter_without_changing_retained_configuration(
    core_factory: tuple[ServiceFactory, datetime],
) -> None:
    make_service, _ = core_factory
    initial = make_service(account_context=_account_context)
    initial.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), []))
    configuration = _configuration()
    await initial.configure(configuration)
    await initial.start()
    await initial.close()

    restarted = make_service(account_context=_account_context)
    restarted.sources.register("freewheel", ScriptedReportingAdapter(redacted_capabilities(), []))
    try:
        with pytest.raises(ReliableReportingConfigurationError, match="unregistered.*gam"):
            await restarted.start()
        assert not restarted.ready
        assert await restarted.store.list_all_configurations() == (configuration,)
    finally:
        await restarted.close()
