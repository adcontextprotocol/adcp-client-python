from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from adcp.reporting.ledger.producer import ProducerOfferings
from adcp.reporting.production.source_registry import ReportingProductionSourceRegistry
from adcp.reporting.service import ReportingAccountContext


def test_registry_freeze_rejects_producers_without_an_offering():
    producer = SimpleNamespace(_offerings=ProducerOfferings())
    unused = SimpleNamespace(_offerings=ProducerOfferings())
    assert unused == producer and unused is not producer
    registry = ReportingProductionSourceRegistry(account_context=lambda _: None)
    registry.register("used", producer)
    registry.register("unused", unused)
    offering = SimpleNamespace(producer=producer)

    with pytest.raises(ValueError, match="offering"):
        registry.freeze((offering,))
    assert not registry._frozen

    registry.freeze((offering, SimpleNamespace(producer=unused)))
    with pytest.raises(ValueError, match="frozen"):
        registry.register("later", SimpleNamespace(_offerings=ProducerOfferings()))


@pytest.mark.asyncio
async def test_registry_freezes_profile_and_refuses_replaced_currency():
    producer = SimpleNamespace(_offerings=ProducerOfferings(official_offering_id="official"))
    context = ReportingAccountContext(
        "account", "adapter", "USD", {}, official_offering_id="official"
    )
    calls = []

    def resolve(configuration):
        calls.append(configuration)
        return context

    registry = ReportingProductionSourceRegistry(account_context=resolve)
    registry.register("adapter", producer)
    configuration = SimpleNamespace(account_id="account", account_timezone="UTC")
    offering = SimpleNamespace(
        producer=producer, offering_id="public", source_offering_id="official"
    )
    capabilities = SimpleNamespace(capabilities_sha256="a" * 64)
    frozen = await registry.resolve(configuration, offering, capabilities)
    assert frozen.document()["currency"] == "USD"
    assert registry.recover(configuration, offering, capabilities, frozen.document()) == frozen
    assert len(calls) == 1
    producer._offerings = replace(producer._offerings, currency="EUR")
    with pytest.raises(ValueError, match="profile"):
        registry.recover(configuration, offering, capabilities, frozen.document())


@pytest.mark.asyncio
async def test_registry_missing_context_is_explicit_and_budget_is_frozen():
    producer = SimpleNamespace(_offerings=ProducerOfferings(official_offering_id="official"))
    context = ReportingAccountContext(
        "account", "adapter", "USD", {}, official_offering_id="official"
    )
    registry = ReportingProductionSourceRegistry(account_context=lambda _: context)
    registry.register("adapter", producer)
    configuration = SimpleNamespace(account_id="account", account_timezone="UTC")
    offering = SimpleNamespace(
        producer=producer, offering_id="public", source_offering_id="official"
    )
    capabilities = SimpleNamespace(capabilities_sha256="a" * 64)
    with pytest.raises(ValueError, match="migration"):
        registry.recover(configuration, offering, capabilities, None)
    frozen = await registry.resolve(configuration, offering, capabilities)
    producer._offerings = replace(producer._offerings, slice_timeout=timedelta(seconds=1))
    with pytest.raises(ValueError, match="profile"):
        registry.recover(configuration, offering, capabilities, frozen.document())
