"""Tests for :mod:`adcp.signing.brand_authz`.

Behavior under test (matches ADCP request-signing spec #3690):

* eTLD+1 binding: same-eTLD+1 agent ↔ brand pair authorizes.
* ``house.authorized_operators[]`` delegation: cross-eTLD+1 agent
  authorized when its host is a listed operator and the operator's
  ``brands[]`` scope covers the request.
* Listing requirement: agent_url MUST appear in some ``agents[]``
  array (top-level / house / brand-scoped).
* ``agent_type`` filter narrows the listing match without affecting
  the binding check.
* ``brand_id`` filter narrows the agents[] walk to that brand's
  array (plus house) and scopes the operator delegation check.
* Failure modes: agent not listed; agent listed under wrong type;
  binding failed (cross-eTLD+1 with no operator delegation);
  brand_domain invalid (IP literal / no public suffix); brand.json
  fetch error.
* Shared fetcher: :func:`build_brand_json_resolvers` returns a JWKS
  resolver and an authz resolver that share one fetch.
"""

from __future__ import annotations

import json

import httpx
import pytest

from adcp.signing.brand_authz import (
    BrandAuthorizationResolver,
    BrandJsonAuthorizationResolver,
    build_brand_json_resolvers,
)


class _MockTransport(httpx.AsyncBaseTransport):
    """Minimal async transport that returns canned responses keyed on URL.

    Records each call so tests can assert "only one fetch happened"
    when sharing the fetcher across two resolvers.
    """

    def __init__(self, responses: dict[str, dict]):
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


def _factory(transport: _MockTransport):
    def f(_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=transport,
            timeout=5.0,
            follow_redirects=False,
            trust_env=False,
        )

    return f


def _brand_json(body: dict) -> bytes:
    return json.dumps(body).encode("utf-8")


# ----- protocol conformance -----


def test_brand_json_resolver_satisfies_protocol() -> None:
    resolver = BrandJsonAuthorizationResolver("https://brand.example/.well-known/brand.json")
    assert isinstance(resolver, BrandAuthorizationResolver)


# ----- eTLD+1 binding (happy path) -----


@pytest.mark.asyncio
async def test_authz_etld1_match_authorizes_same_origin_agent() -> None:
    body = _brand_json(
        {
            "agents": [
                {
                    "type": "signals",
                    "id": "signals_main",
                    "url": "https://ads.brand.com/signals",
                }
            ]
        }
    )
    transport = _MockTransport(
        {"https://brand.com/.well-known/brand.json": {"body": body}},
    )
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    result = await resolver.check(
        agent_url="https://ads.brand.com/signals",
        brand_domain="brand.com",
    )

    assert result.authorized is True
    assert result.reason == "etld1_match"
    assert result.matched_agent_url == "https://ads.brand.com/signals"
    assert result.matched_agent_type == "signals"


