"""Tests for callable ``public_url`` (PublicUrlResolver) on the A2A agent card.

Issue #647: per-request agent-card URL resolution for multi-tenant
subdomain deployments.
"""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from adcp.server.a2a_server import (
    PublicUrlResolver,
    _validate_card_url,
    create_a2a_server,
)
from adcp.server.base import ADCPHandler
from adcp.server.responses import capabilities_response


class _OkHandler(ADCPHandler):  # type: ignore[misc]
    async def get_adcp_capabilities(self, params, context=None):  # type: ignore[no-untyped-def]
        return capabilities_response(["media_buy"])


# ---------------------------------------------------------------------------
# _validate_card_url unit tests
# ---------------------------------------------------------------------------


def test_validate_card_url_accepts_https() -> None:
    assert _validate_card_url("https://acme.example.com/") == "https://acme.example.com/"


def test_validate_card_url_accepts_http_localhost() -> None:
    assert _validate_card_url("http://localhost:3001/") == "http://localhost:3001/"


def test_validate_card_url_accepts_http_127() -> None:
    assert _validate_card_url("http://127.0.0.1:3001/") == "http://127.0.0.1:3001/"


def test_validate_card_url_rejects_http_non_loopback() -> None:
    with pytest.raises(ValueError, match="scheme must be 'https'"):
        _validate_card_url("http://acme.example.com/")


def test_validate_card_url_rejects_no_scheme() -> None:
    with pytest.raises(ValueError, match="must be an absolute URL"):
        _validate_card_url("acme.example.com")


def test_validate_card_url_rejects_empty() -> None:
    with pytest.raises(ValueError):
        _validate_card_url("")


# ---------------------------------------------------------------------------
# Integration tests — per-request card endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callable_public_url_serves_per_request_card() -> None:
    """Callable ``public_url`` returns a card with the resolver's URL on each request."""
    calls: list[str] = []

    def resolver(request) -> str:  # type: ignore[no-untyped-def]
        host = request.headers.get("host", "localhost")
        calls.append(host)
        return f"https://{host}/"

    app = create_a2a_server(_OkHandler(), name="test-agent", validation=None, public_url=resolver)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/.well-known/agent-card.json",
                headers={"host": "tenant-a.example.com"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert "name" in body
    # The resolver's URL must appear in supportedInterfaces
    interfaces = body.get("supportedInterfaces") or body.get("supported_interfaces", [])
    urls = [iface.get("url", "") for iface in interfaces]
    assert any(
        "tenant-a.example.com" in u for u in urls
    ), f"expected tenant-a.example.com in {urls}"
    # Resolver was called
    assert calls == ["tenant-a.example.com"]


@pytest.mark.asyncio
async def test_callable_public_url_different_hosts_per_request() -> None:
    """Each card request gets its own URL from the resolver."""

    def resolver(request) -> str:  # type: ignore[no-untyped-def]
        host = request.headers.get("host", "localhost")
        return f"https://{host}/"

    app = create_a2a_server(_OkHandler(), name="test-agent", validation=None, public_url=resolver)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp_acme = await client.get(
                "/.well-known/agent-card.json",
                headers={"host": "tenant-a.example.com"},
            )
            resp_beta = await client.get(
                "/.well-known/agent-card.json",
                headers={"host": "tenant-b.example.com"},
            )

    for resp, expected_host in [
        (resp_acme, "tenant-a.example.com"),
        (resp_beta, "tenant-b.example.com"),
    ]:
        assert resp.status_code == 200
        body = resp.json()
        interfaces = body.get("supportedInterfaces") or body.get("supported_interfaces", [])
        urls = [iface.get("url", "") for iface in interfaces]
        assert any(expected_host in u for u in urls), f"expected {expected_host} in {urls}"


@pytest.mark.asyncio
async def test_callable_public_url_0_3_alias_also_per_request() -> None:
    """0.3 alias /.well-known/agent.json also served by the resolver."""

    def resolver(request) -> str:  # type: ignore[no-untyped-def]
        host = request.headers.get("host", "localhost")
        return f"https://{host}/"

    app = create_a2a_server(_OkHandler(), name="test-agent", validation=None, public_url=resolver)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/.well-known/agent.json",
                headers={"host": "tenant.example.com"},
            )

    assert resp.status_code == 200
    body = resp.json()
    interfaces = body.get("supportedInterfaces") or body.get("supported_interfaces", [])
    urls = [iface.get("url", "") for iface in interfaces]
    assert any("tenant.example.com" in u for u in urls), f"expected tenant.example.com in {urls}"


@pytest.mark.asyncio
async def test_callable_public_url_resolver_error_returns_500() -> None:
    """When the resolver raises, the endpoint returns 500 without leaking the error."""

    def resolver(request) -> str:  # type: ignore[no-untyped-def]
        raise RuntimeError("upstream lookup failed")

    app = create_a2a_server(_OkHandler(), name="test-agent", validation=None, public_url=resolver)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/.well-known/agent-card.json")

    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_callable_public_url_invalid_url_returns_500() -> None:
    """When the resolver returns an http:// non-loopback URL, the endpoint returns 500."""

    def resolver(request) -> str:  # type: ignore[no-untyped-def]
        return "http://acme.example.com/"  # http non-loopback — invalid

    app = create_a2a_server(_OkHandler(), name="test-agent", validation=None, public_url=resolver)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/.well-known/agent-card.json")

    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_async_resolver_works() -> None:
    """Async resolver coroutines are awaited and resolve the card correctly."""

    async def resolver(request) -> str:  # type: ignore[no-untyped-def]
        host = request.headers.get("host", "localhost")
        return f"https://{host}/"

    app = create_a2a_server(_OkHandler(), name="test-agent", validation=None, public_url=resolver)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/.well-known/agent-card.json",
                headers={"host": "async.example.com"},
            )

    assert resp.status_code == 200
    body = resp.json()
    interfaces = body.get("supportedInterfaces") or body.get("supported_interfaces", [])
    urls = [iface.get("url", "") for iface in interfaces]
    assert any("async.example.com" in u for u in urls), f"expected async.example.com in {urls}"


@pytest.mark.asyncio
async def test_static_public_url_unchanged() -> None:
    """Existing static ``public_url`` string behaviour is preserved."""
    app = create_a2a_server(
        _OkHandler(),
        name="test-agent",
        validation=None,
        public_url="https://agent.example.com/",
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/.well-known/agent-card.json")

    assert resp.status_code == 200
    body = resp.json()
    interfaces = body.get("supportedInterfaces") or body.get("supported_interfaces", [])
    urls = [iface.get("url", "") for iface in interfaces]
    assert any("agent.example.com" in u for u in urls), f"expected agent.example.com in {urls}"


@pytest.mark.asyncio
async def test_no_public_url_unchanged() -> None:
    """Existing ``public_url=None`` behaviour is preserved (localhost URL)."""
    app = create_a2a_server(_OkHandler(), name="test-agent", validation=None)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/.well-known/agent-card.json")

    assert resp.status_code == 200
    body = resp.json()
    assert "name" in body


def test_public_url_resolver_is_exported() -> None:
    """PublicUrlResolver is importable from adcp.server."""
    from adcp.server import PublicUrlResolver as ImportedResolver  # noqa: N814

    assert ImportedResolver is PublicUrlResolver
