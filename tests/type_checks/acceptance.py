"""Adopter-facing type checks for seller acceptance-policy discovery."""

from adcp import (
    AcceptanceContext,
    AcceptancePolicyAssessment,
    AcceptancePolicyDiscovery,
    AcceptancePolicyResolver,
)


async def assess(
    discovery: AcceptancePolicyDiscovery,
    context: AcceptanceContext,
) -> AcceptancePolicyAssessment:
    async with AcceptancePolicyResolver() as resolver:
        return await resolver.assess(
            discovery,
            context,
            applies_to="media_buy",
            product_profile_ids=["product-restrictions"],
            cache_ttl_seconds=300,
            capabilities_version="cap-42",
        )