@pytest.mark.asyncio
async def test_authz_etld1_match_with_subdomain_brand_url() -> None:
    body = _brand_json(
        {"agents": [{"type": "signals", "id": "x", "url": "https://api.brand.com/x"}]}
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    assert await resolver.is_authorized(
        agent_url="https://api.brand.com/x",
        brand_domain="https://www.brand.com/",
    )


# ----- agent_type filter -----


@pytest.mark.asyncio
async def test_authz_agent_type_filter_matches() -> None:
    body = _brand_json(
        {
            "agents": [
                {"type": "signals", "id": "s", "url": "https://ads.brand.com/agent"},
                {"type": "creative", "id": "c", "url": "https://ads.brand.com/agent"},
            ]
        }
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    result = await resolver.check(
        agent_url="https://ads.brand.com/agent",
        brand_domain="brand.com",
        agent_type="creative",
    )
    assert result.authorized
    assert result.matched_agent_type == "creative"


@pytest.mark.asyncio
async def test_authz_agent_type_mismatch_distinguished_from_not_listed() -> None:
    # URL is listed under "signals" but caller is asking for "creative".
    # We must distinguish the "wrong type" case from "not present at all"
    # so the verifier can emit a precise error.
    body = _brand_json(
        {"agents": [{"type": "signals", "id": "s", "url": "https://ads.brand.com/agent"}]}
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    result = await resolver.check(
        agent_url="https://ads.brand.com/agent",
        brand_domain="brand.com",
        agent_type="creative",
    )
    assert result.authorized is False
    assert result.reason == "agent_type_mismatch"


@pytest.mark.asyncio
async def test_authz_unlisted_agent_returns_agent_not_listed() -> None:
    body = _brand_json(
        {"agents": [{"type": "signals", "id": "s", "url": "https://ads.brand.com/agent"}]}
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    result = await resolver.check(
        agent_url="https://other.brand.com/agent",
        brand_domain="brand.com",
    )
    assert result.authorized is False
    assert result.reason == "agent_not_listed"


# ----- cross-eTLD+1 with no delegation: fails -----


@pytest.mark.asyncio
async def test_authz_cross_etld1_without_operator_fails_binding() -> None:
    # Agent host is wpp.com but brand is brand.com and there's no
    # authorized_operators[] entry. The agent IS listed in agents[]
    # (the brand acknowledges it exists) but cannot bind to the brand.
    body = _brand_json(
        {"agents": [{"type": "signals", "id": "s", "url": "https://wpp.com/brand/agent"}]}
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    result = await resolver.check(
        agent_url="https://wpp.com/brand/agent",
        brand_domain="brand.com",
    )
    assert result.authorized is False
    assert result.reason == "binding_failed"
    # matched_agent_url IS set: the agent matched the listing, just
    # not the binding. Verifier callers use this for log attribution.
    assert result.matched_agent_url == "https://wpp.com/brand/agent"


# ----- authorized_operators[] delegation -----


@pytest.mark.asyncio
async def test_authz_operator_delegation_with_wildcard_brands() -> None:
    body = _brand_json(
        {
            "house": {
                "agents": [
                    {"type": "signals", "id": "s", "url": "https://wpp.com/brand/agent"},
                ],
                "authorized_operators": [
                    {"domain": "wpp.com", "brands": ["*"]},
                ],
            },
            "brands": [{"id": "brand_one"}],
        }
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    result = await resolver.check(
        agent_url="https://wpp.com/brand/agent",
        brand_domain="brand.com",
    )
    assert result.authorized is True
    assert result.reason == "operator_delegation"
    assert result.matched_operator_domain == "wpp.com"


@pytest.mark.asyncio
async def test_authz_operator_delegation_scoped_brand_id_matches() -> None:
    body = _brand_json(
        {
            "house": {
                "agents": [
                    {"type": "signals", "id": "s", "url": "https://wpp.com/agent"},
                ],
                "authorized_operators": [
                    {"domain": "wpp.com", "brands": ["nike"]},
                ],
            },
            "brands": [{"id": "nike"}, {"id": "adidas"}],
        }
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    result = await resolver.check(
        agent_url="https://wpp.com/agent",
        brand_domain="brand.com",
        brand_id="nike",
    )
    assert result.authorized is True
    assert result.reason == "operator_delegation"


@pytest.mark.asyncio
async def test_authz_operator_delegation_scoped_brand_id_misses() -> None:
    # Operator is authorized for nike but caller passed brand_id=adidas.
    body = _brand_json(
        {
            "house": {
                "agents": [
                    {"type": "signals", "id": "s", "url": "https://wpp.com/agent"},
                ],
                "authorized_operators": [
                    {"domain": "wpp.com", "brands": ["nike"]},
                ],
            },
            "brands": [{"id": "nike"}, {"id": "adidas"}],
        }
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    result = await resolver.check(
        agent_url="https://wpp.com/agent",
        brand_domain="brand.com",
        brand_id="adidas",
    )
    assert result.authorized is False
    assert result.reason == "binding_failed"


@pytest.mark.asyncio
async def test_authz_operator_without_wildcard_fails_unscoped_request() -> None:
    # No brand_id from the caller AND operator is scoped to a specific
    # brand → fail closed. Without the brand context we cannot verify
    # which brand the operator is acting for.
    body = _brand_json(
        {
            "house": {
                "agents": [
                    {"type": "signals", "id": "s", "url": "https://wpp.com/agent"},
                ],
                "authorized_operators": [
                    {"domain": "wpp.com", "brands": ["nike"]},
                ],
            },
        }
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    result = await resolver.check(
        agent_url="https://wpp.com/agent",
        brand_domain="brand.com",
    )
    assert result.authorized is False
    assert result.reason == "binding_failed"


@pytest.mark.asyncio
async def test_authz_operator_etld1_compared_not_byte_equal() -> None:
    # Operator declared as ``wpp.com`` covers ``api.wpp.com`` — eTLD+1
    # equivalence, not byte-equal hostname. Matches the same posture
    # as the agent-to-brand binding.
    body = _brand_json(
        {
            "house": {
                "agents": [
                    {"type": "signals", "id": "s", "url": "https://api.wpp.com/agent"},
                ],
                "authorized_operators": [
                    {"domain": "wpp.com", "brands": ["*"]},
                ],
            },
        }
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    result = await resolver.check(
        agent_url="https://api.wpp.com/agent",
        brand_domain="brand.com",
    )
    assert result.authorized is True
    assert result.reason == "operator_delegation"


# ----- brand_id scopes the agents[] walk -----


@pytest.mark.asyncio
async def test_authz_brand_id_narrows_agents_walk() -> None:
    # Agent listed under brands[id=adidas] but caller passed
    # brand_id=nike. House agents and the requested brand's agents
    # are the only valid sources — adidas's agents are out of scope.
    body = _brand_json(
        {
            "house": {"agents": []},
            "brands": [
                {
                    "id": "nike",
                    "agents": [
                        {"type": "signals", "id": "n", "url": "https://nike.brand.com/agent"},
                    ],
                },
                {
                    "id": "adidas",
                    "agents": [
                        {"type": "signals", "id": "a", "url": "https://adidas.brand.com/agent"},
                    ],
                },
            ],
        }
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    # Right brand_id → match.
    assert await resolver.is_authorized(
        agent_url="https://nike.brand.com/agent",
        brand_domain="brand.com",
        brand_id="nike",
    )

    # Wrong brand_id → agent_not_listed (adidas's agent is invisible
    # when brand_id=nike).
    result = await resolver.check(
        agent_url="https://adidas.brand.com/agent",
        brand_domain="brand.com",
        brand_id="nike",
    )
    assert result.authorized is False
    assert result.reason == "agent_not_listed"


# ----- brand_domain validation -----


@pytest.mark.asyncio
async def test_authz_rejects_ip_literal_brand_domain() -> None:
    # An IP literal has no eTLD+1; binding cannot succeed.
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
    )

    result = await resolver.check(
        agent_url="https://ads.brand.com/x",
        brand_domain="192.0.2.1",
    )
    assert result.authorized is False
    assert result.reason == "brand_domain_invalid"


@pytest.mark.asyncio
async def test_authz_rejects_localhost_brand_domain() -> None:
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
    )

    result = await resolver.check(
        agent_url="https://ads.brand.com/x",
        brand_domain="localhost",
    )
    assert result.authorized is False
    assert result.reason == "brand_domain_invalid"


# ----- fetch errors -----


@pytest.mark.asyncio
async def test_authz_brand_json_404_returns_brand_json_unavailable() -> None:
    transport = _MockTransport({})  # 404 for everything
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    result = await resolver.check(
        agent_url="https://ads.brand.com/x",
        brand_domain="brand.com",
    )
    assert result.authorized is False
    assert result.reason == "brand_json_unavailable"
    assert result.fetch_error is not None


# ----- byte-equal agents[] matching (spec mandate) -----


@pytest.mark.asyncio
async def test_authz_trailing_slash_mismatch_fails_byte_equal() -> None:
    # Per ADCP #3690: agents[].url match MUST be byte-equal. A trailing
    # slash on the request side vs no trailing slash on the brand.json
    # side is a mismatch — operators must list the exact URL.
    body = _brand_json(
        {"agents": [{"type": "signals", "id": "s", "url": "https://ads.brand.com/agent"}]}
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    result = await resolver.check(
        agent_url="https://ads.brand.com/agent/",  # extra trailing slash
        brand_domain="brand.com",
    )
    assert result.authorized is False
    assert result.reason == "agent_not_listed"


@pytest.mark.asyncio
async def test_authz_case_mismatch_fails_byte_equal() -> None:
    # Scheme/host case differences are NOT canonicalized at this step.
    # The spec's rationale: operators must be deliberate about what
    # they list; a canonicalization-permissive match silently authorizes
    # URLs that drift from what the brand declared.
    body = _brand_json(
        {"agents": [{"type": "signals", "id": "s", "url": "https://ads.brand.com/agent"}]}
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    result = await resolver.check(
        agent_url="https://ADS.brand.com/agent",  # uppercase host
        brand_domain="brand.com",
    )
    assert result.authorized is False
    assert result.reason == "agent_not_listed"


@pytest.mark.asyncio
async def test_authz_duplicate_agents_entry_returns_ambiguous() -> None:
    # brand.json schema does NOT constrain agents[] to be unique-by-URL.
    # If an operator misconfigures with duplicate entries for the same
    # URL, fail closed rather than silently picking one — maps to
    # ``request_signature_brand_json_ambiguous`` at the framework boundary.
    body = _brand_json(
        {
            "agents": [
                {"type": "signals", "id": "a", "url": "https://ads.brand.com/agent"},
                {"type": "signals", "id": "b", "url": "https://ads.brand.com/agent"},
            ]
        }
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})
    resolver = BrandJsonAuthorizationResolver(
        "https://brand.com/.well-known/brand.json",
        _client_factory=_factory(transport),
    )

    result = await resolver.check(
        agent_url="https://ads.brand.com/agent",
        brand_domain="brand.com",
    )
    assert result.authorized is False
    assert result.reason == "agent_ambiguous"


# ----- shared-fetcher builder -----


@pytest.mark.asyncio
async def test_build_brand_json_resolvers_shares_one_fetch() -> None:
    # Both resolvers share one fetcher → ONE brand.json HTTP call
    # even when both consumers do work.
    body = _brand_json(
        {
            "agents": [
                {
                    "type": "signals",
                    "id": "s",
                    "url": "https://ads.brand.com/agent",
                    "jwks_uri": "https://ads.brand.com/.well-known/jwks.json",
                }
            ]
        }
    )
    transport = _MockTransport({"https://brand.com/.well-known/brand.json": {"body": body}})

    jwks, authz = build_brand_json_resolvers(
        "https://brand.com/.well-known/brand.json",
        agent_type="signals",
    )
    # Inject the test transport into both via the shared fetcher.
    # The builder doesn't expose that seam publicly (by design), so
    # we monkey-patch the private fetcher's factory.
    jwks._fetcher._client_factory = _factory(transport)  # type: ignore[attr-defined]

    # First call: cold cache, one fetch.
    assert await authz.is_authorized(
        agent_url="https://ads.brand.com/agent",
        brand_domain="brand.com",
    )

    # Cooldown blocks a second fetch even if the JWKS resolver also
    # walks brand.json — but the relevant assertion is that the authz
    # call alone did not trigger a duplicate brand.json fetch from the
    # JWKS-side construction. ONE fetch total at this point.
    brand_json_calls = [c for c in transport.calls if "brand.json" in str(c.url)]
    assert len(brand_json_calls) == 1
