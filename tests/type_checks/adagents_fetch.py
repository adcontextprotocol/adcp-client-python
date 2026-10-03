"""Adopters can inject a typed per-hop context and catch typed status errors."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from adcp import (
    AdagentsHTTPError,
    AdagentsTransportFactory,
    fetch_adagents,
    fetch_adagents_with_cache,
    parse_managerdomains,
    validate_adagents_domain,
)


@asynccontextmanager
async def fixture_transport(url: str, timeout: float) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        yield client


async def fetch_publisher(domain: str) -> int:
    factory: AdagentsTransportFactory = fixture_transport
    managers: list[str] = parse_managerdomains("MANAGERDOMAIN=manager.com")
    try:
        document = await fetch_adagents(domain, validate_structure=False, transport_factory=factory)
        await fetch_adagents_with_cache(domain, validate_structure=False, transport_factory=factory)
        await validate_adagents_domain(domain, validate_structure=False, transport_factory=factory)
        return len(document) + len(managers)
    except AdagentsHTTPError as error:
        status: int = error.status_code
        url: str = error.url
        return status + len(url)
