"""``verify_from_agent_url`` — single-call resolver+verifier factory.

Tests the orchestration layer: resolver feeds a JWK set to a
:class:`StaticJwksResolver`, that JWKS resolver feeds the existing
:func:`verify_starlette_request` verifier, errors map cleanly between
the two hierarchies. Happy-path JWKS verification is covered
end-to-end in ``tests/conformance/signing/test_e2e_fastapi.py``; this
file focuses on the factory's value-add (composition + error mapping).
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.signing import agent_resolver
from adcp.signing.agent_resolver import (
    AgentResolution,
    AgentResolverError,
    verify_from_agent_url,
)
from adcp.signing.errors import (
    REQUEST_SIGNATURE_JWKS_UNAVAILABLE,
    REQUEST_SIGNATURE_JWKS_UNTRUSTED,
    SignatureVerificationError,
)

# ---- Test seams ----


class _FakeStarletteRequest:
    """Duck-types just enough of Starlette's Request for
    :func:`verify_starlette_request`."""

    def __init__(
        self,
        *,
        method: str = "POST",
        url: str = "https://seller.example.com/mcp",
        headers=None,
        body: bytes = b"",
    ) -> None:
        self.method = method
        self.url = url
        self.headers = headers or {}
        self._body = body

    async def body(self) -> bytes:
        return self._body


_RESOLVED_AGENT = AgentResolution(
    agent_url="https://buyer.example.com/mcp",
    brand_json_url="https://example.com/.well-known/brand.json",
    agent_entry={
        "type": "sales",
        "url": "https://buyer.example.com/mcp",
        "jwks_uri": "https://example.com/.well-known/jwks.json",
    },
    jwks_uri="https://example.com/.well-known/jwks.json",
    jwks={"keys": []},
    fetched_at=0.0,
    trace=[],
)


# ---- Happy path: resolver feeds verifier ----


@pytest.mark.asyncio
async def test_factory_passes_resolved_jwks_to_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the resolver returns a JWK set, the verifier is constructed
    with a :class:`StaticJwksResolver` over those keys, the resolved
    agent_url is forwarded as ``options.agent_url``, and the verifier's
    return value flows back through unchanged. Pins the wiring so a
    refactor of the inner ``VerifyOptions`` shape can't silently drop
    the JWKS.
    """
    seen: dict[str, Any] = {}

    async def fake_resolve(*args, **kwargs):
        return _RESOLVED_AGENT

    async def fake_verify_starlette(request, *, options):  # type: ignore[no-untyped-def]
        seen["options"] = options
        seen["request"] = request
        return "verified-signer-sentinel"

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)
    monkeypatch.setattr("adcp.signing.middleware.verify_starlette_request", fake_verify_starlette)

    request = _FakeStarletteRequest()
    result = await verify_from_agent_url(
        request,
        "https://buyer.example.com/mcp",
        agent_type="sales",
        operation="get_products",
    )
    assert result == "verified-signer-sentinel"
    assert seen["options"].operation == "get_products"
    assert seen["options"].agent_url == "https://buyer.example.com/mcp"
    # JWKS resolver constructed from the resolution's jwks set.
    assert seen["options"].jwks_resolver is not None


# ---- Resolver failure mapping ----


@pytest.mark.asyncio
async def test_factory_maps_capabilities_unreachable_to_jwks_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``capabilities_unreachable`` is a discovery-time failure; verifier
    callers should see ``REQUEST_SIGNATURE_JWKS_UNAVAILABLE`` and emit
    a 401 with that code on the WWW-Authenticate header. The original
    :class:`AgentResolverError` chains via ``__cause__`` so adopters
    can drill into the resolver-side code if needed.
    """

    async def fake_resolve(*args, **kwargs):
        raise AgentResolverError("capabilities_unreachable", "503")

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)

    with pytest.raises(SignatureVerificationError) as exc:
        await verify_from_agent_url(
            _FakeStarletteRequest(),
            "https://buyer.example.com/mcp",
            agent_type="sales",
            operation="get_products",
        )
    assert exc.value.code == REQUEST_SIGNATURE_JWKS_UNAVAILABLE
    assert exc.value.step == "resolve"
    assert isinstance(exc.value.__cause__, AgentResolverError)
    assert exc.value.__cause__.code == "capabilities_unreachable"


@pytest.mark.asyncio
async def test_factory_maps_invalid_agent_url_to_jwks_untrusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``invalid_agent_url`` is a trust-boundary rejection (URL didn't
    canonicalize / scheme banned / SSRF-banned host) — the verifier-side
    semantic is ``JWKS_UNTRUSTED``, NOT ``JWKS_UNAVAILABLE``. Pins
    this discrimination so a refactor of the mapping table doesn't
    silently downgrade the security signal.
    """

    async def fake_resolve(*args, **kwargs):
        raise AgentResolverError("invalid_agent_url", "scheme: http not https")

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)

    with pytest.raises(SignatureVerificationError) as exc:
        await verify_from_agent_url(
            _FakeStarletteRequest(),
            "http://buyer.example.com/mcp",
            agent_type="sales",
            operation="get_products",
        )
    assert exc.value.code == REQUEST_SIGNATURE_JWKS_UNTRUSTED


@pytest.mark.asyncio
async def test_factory_maps_brand_json_failure_to_jwks_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """brand.json walk failure is also a discovery-time issue —
    verifier sees JWKS_UNAVAILABLE so the receiver can retry / fail
    gracefully without treating the buyer as adversarial."""

    async def fake_resolve(*args, **kwargs):
        raise AgentResolverError(
            "brand_json_resolution_failed",
            "brand.json resolution failed: fetch_failed: HTTP 404",
        )

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)

    with pytest.raises(SignatureVerificationError) as exc:
        await verify_from_agent_url(
            _FakeStarletteRequest(),
            "https://buyer.example.com/mcp",
            agent_type="sales",
            operation="get_products",
        )
    assert exc.value.code == REQUEST_SIGNATURE_JWKS_UNAVAILABLE


# ---- Verifier failure passes through ----


@pytest.mark.asyncio
async def test_factory_passes_verifier_errors_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the resolver succeeds but the verifier rejects (e.g.
    missing Signature header), the original
    :class:`SignatureVerificationError` propagates with its spec code
    intact — the factory does NOT remap verifier-side failures."""

    async def fake_resolve(*args, **kwargs):
        return _RESOLVED_AGENT

    async def fake_verify_starlette(request, *, options):  # type: ignore[no-untyped-def]
        raise SignatureVerificationError(
            "request_signature_required",
            step=0,
            message="Signature header missing",
        )

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)
    monkeypatch.setattr("adcp.signing.middleware.verify_starlette_request", fake_verify_starlette)

    with pytest.raises(SignatureVerificationError) as exc:
        await verify_from_agent_url(
            _FakeStarletteRequest(),
            "https://buyer.example.com/mcp",
            agent_type="sales",
            operation="get_products",
        )
    assert exc.value.code == "request_signature_required"
    assert exc.value.step == 0
