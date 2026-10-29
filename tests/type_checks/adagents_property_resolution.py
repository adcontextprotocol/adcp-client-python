"""Publisher sync helpers accept typed custom discovery and preserve dictionaries."""

from typing import Any

from adcp import (
    fetch_adagents,
    properties_for_publisher_domain,
    resolve_properties_for_agent,
    verify_cross_domain_properties,
)


async def fetch_publisher(domain: str) -> dict[str, Any]:
    return await fetch_adagents(domain, timeout=5.0)


async def sync_publisher(domain: str, agent_url: str) -> list[dict[str, Any]]:
    document = await fetch_adagents(domain, validate_structure=False)
    authorized = resolve_properties_for_agent(
        document, agent_url, mode="permissive", publisher_domain=domain
    )
    verified = await verify_cross_domain_properties(
        authorized, domain, agent_url, fetcher=fetch_publisher
    )
    return properties_for_publisher_domain(verified, domain, include_unattributed=False)
