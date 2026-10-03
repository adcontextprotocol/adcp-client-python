"""Conditional cache refresh respects each caller's current validation policy."""

import socket
from contextlib import asynccontextmanager

import httpx
import pytest

from adcp.adagents import AdagentsCacheEntry, fetch_adagents_with_cache
from adcp.exceptions import AdagentsValidationError


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))
        ],
    )


async def test_opt_out_cache_revalidates_on_strict_refresh() -> None:
    bare = {
        "authorized_agents": [{"url": "https://agent.example/mcp", "authorized_for": "Display"}]
    }

    @asynccontextmanager
    async def factory(url: str, timeout: float):
        def response(request: httpx.Request) -> httpx.Response:
            if request.headers.get("if-none-match") == "cached":
                return httpx.Response(304)
            return httpx.Response(200, json=bare, headers={"etag": "cached"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            yield client

    fetched = await fetch_adagents_with_cache(
        "publisher.com", validate_structure=False, transport_factory=factory
    )
    cache = AdagentsCacheEntry(fetched.data, etag=fetched.etag)
    with pytest.raises(AdagentsValidationError, match="structure"):
        await fetch_adagents_with_cache(
            "publisher.com", cache_entry=cache, transport_factory=factory
        )
    refreshed = await fetch_adagents_with_cache(
        "publisher.com", cache_entry=cache, validate_structure=False, transport_factory=factory
    )
    assert refreshed.not_modified
    assert refreshed.data is cache.body
    assert refreshed.etag == "cached"


@pytest.mark.parametrize(
    "body",
    [
        {"authorized_agents": {}},
        {},
        {
            "authorized_agents": [
                {"url": "https://agent.example/mcp", "authorized_for": "Display"}
            ],
            "formats": [{"reference_renderer": {"runtime": "browser-esm"}}],
        },
    ],
)
async def test_opt_out_cached_document_still_requires_endpoint_shape(body) -> None:
    @asynccontextmanager
    async def factory(url: str, timeout: float):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(304))
        ) as client:
            yield client

    with pytest.raises(AdagentsValidationError):
        await fetch_adagents_with_cache(
            "publisher.com",
            cache_entry=AdagentsCacheEntry(body, etag="cached"),
            validate_structure=False,
            transport_factory=factory,
        )
