"""8.1 compatibility: brand.json key selection without ``agent_url``.

Omitting ``agent_url`` is deprecated. The fallback keeps the 8.0 selection
scope but fails closed on ambiguity and restores the same-origin guard for a
defaulted JWKS. Discovery always selects by the caller's agent URL, so the
cross-tenant impersonation fix holds in every mode.
"""

from __future__ import annotations

import warnings
from typing import Any

import httpx
import pytest

from adcp.signing import agent_resolver
from adcp.signing.agent_resolver import (
    AgentResolution,
    AgentResolverError,
    verify_from_agent_url,
)
from adcp.signing.brand_authz import build_brand_json_resolvers
from adcp.signing.brand_jwks import (
    BrandJsonJwksResolver,
    BrandJsonResolverError,
    _select_agent_without_url,
)
from adcp.signing.errors import (
    REQUEST_SIGNATURE_AGENT_NOT_IN_BRAND_JSON,
    SignatureVerificationError,
)
from tests.test_agent_resolution_33 import AGENT, resolve_record

BRAND_URL = "https://brand.example/.well-known/brand.json"
VICTIM = "https://victim.example/mcp"
JWK = {"kty": "OKP", "crv": "Ed25519", "x": "abc", "kid": "k1"}


def _resolver_for(document: dict[str, Any], **kwargs: Any) -> BrandJsonJwksResolver:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == BRAND_URL:
            return httpx.Response(200, json=document)
        return httpx.Response(404)

    def factory(_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fetch_keys(uri: str, **_kwargs: Any) -> dict[str, Any]:
        return {"keys": [JWK]}

    return BrandJsonJwksResolver(
        BRAND_URL, _client_factory=factory, jwks_fetcher=fetch_keys, **kwargs
    )


# ----- deprecation and construction -----


def test_missing_agent_url_warns_and_requires_agent_type() -> None:
    with pytest.warns(DeprecationWarning, match="without agent_url is deprecated"):
        BrandJsonJwksResolver(BRAND_URL, agent_type="sales")
    with pytest.raises(TypeError, match="requires agent_url"):
        BrandJsonJwksResolver(BRAND_URL)


def test_agent_url_path_does_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        BrandJsonJwksResolver(BRAND_URL, agent_url="https://brand.example/mcp")


def test_builder_without_agent_url_warns_once() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build_brand_json_resolvers(BRAND_URL, agent_type="sales")
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1
    assert "build_brand_json_resolvers" in str(deprecations[0].message)


# ----- unambiguous fallback works -----


@pytest.mark.asyncio
async def test_unambiguous_fallback_resolves_keys_with_warning() -> None:
    document = {
        "agents": [
            {"type": "sales", "url": "https://brand.example/mcp", "jwks_uri": "https://k/j"},
            {"type": "governance", "url": "https://gov.example/", "jwks_uri": "https://g/j"},
        ]
    }
    with pytest.warns(DeprecationWarning):
        resolver = _resolver_for(document, agent_type="sales")
    assert await resolver.resolve("k1") == JWK
    assert resolver.jwks_uri == "https://k/j"
    assert resolver.agent_url == "https://brand.example/mcp"


def test_fallback_defaults_jwks_on_matching_origin() -> None:
    selected = _select_agent_without_url(
        {"agents": [{"type": "sales", "url": "https://brand.example/mcp"}]},
        BRAND_URL,
        agent_type="sales",
    )
    assert selected.jwks_uri == "https://brand.example/.well-known/jwks.json"


def test_fallback_keeps_8_0_portfolio_scope() -> None:
    document = {
        "house": {"agents": [{"type": "sales", "url": "https://house/", "jwks_uri": "https://h/"}]},
        "brands": [
            {
                "id": "nike",
                "agents": [{"type": "sales", "url": "https://nike/", "jwks_uri": "https://n/"}],
            },
            {"id": "puma", "agents": [{"type": "creative", "url": "https://p/"}]},
        ],
    }
    assert (
        _select_agent_without_url(document, BRAND_URL, agent_type="sales").url == "https://house/"
    )
    assert (
        _select_agent_without_url(document, BRAND_URL, agent_type="sales", brand_id="nike").url
        == "https://nike/"
    )
    # A brand without a matching role falls back to the house, as in 8.0.
    assert (
        _select_agent_without_url(document, BRAND_URL, agent_type="sales", brand_id="puma").url
        == "https://house/"
    )


# ----- ambiguous fallback fails closed -----


@pytest.mark.parametrize("agent_id", [None, "dup"])
def test_ambiguous_fallback_fails_closed(agent_id: str | None) -> None:
    document = {
        "agents": [
            {"type": "sales", "id": "dup", "url": "https://a.example/", "jwks_uri": "https://a/"},
            {"type": "sales", "id": "dup", "url": "https://b.example/", "jwks_uri": "https://b/"},
        ]
    }
    with pytest.raises(BrandJsonResolverError) as exc:
        _select_agent_without_url(document, BRAND_URL, agent_type="sales", agent_id=agent_id)
    assert exc.value.code == "agent_ambiguous"
    assert "pass agent_url" in str(exc.value)


@pytest.mark.asyncio
async def test_ambiguous_fallback_resolver_returns_no_key() -> None:
    document = {
        "agents": [
            {"type": "sales", "url": "https://a.example/", "jwks_uri": "https://a/"},
            {"type": "sales", "url": "https://b.example/", "jwks_uri": "https://b/"},
        ]
    }
    with pytest.warns(DeprecationWarning):
        resolver = _resolver_for(document, agent_type="sales")
    with pytest.raises(BrandJsonResolverError) as exc:
        await resolver.resolve("k1")
    assert exc.value.code == "agent_ambiguous"
    assert resolver.jwks_uri is None


def test_missing_role_fails_closed() -> None:
    with pytest.raises(BrandJsonResolverError) as exc:
        _select_agent_without_url({"agents": []}, BRAND_URL, agent_type="sales")
    assert exc.value.code == "agent_not_found"


# ----- restored same-origin guard on the fallback path -----


@pytest.mark.asyncio
async def test_fallback_rejects_cross_origin_default_jwks() -> None:
    """An attacker-controlled brand.json naming a victim origin without
    ``jwks_uri`` must not make the victim origin's JWKS authoritative."""
    document = {"agents": [{"type": "sales", "url": "https://victim-internal.example/"}]}
    with pytest.raises(BrandJsonResolverError) as exc:
        _select_agent_without_url(document, BRAND_URL, agent_type="sales")
    assert exc.value.code == "jwks_origin_mismatch"
    with pytest.warns(DeprecationWarning):
        resolver = _resolver_for(document, agent_type="sales")
    with pytest.raises(BrandJsonResolverError) as resolved:
        await resolver.resolve("k1")
    assert resolved.value.code == "jwks_origin_mismatch"


def test_origin_guard_canonicalizes_both_sides() -> None:
    selected = _select_agent_without_url(
        {"agents": [{"type": "sales", "url": "HTTPS://Brand.Example:443/mcp"}]},
        BRAND_URL,
        agent_type="sales",
    )
    assert selected.jwks_uri == "https://brand.example/.well-known/jwks.json"


# ----- impersonation stays rejected with and without agent_url -----


@pytest.mark.asyncio
async def test_discovery_never_selects_a_listed_victim_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attacker's own record lists the victim's URL with attacker keys.
    Discovery selects by the requested URL, so the victim entry never matches."""
    record = {"agents": [{"type": "sales", "url": VICTIM, "jwks_uri": "https://example.com/k"}]}
    with pytest.raises(AgentResolverError) as exc:
        await resolve_record(monkeypatch, record)
    assert exc.value.signature_code == REQUEST_SIGNATURE_AGENT_NOT_IN_BRAND_JSON


@pytest.mark.asyncio
async def test_verifier_identity_never_comes_from_the_brand_json_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a resolution whose entry names another URL cannot become that
    signer: the factory fails closed instead of verifying as the victim."""
    resolution = AgentResolution(
        agent_url=AGENT,
        brand_json_url="https://example.com/.well-known/brand.json",
        agent_entry={"type": "sales", "url": VICTIM, "jwks_uri": "https://example.com/k"},
        jwks_uri="https://example.com/k",
        jwks={"keys": []},
        fetched_at=0.0,
        trace=[],
    )

    async def fake_resolve(*args: Any, **kwargs: Any) -> AgentResolution:
        return resolution

    async def fail_verify(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not verify against a mismatched entry")

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)
    monkeypatch.setattr("adcp.signing.middleware.verify_starlette_request", fail_verify)
    with pytest.raises(SignatureVerificationError) as exc:
        await verify_from_agent_url(object(), AGENT, operation="get_products")
    assert exc.value.code == REQUEST_SIGNATURE_AGENT_NOT_IN_BRAND_JSON


@pytest.mark.asyncio
async def test_fallback_cannot_pivot_to_a_victim_origin_or_choose_between_agents() -> None:
    """Without agent_url, a record adding a victim entry either pivots trust
    (rejected by the origin guard) or makes the role ambiguous (fail closed)."""
    pivot = {"agents": [{"type": "sales", "url": VICTIM}]}
    with pytest.raises(BrandJsonResolverError) as exc:
        _select_agent_without_url(pivot, BRAND_URL, agent_type="sales")
    assert exc.value.code == "jwks_origin_mismatch"
    crowded = {
        "agents": [
            {"type": "sales", "url": "https://brand.example/mcp"},
            {"type": "sales", "url": VICTIM, "jwks_uri": "https://brand.example/evil"},
        ]
    }
    with pytest.raises(BrandJsonResolverError) as ambiguous:
        _select_agent_without_url(crowded, BRAND_URL, agent_type="sales")
    assert ambiguous.value.code == "agent_ambiguous"
