"""``async_resolve_agent`` — bootstrap from agent URL to JWK set.

Exercises the 3-hop walk: capabilities → brand.json → JWKS. Each hop
gets its own SSRF-pinned client; tests inject a shared
``_MockTransport`` that maps URLs to canned responses to verify
orchestration behavior (status codes, body parsing, error mapping,
trace shape) without standing up real HTTP.

Algorithm-level coverage of the brand.json walk + JWK lookup lives in
``test_brand_jwks.py`` and ``test_jwks.py``; this file pins the
resolver-orchestrator behavior on top of those tested primitives.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from adcp.signing import agent_resolver
from adcp.signing.agent_resolver import (
    AgentResolution,
    AgentResolverError,
    async_resolve_agent,
    resolve_agent,
)
from adcp.signing.brand_jwks import BrandJsonJwksResolver

# ---- Mock transport (shared across all 3 hops) ----


class _MockTransport(httpx.AsyncBaseTransport):
    """Maps URLs to canned ``(status, body, headers)`` triples. Each
    call is recorded for assertion.
    """

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        if url not in self.responses:
            return httpx.Response(404, content=b"")
        spec = self.responses[url]
        return httpx.Response(
            spec.get("status", 200),
            content=spec.get("body", b""),
            headers=spec.get("headers", {}),
        )


@pytest.fixture
def patch_resolver(monkeypatch: pytest.MonkeyPatch):
    """Wire a single ``_MockTransport`` into every hop of the resolver.

    Returns a callable ``patch(responses) -> (transport, factory)`` that:

    * patches ``BrandJsonJwksResolver.__init__`` to inject the factory,
    * patches ``async_default_jwks_fetcher`` at the resolver's import site,
    * returns ``factory`` so the test passes it as
      ``_capabilities_client_factory=factory`` to ``async_resolve_agent``.

    All three hops resolve through one transport. Tests assert
    against ``transport.calls`` and the returned :class:`AgentResolution`.
    """

    def _patch(
        responses: dict[str, dict[str, Any]],
    ) -> tuple[_MockTransport, Any]:
        transport = _MockTransport(responses)

        def factory(_url: str) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=transport,
                timeout=5.0,
                follow_redirects=False,
                trust_env=False,
            )

        # Hop 2: brand.json — patch BrandJsonJwksResolver.__init__ to
        # inject the factory under _client_factory.
        original_brand_init = BrandJsonJwksResolver.__init__

        def patched_brand_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            kwargs.setdefault("_client_factory", factory)
            return original_brand_init(self, *args, **kwargs)

        monkeypatch.setattr(BrandJsonJwksResolver, "__init__", patched_brand_init)

        # Hop 3: JWKS — replace the fetcher at the resolver's import site
        # (the resolver imports it by name from adcp.signing.jwks).
        async def mock_jwks_fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
            async with httpx.AsyncClient(
                transport=transport,
                timeout=5.0,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.get(uri, headers={"Accept": "application/json"})
                response.raise_for_status()
                body = response.json()
            if not isinstance(body, dict) or "keys" not in body:
                raise ValueError(f"JWKS document at {uri!r} has no 'keys' array")
            return body

        monkeypatch.setattr(
            "adcp.signing.agent_resolver.async_default_jwks_fetcher", mock_jwks_fetcher
        )
        return transport, factory

    return _patch


# ---- Helpers ----


def _capabilities_body(brand_json_url: str | None) -> bytes:
    identity: dict[str, Any] = {}
    if brand_json_url is not None:
        identity["brand_json_url"] = brand_json_url
    return json.dumps({"adcp_version": "3.0.5", "identity": identity}).encode()


def _brand_json_body(jwks_uri: str) -> bytes:
    return json.dumps(
        {
            "agents": [
                {
                    "type": "sales",
                    "url": "https://buyer.example.com/mcp",
                    "jwks_uri": jwks_uri,
                }
            ]
        }
    ).encode()


def _jwks_body() -> bytes:
    return json.dumps(
        {
            "keys": [
                {
                    "kty": "OKP",
                    "crv": "Ed25519",
                    "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
                    "kid": "test-key-1",
                    "alg": "EdDSA",
                }
            ]
        }
    ).encode()


# ---- Happy path ----


@pytest.mark.asyncio
async def test_resolve_returns_agent_resolution_with_full_trace(patch_resolver) -> None:
    """End-to-end: all 3 hops succeed → AgentResolution carries the
    expected URLs, the JWK set, and a 3-entry trace marked ok."""
    transport, factory = patch_resolver(
        {
            "https://buyer.example.com/mcp": {
                "body": _capabilities_body("https://example.com/.well-known/brand.json"),
                "headers": {"content-type": "application/json"},
            },
            "https://example.com/.well-known/brand.json": {
                "body": _brand_json_body("https://example.com/.well-known/jwks.json"),
                "headers": {"content-type": "application/json"},
            },
            "https://example.com/.well-known/jwks.json": {
                "body": _jwks_body(),
                "headers": {"content-type": "application/json"},
            },
        }
    )

    result = await async_resolve_agent(
        "https://buyer.example.com/mcp",
        agent_type="sales",
        _capabilities_client_factory=factory,
    )

    assert isinstance(result, AgentResolution)
    assert result.agent_url == "https://buyer.example.com/mcp"
    assert result.brand_json_url == "https://example.com/.well-known/brand.json"
    assert result.jwks_uri == "https://example.com/.well-known/jwks.json"
    assert result.jwks["keys"][0]["kid"] == "test-key-1"
    assert result.agent_entry["type"] == "sales"
    assert len(result.trace) == 3
    assert [t.hop for t in result.trace] == ["capabilities", "brand_json", "jwks"]
    assert all(t.status == "ok" for t in result.trace)
    assert all(t.latency_ms >= 0 for t in result.trace)
    # Verify each hop got hit exactly once
    assert len(transport.calls) == 3


# ---- Error paths ----


@pytest.mark.asyncio
async def test_resolve_raises_when_capabilities_unreachable(patch_resolver) -> None:
    """Capabilities returns 503 → AgentResolverError(capabilities_unreachable),
    trace records the error hop with code + message."""
    _, factory = patch_resolver(
        {
            "https://buyer.example.com/mcp": {
                "status": 503,
                "body": b"",
            },
        }
    )

    with pytest.raises(AgentResolverError) as exc:
        await async_resolve_agent(
            "https://buyer.example.com/mcp",
            agent_type="sales",
            _capabilities_client_factory=factory,
        )
    assert exc.value.code == "capabilities_unreachable"
    assert "503" in exc.value.message


@pytest.mark.asyncio
async def test_resolve_raises_when_capabilities_body_invalid_json(patch_resolver) -> None:
    """Capabilities returns 200 with non-JSON body → capabilities_invalid."""
    _, factory = patch_resolver(
        {
            "https://buyer.example.com/mcp": {
                "body": b"<html>not json</html>",
                "headers": {"content-type": "text/html"},
            },
        }
    )

    with pytest.raises(AgentResolverError) as exc:
        await async_resolve_agent(
            "https://buyer.example.com/mcp",
            agent_type="sales",
            _capabilities_client_factory=factory,
        )
    assert exc.value.code == "capabilities_invalid"


@pytest.mark.asyncio
async def test_resolve_raises_when_brand_json_url_missing(patch_resolver) -> None:
    """Capabilities response has no ``identity.brand_json_url`` →
    brand_json_url_missing. This is the gate on operators publishing
    adcp#3690.
    """
    _, factory = patch_resolver(
        {
            "https://buyer.example.com/mcp": {
                "body": _capabilities_body(brand_json_url=None),
                "headers": {"content-type": "application/json"},
            },
        }
    )

    with pytest.raises(AgentResolverError) as exc:
        await async_resolve_agent(
            "https://buyer.example.com/mcp",
            agent_type="sales",
            _capabilities_client_factory=factory,
        )
    assert exc.value.code == "brand_json_url_missing"


@pytest.mark.asyncio
async def test_resolve_raises_when_brand_json_unreachable(patch_resolver) -> None:
    """Capabilities OK, brand.json returns 404 →
    brand_json_resolution_failed (wraps the underlying
    BrandJsonResolverError code in the message).
    """
    _, factory = patch_resolver(
        {
            "https://buyer.example.com/mcp": {
                "body": _capabilities_body("https://example.com/.well-known/brand.json"),
                "headers": {"content-type": "application/json"},
            },
            # brand.json URL not in mock → 404
        }
    )

    with pytest.raises(AgentResolverError) as exc:
        await async_resolve_agent(
            "https://buyer.example.com/mcp",
            agent_type="sales",
            _capabilities_client_factory=factory,
        )
    assert exc.value.code == "brand_json_resolution_failed"


@pytest.mark.asyncio
async def test_resolve_raises_when_jwks_fetch_fails(patch_resolver) -> None:
    """Capabilities + brand.json succeed, JWKS endpoint returns 500 →
    jwks_fetch_failed.
    """
    _, factory = patch_resolver(
        {
            "https://buyer.example.com/mcp": {
                "body": _capabilities_body("https://example.com/.well-known/brand.json"),
                "headers": {"content-type": "application/json"},
            },
            "https://example.com/.well-known/brand.json": {
                "body": _brand_json_body("https://example.com/.well-known/jwks.json"),
                "headers": {"content-type": "application/json"},
            },
            "https://example.com/.well-known/jwks.json": {
                "status": 500,
                "body": b"",
            },
        }
    )

    with pytest.raises(AgentResolverError) as exc:
        await async_resolve_agent(
            "https://buyer.example.com/mcp",
            agent_type="sales",
            _capabilities_client_factory=factory,
        )
    assert exc.value.code == "jwks_fetch_failed"


# ---- Body cap (DoS guard) ----


@pytest.mark.asyncio
async def test_resolve_rejects_oversize_capabilities_body(patch_resolver) -> None:
    """Capabilities body exceeding ``max_capabilities_bytes`` is
    rejected before parse — DoS guard. Also confirms the cap actually
    fires at the configured limit (not at some baked-in default).
    """
    _, factory = patch_resolver(
        {
            "https://buyer.example.com/mcp": {
                "body": b"x" * 1024,
                "headers": {"content-type": "application/json"},
            },
        }
    )

    with pytest.raises(AgentResolverError) as exc:
        await async_resolve_agent(
            "https://buyer.example.com/mcp",
            agent_type="sales",
            max_capabilities_bytes=512,
            _capabilities_client_factory=factory,
        )
    assert exc.value.code == "capabilities_invalid"
    assert "exceeds" in exc.value.message


# ---- Sync wrapper ----


def test_sync_wrapper_dispatches_via_asyncio_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """``resolve_agent`` is the sync convenience wrapper for CLI /
    scripts. Spot-check that it dispatches through ``asyncio.run`` and
    returns the result of ``async_resolve_agent`` unchanged.

    Library code on an event loop should call ``async_resolve_agent``
    directly — wrapping in ``asyncio.run`` from inside a running loop
    would deadlock.
    """
    sentinel = AgentResolution(
        agent_url="https://buyer.example.com/mcp",
        brand_json_url="https://example.com/.well-known/brand.json",
        agent_entry={"type": "sales", "url": "https://x", "jwks_uri": "https://j"},
        jwks_uri="https://j",
        jwks={"keys": []},
        fetched_at=0.0,
        trace=[],
    )

    async def fake_async_resolve(*args, **kwargs):  # type: ignore[no-untyped-def]
        return sentinel

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_async_resolve)

    result = resolve_agent("https://buyer.example.com/mcp", agent_type="sales")
    assert result is sentinel


# ---- Forward-compat read of identity.brand_json_url ----


def test_extract_brand_json_url_reads_from_extra_fields() -> None:
    """``identity.brand_json_url`` is forward-compat on 3.0.5 (typed in
    3.1). The extractor reads it as a raw dict key, so it works whether
    the field is in the typed Pydantic surface or only in
    ``model_extra`` — pinning the read path so a future schema bump
    doesn't break this contract.
    """
    raw = {
        "adcp_version": "3.0.5",
        "identity": {
            "per_principal_key_isolation": True,
            "brand_json_url": "https://example.com/.well-known/brand.json",
        },
    }
    assert (
        agent_resolver._extract_brand_json_url(raw) == "https://example.com/.well-known/brand.json"
    )


def test_extract_brand_json_url_raises_when_identity_missing() -> None:
    with pytest.raises(AgentResolverError) as exc:
        agent_resolver._extract_brand_json_url({"adcp_version": "3.0.5"})
    assert exc.value.code == "brand_json_url_missing"


def test_extract_brand_json_url_raises_when_field_empty_string() -> None:
    with pytest.raises(AgentResolverError) as exc:
        agent_resolver._extract_brand_json_url({"identity": {"brand_json_url": ""}})
    assert exc.value.code == "brand_json_url_missing"
