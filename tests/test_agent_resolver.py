"""Unit tests for adcp.signing.agent_resolver."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from adcp.signing.agent_resolver import (
    AgentResolution,
    AgentResolutionFreshness,
    AgentResolverError,
    _find_agent_by_url,
    _norm_url,
    async_resolve_agent,
    resolve_agent,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_caps_http_response(brand_json_url: str | None) -> dict[str, Any]:
    """Build a minimal MCP tools/call HTTP response body for get_adcp_capabilities."""
    identity: dict[str, Any] = {}
    if brand_json_url is not None:
        identity["brand_json_url"] = brand_json_url
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "structuredContent": {
                "adcp": {"major_versions": [3]},
                "identity": identity,
            }
        },
    }


def _make_brand_json(agent_url: str, jwks_uri: str) -> dict[str, Any]:
    return {
        "agents": [
            {"type": "buying", "url": agent_url, "jwks_uri": jwks_uri},
        ]
    }


def _make_jwks() -> dict[str, Any]:
    return {
        "keys": [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": "dGVzdA==",
                "kid": "test-key-1",
                "use": "sig",
            }
        ]
    }


def _caps_client_factory(caps_response: dict[str, Any]) -> Any:
    """Return a _ClientFactory that returns the given capabilities response."""

    @asynccontextmanager  # type: ignore[arg-type]
    async def _factory(url: str):  # type: ignore[no-untyped-def]
        client = AsyncMock()

        async def _post(*args: Any, **kwargs: Any) -> httpx.Response:
            return httpx.Response(200, json=caps_response)

        client.post = _post
        yield client

    return _factory


# ---------------------------------------------------------------------------
# _norm_url
# ---------------------------------------------------------------------------


class TestNormUrl:
    def test_strips_default_https_port(self) -> None:
        assert _norm_url("https://example.com:443/mcp") == _norm_url("https://example.com/mcp")

    def test_strips_default_http_port(self) -> None:
        assert _norm_url("http://example.com:80/path") == _norm_url("http://example.com/path")

    def test_lowercases_host(self) -> None:
        assert _norm_url("https://BUYER.EXAMPLE.COM/mcp") == _norm_url(
            "https://buyer.example.com/mcp"
        )

    def test_preserves_non_default_port(self) -> None:
        result = _norm_url("https://example.com:8443/mcp")
        assert ":8443" in result

    def test_invalid_url_returns_raw(self) -> None:
        raw = "not-a-url"
        assert _norm_url(raw) == raw


# ---------------------------------------------------------------------------
# _find_agent_by_url
# ---------------------------------------------------------------------------


class TestFindAgentByUrl:
    def test_finds_matching_agent(self) -> None:
        brand_json: dict[str, Any] = {
            "agents": [
                {
                    "type": "buying",
                    "url": "https://buyer.example.com/mcp",
                    "jwks_uri": "https://buyer.example.com/.well-known/jwks.json",
                },
            ]
        }
        entry, jwks_uri = _find_agent_by_url(
            brand_json,
            "https://buyer.example.com/mcp",
            "https://brand.example.com/.well-known/brand.json",
        )
        assert entry["type"] == "buying"
        assert jwks_uri == "https://buyer.example.com/.well-known/jwks.json"

    def test_case_insensitive_host_match(self) -> None:
        brand_json: dict[str, Any] = {
            "agents": [
                {
                    "type": "buying",
                    "url": "https://BUYER.example.com/mcp",
                    "jwks_uri": "https://buyer.example.com/.well-known/jwks.json",
                },
            ]
        }
        entry, _ = _find_agent_by_url(
            brand_json,
            "https://buyer.example.com/mcp",
            "https://brand.example.com/.well-known/brand.json",
        )
        assert entry is not None

    def test_default_port_stripped_match(self) -> None:
        brand_json: dict[str, Any] = {
            "agents": [
                {
                    "type": "buying",
                    "url": "https://buyer.example.com:443/mcp",
                    "jwks_uri": "https://buyer.example.com/.well-known/jwks.json",
                },
            ]
        }
        entry, _ = _find_agent_by_url(
            brand_json,
            "https://buyer.example.com/mcp",
            "https://brand.example.com/.well-known/brand.json",
        )
        assert entry is not None

    def test_raises_when_not_found(self) -> None:
        brand_json: dict[str, Any] = {
            "agents": [{"type": "buying", "url": "https://other.example.com/mcp"}]
        }
        with pytest.raises(AgentResolverError) as exc_info:
            _find_agent_by_url(
                brand_json,
                "https://buyer.example.com/mcp",
                "https://brand.example.com/.well-known/brand.json",
            )
        assert exc_info.value.code == "brand_json_agent_not_found"

    def test_walks_portfolio_brands(self) -> None:
        brand_json: dict[str, Any] = {
            "house": {"agents": []},
            "brands": [
                {
                    "id": "brand-1",
                    "agents": [
                        {
                            "type": "buying",
                            "url": "https://buyer.brand1.com/mcp",
                            "jwks_uri": "https://buyer.brand1.com/.well-known/jwks.json",
                        },
                    ],
                }
            ],
        }
        entry, _ = _find_agent_by_url(
            brand_json,
            "https://buyer.brand1.com/mcp",
            "https://brand.example.com/.well-known/brand.json",
        )
        assert entry is not None

    def test_walks_house_agents_after_brands(self) -> None:
        brand_json: dict[str, Any] = {
            "house": {
                "agents": [
                    {
                        "type": "buying",
                        "url": "https://buyer.example.com/mcp",
                        "jwks_uri": "https://buyer.example.com/.well-known/jwks.json",
                    }
                ]
            },
            "brands": [{"id": "other", "agents": []}],
        }
        entry, _ = _find_agent_by_url(
            brand_json,
            "https://buyer.example.com/mcp",
            "https://brand.example.com/.well-known/brand.json",
        )
        assert entry is not None

    def test_fallback_well_known_jwks_when_no_jwks_uri(self) -> None:
        brand_json: dict[str, Any] = {
            "agents": [
                {"type": "buying", "url": "https://buyer.example.com/mcp"},
            ]
        }
        _, jwks_uri = _find_agent_by_url(
            brand_json,
            "https://buyer.example.com/mcp",
            "https://buyer.example.com/.well-known/brand.json",
        )
        assert jwks_uri == "https://buyer.example.com/.well-known/jwks.json"


# ---------------------------------------------------------------------------
# async_resolve_agent
# ---------------------------------------------------------------------------


class TestAsyncResolveAgent:
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        agent_url = "https://buyer.example.com/mcp"
        brand_json_url = "https://buyer.example.com/.well-known/brand.json"
        jwks_uri = "https://buyer.example.com/.well-known/jwks.json"
        brand_json = _make_brand_json(agent_url, jwks_uri)
        jwks = _make_jwks()
        caps = _make_caps_http_response(brand_json_url)

        from adcp.signing.brand_jwks import _FetchedBrandJson

        with (
            patch(
                "adcp.signing.agent_resolver._fetch_brand_json",
                return_value=_FetchedBrandJson(
                    status="ok",
                    final_url=brand_json_url,
                    data=brand_json,
                    etag=None,
                    cache_control="max-age=3600",
                ),
            ),
            patch("adcp.signing.agent_resolver.async_default_jwks_fetcher", return_value=jwks),
        ):
            result = await async_resolve_agent(
                agent_url, _client_factory=_caps_client_factory(caps)
            )

        assert isinstance(result, AgentResolution)
        assert result.agent_url == agent_url
        assert result.brand_json_url == brand_json_url
        assert result.jwks_uri == jwks_uri
        assert result.jwks == jwks
        assert isinstance(result.freshness, AgentResolutionFreshness)
        assert len(result.trace) == 3
        assert result.trace[0].label == "capabilities"
        assert result.trace[0].status == 200
        assert result.trace[1].label == "brand_json"
        assert result.trace[2].label == "jwks"

    @pytest.mark.asyncio
    async def test_missing_brand_json_url(self) -> None:
        agent_url = "https://buyer.example.com/mcp"
        caps = _make_caps_http_response(None)

        with pytest.raises(AgentResolverError) as exc_info:
            await async_resolve_agent(agent_url, _client_factory=_caps_client_factory(caps))
        assert exc_info.value.code == "brand_json_url_missing"

    @pytest.mark.asyncio
    async def test_capabilities_http_error_status(self) -> None:
        agent_url = "https://buyer.example.com/mcp"

        @asynccontextmanager  # type: ignore[arg-type]
        async def _factory(url: str):  # type: ignore[no-untyped-def]
            client = AsyncMock()

            async def _post(*a: Any, **kw: Any) -> httpx.Response:
                return httpx.Response(500, json={"error": "oops"})

            client.post = _post
            yield client

        with pytest.raises(AgentResolverError) as exc_info:
            await async_resolve_agent(agent_url, _client_factory=_factory)
        assert exc_info.value.code == "capability_fetch_failed"
        assert "500" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_capabilities_network_error(self) -> None:
        agent_url = "https://buyer.example.com/mcp"

        @asynccontextmanager  # type: ignore[arg-type]
        async def _factory(url: str):  # type: ignore[no-untyped-def]
            client = AsyncMock()

            async def _post(*a: Any, **kw: Any) -> httpx.Response:
                raise httpx.ConnectError("connection refused")

            client.post = _post
            yield client

        with pytest.raises(AgentResolverError) as exc_info:
            await async_resolve_agent(agent_url, _client_factory=_factory)
        assert exc_info.value.code == "capability_fetch_failed"

    @pytest.mark.asyncio
    async def test_capabilities_body_cap_exceeded(self) -> None:
        agent_url = "https://buyer.example.com/mcp"

        @asynccontextmanager  # type: ignore[arg-type]
        async def _factory(url: str):  # type: ignore[no-untyped-def]
            client = AsyncMock()

            async def _post(*a: Any, **kw: Any) -> httpx.Response:
                big = b"x" * 70000
                return httpx.Response(200, content=big)

            client.post = _post
            yield client

        with pytest.raises(AgentResolverError) as exc_info:
            await async_resolve_agent(
                agent_url, capabilities_body_cap=64 * 1024, _client_factory=_factory
            )
        assert exc_info.value.code == "capability_fetch_failed"

    @pytest.mark.asyncio
    async def test_agent_not_in_brand_json(self) -> None:
        agent_url = "https://buyer.example.com/mcp"
        brand_json_url = "https://buyer.example.com/.well-known/brand.json"
        caps = _make_caps_http_response(brand_json_url)
        brand_json_no_match: dict[str, Any] = {
            "agents": [
                {
                    "type": "buying",
                    "url": "https://other.com/mcp",
                    "jwks_uri": "https://other.com/.well-known/jwks.json",
                }
            ]
        }

        from adcp.signing.brand_jwks import _FetchedBrandJson

        with patch(
            "adcp.signing.agent_resolver._fetch_brand_json",
            return_value=_FetchedBrandJson(
                status="ok",
                final_url=brand_json_url,
                data=brand_json_no_match,
                etag=None,
                cache_control=None,
            ),
        ):
            with pytest.raises(AgentResolverError) as exc_info:
                await async_resolve_agent(agent_url, _client_factory=_caps_client_factory(caps))
        assert exc_info.value.code == "brand_json_agent_not_found"

    @pytest.mark.asyncio
    async def test_brand_json_fetch_error_wrapped(self) -> None:
        agent_url = "https://buyer.example.com/mcp"
        brand_json_url = "https://buyer.example.com/.well-known/brand.json"
        caps = _make_caps_http_response(brand_json_url)

        from adcp.signing.brand_jwks import BrandJsonResolverError

        with patch(
            "adcp.signing.agent_resolver._fetch_brand_json",
            side_effect=BrandJsonResolverError("fetch_failed", "network error"),
        ):
            with pytest.raises(AgentResolverError) as exc_info:
                await async_resolve_agent(agent_url, _client_factory=_caps_client_factory(caps))
        assert exc_info.value.code == "brand_json_fetch_failed"

    @pytest.mark.asyncio
    async def test_freshness_captures_cache_control(self) -> None:
        agent_url = "https://buyer.example.com/mcp"
        brand_json_url = "https://buyer.example.com/.well-known/brand.json"
        jwks_uri = "https://buyer.example.com/.well-known/jwks.json"
        brand_json = _make_brand_json(agent_url, jwks_uri)
        caps = _make_caps_http_response(brand_json_url)

        from adcp.signing.brand_jwks import _FetchedBrandJson

        with (
            patch(
                "adcp.signing.agent_resolver._fetch_brand_json",
                return_value=_FetchedBrandJson(
                    status="ok",
                    final_url=brand_json_url,
                    data=brand_json,
                    etag=None,
                    cache_control="max-age=1800",
                ),
            ),
            patch(
                "adcp.signing.agent_resolver.async_default_jwks_fetcher",
                return_value=_make_jwks(),
            ),
        ):
            result = await async_resolve_agent(
                agent_url, _client_factory=_caps_client_factory(caps)
            )

        assert result.freshness.cache_control == "max-age=1800"
        assert result.freshness.fetched_at <= time.time()

    @pytest.mark.asyncio
    async def test_brand_json_origin_mismatch_rejected(self) -> None:
        """brand_json_url on a different domain from agent_url is rejected before hop 2."""
        agent_url = "https://buyer.example.com/mcp"
        # evil.com is not same-origin or parent-domain of buyer.example.com
        bad_brand_json_url = "https://evil.com/.well-known/brand.json"
        caps = _make_caps_http_response(bad_brand_json_url)

        with pytest.raises(AgentResolverError) as exc_info:
            await async_resolve_agent(agent_url, _client_factory=_caps_client_factory(caps))
        assert exc_info.value.code == "brand_json_origin_mismatch"

    @pytest.mark.asyncio
    async def test_brand_json_parent_domain_accepted(self) -> None:
        """brand_json_url on a parent domain of agent_url is accepted."""
        agent_url = "https://buyer.example.com/mcp"
        brand_json_url = "https://example.com/.well-known/brand.json"
        jwks_uri = "https://buyer.example.com/.well-known/jwks.json"
        brand_json = _make_brand_json(agent_url, jwks_uri)
        caps = _make_caps_http_response(brand_json_url)

        from adcp.signing.brand_jwks import _FetchedBrandJson

        with (
            patch(
                "adcp.signing.agent_resolver._fetch_brand_json",
                return_value=_FetchedBrandJson(
                    status="ok",
                    final_url=brand_json_url,
                    data=brand_json,
                    etag=None,
                    cache_control=None,
                ),
            ),
            patch(
                "adcp.signing.agent_resolver.async_default_jwks_fetcher",
                return_value=_make_jwks(),
            ),
        ):
            result = await async_resolve_agent(
                agent_url, _client_factory=_caps_client_factory(caps)
            )

        assert result.brand_json_url == brand_json_url

    @pytest.mark.asyncio
    async def test_verify_from_agent_url(self) -> None:
        """verify_from_agent_url resolves keys then delegates to verify_request_signature."""
        from unittest.mock import MagicMock

        from adcp.signing.agent_resolver import verify_from_agent_url
        from adcp.signing.verifier import VerifiedSigner, VerifyOptions

        agent_url = "https://buyer.example.com/mcp"
        brand_json_url = "https://buyer.example.com/.well-known/brand.json"
        jwks_uri = "https://buyer.example.com/.well-known/jwks.json"
        brand_json = _make_brand_json(agent_url, jwks_uri)
        jwks = _make_jwks()
        caps = _make_caps_http_response(brand_json_url)

        fake_signer = MagicMock(spec=VerifiedSigner)

        from adcp.signing.brand_jwks import _FetchedBrandJson
        from adcp.signing.verifier import VerifierCapability

        base_options = VerifyOptions(
            now=0.0,
            capability=VerifierCapability(),
            operation="test_op",
            jwks_resolver=MagicMock(),
        )

        with (
            patch(
                "adcp.signing.agent_resolver._fetch_brand_json",
                return_value=_FetchedBrandJson(
                    status="ok",
                    final_url=brand_json_url,
                    data=brand_json,
                    etag=None,
                    cache_control=None,
                ),
            ),
            patch("adcp.signing.agent_resolver.async_default_jwks_fetcher", return_value=jwks),
            patch(
                "adcp.signing.verifier.verify_request_signature",
                return_value=fake_signer,
            ) as mock_verify,
        ):
            options = base_options
            result = await verify_from_agent_url(
                method="POST",
                url="https://seller.example.com/api",
                headers={"signature": "sig1=:abc:"},
                body=b"{}",
                agent_url=agent_url,
                options=options,
                _client_factory=_caps_client_factory(caps),
            )

        assert result is fake_signer
        # jwks_resolver in the call must be a StaticJwksResolver, not the original mock
        call_options = mock_verify.call_args.kwargs["options"]
        from adcp.signing.jwks import StaticJwksResolver

        assert isinstance(call_options.jwks_resolver, StaticJwksResolver)


# ---------------------------------------------------------------------------
# resolve_agent (sync wrapper)
# ---------------------------------------------------------------------------


class TestResolveAgent:
    def test_sync_wrapper_delegates_to_async(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """resolve_agent calls asyncio.run(async_resolve_agent(...))."""
        expected = AgentResolution(
            agent_url="https://buyer.example.com/mcp",
            brand_json_url="https://buyer.example.com/.well-known/brand.json",
            agent_entry={"type": "buying"},
            jwks_uri="https://buyer.example.com/.well-known/jwks.json",
            jwks={"keys": []},
            freshness=AgentResolutionFreshness(fetched_at=0.0),
        )

        async def _fake(url: str, **kwargs: Any) -> AgentResolution:
            return expected

        monkeypatch.setattr("adcp.signing.agent_resolver.async_resolve_agent", _fake)
        result = resolve_agent("https://buyer.example.com/mcp")
        assert result is expected


# ---------------------------------------------------------------------------
# AgentResolverError attributes
# ---------------------------------------------------------------------------


class TestAgentResolverError:
    def test_code_attribute(self) -> None:
        err = AgentResolverError("ssrf_blocked", "private IP")
        assert err.code == "ssrf_blocked"
        assert err.detail == "private IP"
        assert str(err) == "private IP"

    def test_is_exception(self) -> None:
        err = AgentResolverError("invalid_url", "bad url")
        assert isinstance(err, Exception)
