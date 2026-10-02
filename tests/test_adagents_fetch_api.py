"""Public fetch API contracts, using only deterministic transport fixtures."""

import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest

from adcp import (
    AdagentsHTTPError,
    AdagentsTransportFactory,
    fetch_adagents,
    fetch_adagents_with_cache,
    parse_managerdomains,
    validate_adagents_domain,
)
from adcp.adagents import MAX_POINTER_BYTES, _parse_managerdomains, resolve_properties_for_agent
from adcp.exceptions import (
    AdagentsAccessBlockedError,
    AdagentsNotFoundError,
    AdagentsValidationError,
)

AGENT = "https://sales.agent.com/mcp"
BARE = {
    "authorized_agents": [{"url": AGENT, "authorized_for": "Display"}],
    "properties": [
        {
            "property_id": "site",
            "property_type": "website",
            "name": "Site",
            "identifiers": [{"type": "domain", "value": "publisher.com"}],
        }
    ],
}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))
        ],
    )


def mock_factory(
    handler: Callable[[httpx.Request], httpx.Response], created: list[str]
) -> AdagentsTransportFactory:
    @asynccontextmanager
    async def factory(url: str, timeout: float) -> AsyncIterator[httpx.AsyncClient]:
        created.append(url)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ) as client:
            yield client
        assert client.is_closed

    return factory


@pytest.mark.parametrize("status", [201, 204, 401, 403, 429, 500, 503])
async def test_http_errors_carry_status_and_url(status: int) -> None:
    created: list[str] = []
    with pytest.raises(AdagentsHTTPError) as caught:
        await fetch_adagents(
            "publisher.com",
            transport_factory=mock_factory(lambda r: httpx.Response(status), created),
        )
    assert caught.value.status_code == status
    assert caught.value.url == "https://publisher.com/.well-known/adagents.json"
    assert isinstance(caught.value, AdagentsValidationError)
    assert len(created) == 1  # No MANAGERDOMAIN fallback on an outage.


async def test_specialized_errors_keep_their_meaning() -> None:
    with pytest.raises(AdagentsAccessBlockedError):
        await fetch_adagents(
            "publisher.com",
            transport_factory=mock_factory(
                lambda r: httpx.Response(403, headers={"cf-mitigated": "challenge"}), []
            ),
        )
    with pytest.raises(AdagentsNotFoundError):
        await fetch_adagents(
            "publisher.com", transport_factory=mock_factory(lambda r: httpx.Response(404), [])
        )


async def test_bare_entries_require_explicit_opt_out() -> None:
    factory = mock_factory(lambda r: httpx.Response(200, json=BARE), [])
    with pytest.raises(AdagentsValidationError, match="structure"):
        await fetch_adagents("publisher.com", transport_factory=factory)
    data = await fetch_adagents(
        "publisher.com", validate_structure=False, transport_factory=factory
    )
    assert data == BARE
    assert resolve_properties_for_agent(data, AGENT, mode="permissive") == BARE["properties"]
    cached = await fetch_adagents_with_cache(
        "publisher.com", validate_structure=False, transport_factory=factory
    )
    assert cached.data == BARE
    validated = await validate_adagents_domain(
        "publisher.com", validate_structure=False, transport_factory=factory
    )
    assert validated.valid and validated.data == BARE


@pytest.mark.parametrize("body", [b"not json", b"\xff", b"[]", b"{}", b'{"authorized_agents": {}}'])
async def test_opt_out_preserves_json_and_endpoint_shape(body: bytes) -> None:
    with pytest.raises(AdagentsValidationError):
        await fetch_adagents(
            "publisher.com",
            validate_structure=False,
            transport_factory=mock_factory(lambda r: httpx.Response(200, content=body), []),
        )


async def test_opt_out_preserves_renderer_origin_and_metadata() -> None:
    data = dict(BARE, formats=[{"reference_renderer": {"runtime": "browser-esm"}}])
    with pytest.raises(AdagentsValidationError, match="renderer catalog"):
        await fetch_adagents(
            "publisher.com",
            validate_structure=False,
            transport_factory=mock_factory(lambda r: httpx.Response(200, json=data), []),
        )


async def test_opt_out_preserves_size_cap() -> None:
    with pytest.raises(AdagentsValidationError, match="size cap"):
        await fetch_adagents(
            "publisher.com",
            validate_structure=False,
            transport_factory=mock_factory(
                lambda r: httpx.Response(200, content=b" " * (MAX_POINTER_BYTES + 1)), []
            ),
        )


async def test_opt_out_preserves_ssrf_and_dns_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []
    factory = mock_factory(lambda r: httpx.Response(200, json=BARE), created)
    with pytest.raises(AdagentsValidationError, match="private"):
        await fetch_adagents("169.254.169.254", validate_structure=False, transport_factory=factory)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
        ],
    )
    with pytest.raises(AdagentsValidationError, match="private"):
        await fetch_adagents("publisher.com", validate_structure=False, transport_factory=factory)
    assert created == []


@pytest.mark.parametrize("discovery", ["http", "authoritative", "manager"])
async def test_factory_and_structure_option_reach_owned_hops(discovery: str) -> None:
    initial = "https://publisher.com/.well-known/adagents.json"
    target = (
        "https://www.publisher.com/.well-known/adagents.json"
        if discovery == "http"
        else "https://manager.com/.well-known/adagents.json"
    )
    created: list[str] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == initial:
            if discovery == "http":
                return httpx.Response(302, headers={"location": target})
            if discovery == "authoritative":
                return httpx.Response(200, json={"authoritative_location": target})
            return httpx.Response(404)
        if request.url.path == "/ads.txt":
            return httpx.Response(200, text="MANAGERDOMAIN=manager.com")
        assert str(request.url) == target
        assert "authorization" not in request.headers
        return httpx.Response(200, json=BARE)

    factory = mock_factory(handler, created)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer publisher-only"}
    ) as client:
        data = await fetch_adagents(
            "publisher.com", client=client, validate_structure=False, transport_factory=factory
        )
        assert not client.is_closed
    assert data == BARE
    assert created == [target]
    assert requests[0].headers["authorization"] == "Bearer publisher-only"


def test_public_manager_parser_preserves_private_alias_and_source_order() -> None:
    text = (
        "# MANAGERDOMAIN=ignored.com\n"
        " ManagerDomain=FIRST.COM # comment\nmanagerdomain=second.com\n"
    )
    assert parse_managerdomains(text) == ["first.com", "second.com"]
    assert _parse_managerdomains is parse_managerdomains
