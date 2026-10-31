"""A deactivated subscription reads inactive in coverage (drafted for 3.3.0-beta.3).

It stays listed only while it still has retained obligations for the account,
with ``status: "inactive"`` and ``deactivated_at``; after that its entry is
omitted. Callers served before 3.3.0-beta.3 get the account-level status their
schema defines.
"""

from __future__ import annotations

import pytest

from adcp.reporting.subscriptions.wire import serves_inactive_coverage
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


async def _coverage(world: World, version: str, now: str):
    world.clock.set(now)
    return await world.engine.subscription_coverage(IDENTITY, "acct-1", adcp_version=version)


@pytest.mark.asyncio
async def test_a_deactivated_subscription_reads_inactive_with_its_deactivation() -> None:
    world = await _deactivated()
    (entry,) = await _coverage(world, "3.3-beta.3", "2026-10-04T00:00:00Z")
    wire = entry.model_dump(mode="json", exclude_none=True)
    assert wire["status"] == "inactive"
    assert wire["deactivated_at"] == "2026-10-03T12:00:00Z"


@pytest.mark.asyncio
async def test_earlier_callers_get_the_account_level_status() -> None:
    world = await _deactivated()
    for version in ("3.3-beta.1", "3.3-beta.2", None):
        (entry,) = await _coverage(world, version, "2026-10-04T00:00:00Z")
        wire = entry.model_dump(mode="json", exclude_none=True)
        assert wire["status"] == "active" and "deactivated_at" not in wire


@pytest.mark.asyncio
async def test_the_entry_is_omitted_once_no_obligation_is_retained() -> None:
    world = await _deactivated(retention_days=30)
    # The last owed period ended 2026-10-04 (deactivation at 10-03T12:00 keeps
    # the period that started before it); 30 days later nothing is retained.
    assert await _coverage(world, "3.3-beta.3", "2026-11-02T00:00:00Z")
    assert await _coverage(world, "3.3-beta.3", "2026-11-05T00:00:00Z") == []


def test_the_inactive_gate_is_3_3_beta_3() -> None:
    assert not serves_inactive_coverage(None)
    assert not serves_inactive_coverage("3.3-beta.2")
    assert serves_inactive_coverage("3.3.0-beta.3")
    assert serves_inactive_coverage("3.3.0-rc.1")
    assert serves_inactive_coverage("3.3")
    assert serves_inactive_coverage("3.4")
