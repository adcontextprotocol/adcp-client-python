"""Adopter pattern: LazyPlatformRouter with an async factory.

Verifies that an async factory satisfying PlatformFactory type-checks
cleanly. LazyPlatformRouter is the migration target for registry-keyed
adapters; this file tests the basic constructor + factory wiring.
"""
from __future__ import annotations

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    LazyPlatformRouter,
    SingletonAccounts,
)


class _StubPlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        channels=["display"],
        pricing_models=["cpm"],
    )
    accounts = SingletonAccounts(account_id="stub")


async def platform_factory(tenant_id: str) -> DecisioningPlatform:
    return _StubPlatform()


router = LazyPlatformRouter(
    accounts=SingletonAccounts(account_id="router"),
    factory=platform_factory,
    capabilities=DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        channels=["display"],
        pricing_models=["cpm"],
    ),
)
