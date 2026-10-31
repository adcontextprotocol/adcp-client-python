"""A deactivated subscription reads inactive in coverage (adcp#8613, 3.3.0-beta.3).

It stays listed only while it still has retained obligations for the account,
with ``status: "inactive"`` and ``deactivated_at``; after that its entry is
omitted. The generated 3.3.0-beta.3 models declare both, so every caller the
SDK serves subscriptions to reads them.
"""

from __future__ import annotations

import pytest

from adcp.reporting.subscriptions import SUBSCRIPTIONS_MIN_ADCP_VERSION, serves_subscriptions
from adcp.types.domains.core.reporting_subscription_coverage import (
    ReportingSubscriptionCoverage,
    Status,
)
from tests.test_reporting_subscription_engine import IDENTITY, World, buy, subscription


async def _deactivated(retention_days: int = 30) -> World:
    world = World(retention_days=retention_days)
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.host.buys["acct-1"] = [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-05T04:00:00Z")]
    await world.sync(subscription())
    await world.reconcile("2026-10-03T00:00:00Z")
    world.clock.set("2026-10-03T12:00:00Z")
    await world.sync(subscription(active=False))
    await world.reconcile("2026-10-04T00:00:00Z")
    return world


async def _coverage(world: World, now: str):
    world.clock.set(now)
    return await world.engine.subscription_coverage(IDENTITY, "acct-1")


@pytest.mark.asyncio
async def test_a_deactivated_subscription_reads_inactive_with_its_deactivation() -> None:
    world = await _deactivated()
    (entry,) = await _coverage(world, "2026-10-04T00:00:00Z")
    wire = entry.model_dump(mode="json", exclude_none=True)
    assert wire["status"] == "inactive"
    assert wire["deactivated_at"] == "2026-10-03T12:00:00Z"


@pytest.mark.asyncio
async def test_the_entry_is_omitted_once_no_obligation_is_retained() -> None:
    world = await _deactivated(retention_days=30)
    # The last owed period ended 2026-10-04 (deactivation at 10-03T12:00 keeps
    # the period that started before it); 30 days later nothing is retained.
    assert await _coverage(world, "2026-11-02T00:00:00Z")
    assert await _coverage(world, "2026-11-05T00:00:00Z") == []


def test_inactive_and_deactivated_at_are_generated() -> None:
    assert Status("inactive") is Status.inactive
    assert "deactivated_at" in ReportingSubscriptionCoverage.model_fields


@pytest.mark.parametrize(
    ("version", "served"),
    [
        (None, True),  # the pinned default, 3.3.0-beta.3
        ("3.3-beta.3", True),
        ("3.3.0-beta.3", True),
        ("3.3.0-beta.10", True),
        ("3.3-rc.1", True),
        ("3.3", True),
        ("3.4", True),
        ("4.0", True),
        ("3.3-beta.2", False),
        ("3.3.0-beta.1", False),
        ("3.2", False),
        ("", False),
        ("not-a-version", False),
    ],
)
def test_one_predicate_gates_the_subscription_surface(version: str | None, served: bool) -> None:
    assert SUBSCRIPTIONS_MIN_ADCP_VERSION == "3.3.0-beta.3"
    assert serves_subscriptions(version) is served
