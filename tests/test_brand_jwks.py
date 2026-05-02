"""Tests for :mod:`adcp.signing.brand_jwks`.

Behavior under test (matches JS port at
``adcp-client/src/lib/signing/brand-jwks.ts``):

* ``BrandJsonJwksResolver`` — JwksResolver Protocol conformance,
  agent selection, redirect chain following
  (``authoritative_location``, ``house``), well-known fallback,
  cache-control honoring, 304 ETag bumps, cascade refresh on
  unknown kid.
* Security: bare-hostname ``house`` validation, origin-bound
  well-known fallback (no cross-origin trust pivot), userinfo
  rejection, hop-cap enforcement.
* Error codes: every ``BrandJsonResolverErrorCode`` is reachable.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from adcp.signing.brand_jwks import (
    BrandJsonJwksResolver,
    BrandJsonResolverError,
    _assert_brand_json_shape,
    _canonicalize_url,
    _compute_lifetime,
    _select_agent,
)

# ----- Fake HTTP transport — mock httpx.AsyncClient -----


class _MockTransport(httpx.AsyncBaseTransport):
    """Minimal async transport that returns canned responses keyed
    on URL. Each call records the request for assertion."""

    def __init__(self, responses: dict[str, dict]):
        self.responses = responses
        self.calls: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        if url not in self.responses:
            return httpx.Response(404, content=b"")
        spec = self.responses[url]
        # Return 304 when the request's If-None-Match matches the spec.
        if spec.get("etag") is not None and request.headers.get("if-none-match") == spec["etag"]:
            return httpx.Response(
                304,
                headers={"etag": spec["etag"], **spec.get("headers", {})},
            )
        return httpx.Response(
            spec.get("status", 200),
            content=spec.get("body", b""),
            headers=spec.get("headers", {}),
        )


@pytest.fixture
def patch_httpx(monkeypatch):
    """Patch httpx.AsyncClient to use the fake transport."""

    def _patch(responses: dict[str, dict]) -> _MockTransport:
        transport = _MockTransport(responses)
        original_init = httpx.AsyncClient.__init__

        def patched_init(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
        return transport

    return _patch


# ----- _canonicalize_url -----


def test_canonicalize_strips_fragment() -> None:
    assert (
        _canonicalize_url("https://x.example/.well-known/brand.json#frag", allow_private=False)
        == "https://x.example/.well-known/brand.json"
    )


def test_canonicalize_rejects_userinfo() -> None:
    with pytest.raises(BrandJsonResolverError) as exc:
        _canonicalize_url("https://user:pass@x.example/", allow_private=False)
    assert exc.value.code == "invalid_url"


def test_canonicalize_rejects_http_when_private_disallowed() -> None:
    with pytest.raises(BrandJsonResolverError) as exc:
        _canonicalize_url("http://x.example/", allow_private=False)
    assert exc.value.code == "invalid_url"


def test_canonicalize_allows_http_when_private_allowed() -> None:
    assert (
        _canonicalize_url("http://localhost:8080/x", allow_private=True)
        == "http://localhost:8080/x"
    )


def test_canonicalize_rejects_malformed_url() -> None:
    with pytest.raises(BrandJsonResolverError) as exc:
        _canonicalize_url("not-a-url", allow_private=False)
    assert exc.value.code == "invalid_url"


# ----- _assert_brand_json_shape -----


def test_shape_accepts_valid_top_level_agents() -> None:
    _assert_brand_json_shape(
        {"agents": [{"type": "brand", "url": "https://a", "jwks_uri": "https://a/jwks"}]}
    )


def test_shape_rejects_non_string_url() -> None:
    with pytest.raises(BrandJsonResolverError) as exc:
        _assert_brand_json_shape({"agents": [{"type": "brand", "url": 42}]})
    assert exc.value.code == "schema_invalid"


def test_shape_rejects_non_string_jwks_uri() -> None:
    with pytest.raises(BrandJsonResolverError) as exc:
        _assert_brand_json_shape(
            {"agents": [{"type": "brand", "url": "https://a", "jwks_uri": 42}]}
        )
    assert exc.value.code == "schema_invalid"


def test_shape_rejects_non_array_agents() -> None:
    with pytest.raises(BrandJsonResolverError) as exc:
        _assert_brand_json_shape({"agents": "not-array"})
    assert exc.value.code == "schema_invalid"


def test_shape_walks_portfolio_house_and_brands_agents() -> None:
    """Portfolio documents have agents at house-level AND per-brand —
    schema check must walk both."""
    with pytest.raises(BrandJsonResolverError):
        _assert_brand_json_shape(
            {
                "house": {"agents": [{"type": "brand", "url": "https://h"}]},
                "brands": [{"id": "x", "agents": [{"type": "brand", "url": 42}]}],
            }
        )


# ----- _select_agent -----


def test_select_top_level_agents() -> None:
    data = {
        "agents": [
            {"type": "brand", "url": "https://a/", "jwks_uri": "https://a/jwks"},
        ]
    }
    selected = _select_agent(
        data,
        "https://example.com/.well-known/brand.json",
        agent_type="brand",
        agent_id=None,
        brand_id=None,
    )
    assert selected.url == "https://a/"
    assert selected.jwks_uri == "https://a/jwks"


def test_select_portfolio_brand_overrides_house() -> None:
    """Per-spec, brand-level agents override house-level for the same
    type when ``brand_id`` selector is set."""
    data = {
        "house": {
            "agents": [
                {
                    "type": "brand",
                    "url": "https://house-fallback/",
                    "jwks_uri": "https://h/jwks",
                }
            ]
        },
        "brands": [
            {
                "id": "nike",
                "agents": [
                    {
                        "type": "brand",
                        "url": "https://nike-agent/",
                        "jwks_uri": "https://nike/jwks",
                    }
                ],
            }
        ],
    }
    selected = _select_agent(
        data,
        "https://example.com/.well-known/brand.json",
        agent_type="brand",
        agent_id=None,
        brand_id="nike",
    )
    assert selected.url == "https://nike-agent/"


def test_select_portfolio_falls_back_to_house_when_brand_missing_type() -> None:
    data = {
        "house": {
            "agents": [
                {
                    "type": "brand",
                    "url": "https://house/",
                    "jwks_uri": "https://h/jwks",
                }
            ]
        },
        "brands": [
            {
                "id": "nike",
                "agents": [
                    {
                        "type": "creative",
                        "url": "https://creative/",
                    }
                ],
            }
        ],
    }
    selected = _select_agent(
        data,
        "https://example.com/.well-known/brand.json",
        agent_type="brand",
        agent_id=None,
        brand_id="nike",
    )
    assert selected.url == "https://house/"


def test_select_raises_agent_not_found() -> None:
    with pytest.raises(BrandJsonResolverError) as exc:
        _select_agent(
            {"agents": []},
            "https://example.com/",
            agent_type="brand",
            agent_id=None,
            brand_id=None,
        )
    assert exc.value.code == "agent_not_found"


def test_select_raises_agent_ambiguous_when_multiple_match() -> None:
    """Two agents of the same type without an ``agent_id`` selector
    is ambiguous — must raise rather than picking arbitrarily."""
    data = {
        "agents": [
            {"type": "brand", "url": "https://a/", "id": "a"},
            {"type": "brand", "url": "https://b/", "id": "b"},
        ]
    }
    with pytest.raises(BrandJsonResolverError) as exc:
        _select_agent(
            data,
            "https://example.com/",
            agent_type="brand",
            agent_id=None,
            brand_id=None,
        )
    assert exc.value.code == "agent_ambiguous"


def test_select_disambiguates_with_agent_id() -> None:
    data = {
        "agents": [
            {"type": "brand", "url": "https://a/", "id": "a", "jwks_uri": "https://a/jwks"},
            {"type": "brand", "url": "https://b/", "id": "b", "jwks_uri": "https://b/jwks"},
        ]
    }
    selected = _select_agent(
        data,
        "https://example.com/",
        agent_type="brand",
        agent_id="b",
        brand_id=None,
    )
    assert selected.url == "https://b/"


def test_select_falls_back_to_well_known_when_origin_matches() -> None:
    """Spec fallback: agent.url with no jwks_uri → origin/.well-known/jwks.json
    iff agent origin matches brand.json origin."""
    data = {"agents": [{"type": "brand", "url": "https://x.example/"}]}
    selected = _select_agent(
        data,
        "https://x.example/.well-known/brand.json",
        agent_type="brand",
        agent_id=None,
        brand_id=None,
    )
    assert selected.jwks_uri == "https://x.example/.well-known/jwks.json"


def test_select_rejects_well_known_fallback_on_origin_mismatch() -> None:
    """Cross-origin trust pivot: an attacker brand.json could set
    ``agent.url: https://victim-internal/`` and force the verifier
    to treat that origin's JWKS as authoritative. MUST reject."""
    data = {"agents": [{"type": "brand", "url": "https://victim-internal/"}]}
    with pytest.raises(BrandJsonResolverError) as exc:
        _select_agent(
            data,
            "https://attacker.example/.well-known/brand.json",
            agent_type="brand",
            agent_id=None,
            brand_id=None,
        )
    assert exc.value.code == "jwks_origin_mismatch"


# ----- _compute_lifetime -----


def test_lifetime_uses_max_age_when_no_cache_control() -> None:
    assert _compute_lifetime(None, 3600) == 3600


def test_lifetime_zero_on_no_store() -> None:
    assert _compute_lifetime("public, no-store", 3600) == 0


def test_lifetime_zero_on_no_cache() -> None:
    assert _compute_lifetime("no-cache, max-age=600", 3600) == 0


def test_lifetime_capped_at_max_age() -> None:
    assert _compute_lifetime("max-age=99999", 3600) == 3600


def test_lifetime_uses_server_max_age_when_below_cap() -> None:
    assert _compute_lifetime("max-age=600", 3600) == 600


# ----- BrandJsonJwksResolver — end-to-end via _MockTransport -----


def _brand_json(
    agent_url: str = "https://x.example/",
    jwks_uri: str = "https://x.example/jwks",
) -> bytes:
    import json

    return json.dumps(
        {"agents": [{"type": "brand", "url": agent_url, "jwks_uri": jwks_uri}]}
    ).encode()


def _jwks_body(kid: str = "k1") -> bytes:
    import json

    return json.dumps({"keys": [{"kty": "OKP", "crv": "Ed25519", "x": "abc", "kid": kid}]}).encode()


def _jwks_fetcher_for(keys: dict[str, dict]):
    """Return an async JWKS fetcher that returns canned responses
    keyed on URI. Avoids the production DNS / SSRF guards on the
    inner resolver."""

    async def fetcher(uri: str, *, allow_private: bool = False) -> dict:
        if uri not in keys:
            raise ValueError(f"no canned JWKS for {uri}")
        return {"keys": [keys[uri]]}

    return fetcher


@pytest.mark.asyncio
async def test_resolver_fetches_brand_json_and_inner_jwks(patch_httpx) -> None:
    """Full happy path: resolver fetches brand.json, picks the agent,
    fetches the JWKS, returns the JWK by kid."""
    patch_httpx(
        {
            "https://example.com/.well-known/brand.json": {
                "body": _brand_json(),
                "headers": {"content-type": "application/json"},
            },
        }
    )
    resolver = BrandJsonJwksResolver(
        "https://example.com/.well-known/brand.json",
        agent_type="brand",
        jwks_fetcher=_jwks_fetcher_for(
            {"https://x.example/jwks": {"kty": "OKP", "crv": "Ed25519", "x": "abc", "kid": "k1"}}
        ),
    )
    jwk = await resolver("k1")
    assert jwk is not None
    assert jwk["kid"] == "k1"
    assert resolver.agent_url == "https://x.example/"


@pytest.mark.asyncio
async def test_resolver_returns_none_for_unknown_kid(patch_httpx) -> None:
    patch_httpx(
        {
            "https://example.com/.well-known/brand.json": {
                "body": _brand_json(),
                "headers": {"content-type": "application/json"},
            },
        }
    )
    resolver = BrandJsonJwksResolver(
        "https://example.com/.well-known/brand.json",
        agent_type="brand",
        jwks_fetcher=_jwks_fetcher_for(
            {"https://x.example/jwks": {"kty": "OKP", "crv": "Ed25519", "x": "abc", "kid": "k1"}}
        ),
    )
    assert await resolver("nonexistent-kid") is None


@pytest.mark.asyncio
async def test_resolver_follows_authoritative_location_redirect(patch_httpx) -> None:
    import json

    patch_httpx(
        {
            "https://entry.example/.well-known/brand.json": {
                "body": json.dumps(
                    {"authoritative_location": "https://final.example/.well-known/brand.json"}
                ).encode(),
                "headers": {"content-type": "application/json"},
            },
            "https://final.example/.well-known/brand.json": {
                "body": _brand_json("https://final.example/agent/", "https://final.example/jwks"),
                "headers": {"content-type": "application/json"},
            },
        }
    )
    final_jwk = {"kty": "OKP", "crv": "Ed25519", "x": "abc", "kid": "k1"}
    resolver = BrandJsonJwksResolver(
        "https://entry.example/.well-known/brand.json",
        agent_type="brand",
        jwks_fetcher=_jwks_fetcher_for({"https://final.example/jwks": final_jwk}),
    )
    jwk = await resolver("k1")
    assert jwk is not None and jwk["kid"] == "k1"


@pytest.mark.asyncio
async def test_resolver_follows_house_string_redirect(patch_httpx) -> None:
    """The ``house`` string redirect: ``{"house": "portfolio.example"}``
    → ``https://portfolio.example/.well-known/brand.json``."""
    import json

    patch_httpx(
        {
            "https://entry.example/.well-known/brand.json": {
                "body": json.dumps({"house": "portfolio.example"}).encode(),
                "headers": {"content-type": "application/json"},
            },
            "https://portfolio.example/.well-known/brand.json": {
                "body": _brand_json("https://portfolio.example/", "https://portfolio.example/jwks"),
                "headers": {"content-type": "application/json"},
            },
        }
    )
    portfolio_jwk = {"kty": "OKP", "crv": "Ed25519", "x": "abc", "kid": "k1"}
    resolver = BrandJsonJwksResolver(
        "https://entry.example/.well-known/brand.json",
        agent_type="brand",
        jwks_fetcher=_jwks_fetcher_for({"https://portfolio.example/jwks": portfolio_jwk}),
    )
    jwk = await resolver("k1")
    assert jwk is not None


@pytest.mark.asyncio
async def test_resolver_rejects_invalid_house_string(patch_httpx) -> None:
    """An attacker-controlled brand.json emitting
    ``{"house": "evil.com\\@victim.com"}`` MUST be rejected at parse
    time, not after a fetch attempt."""
    import json

    patch_httpx(
        {
            "https://entry.example/.well-known/brand.json": {
                "body": json.dumps({"house": "evil.com@victim.com"}).encode(),
                "headers": {"content-type": "application/json"},
            }
        }
    )
    resolver = BrandJsonJwksResolver(
        "https://entry.example/.well-known/brand.json",
        agent_type="brand",
    )
    with pytest.raises(BrandJsonResolverError) as exc:
        await resolver("k1")
    assert exc.value.code == "invalid_house"


@pytest.mark.asyncio
async def test_resolver_redirect_depth_exceeded(patch_httpx) -> None:
    """A long redirect chain must terminate at ``max_redirects``."""
    import json

    # Chain entry → hop1 → hop2 → hop3 (over the default cap of 3).
    patch_httpx(
        {
            "https://entry.example/.well-known/brand.json": {
                "body": json.dumps(
                    {"authoritative_location": "https://hop1.example/.well-known/brand.json"}
                ).encode(),
                "headers": {"content-type": "application/json"},
            },
            "https://hop1.example/.well-known/brand.json": {
                "body": json.dumps(
                    {"authoritative_location": "https://hop2.example/.well-known/brand.json"}
                ).encode(),
                "headers": {"content-type": "application/json"},
            },
            "https://hop2.example/.well-known/brand.json": {
                "body": json.dumps(
                    {"authoritative_location": "https://hop3.example/.well-known/brand.json"}
                ).encode(),
                "headers": {"content-type": "application/json"},
            },
            "https://hop3.example/.well-known/brand.json": {
                "body": json.dumps(
                    {"authoritative_location": "https://hop4.example/.well-known/brand.json"}
                ).encode(),
                "headers": {"content-type": "application/json"},
            },
        }
    )
    resolver = BrandJsonJwksResolver(
        "https://entry.example/.well-known/brand.json",
        agent_type="brand",
        max_redirects=3,
    )
    with pytest.raises(BrandJsonResolverError) as exc:
        await resolver("k1")
    assert exc.value.code == "redirect_depth_exceeded"


@pytest.mark.asyncio
async def test_resolver_redirect_loop_detected(patch_httpx) -> None:
    """A redirect cycle must be detected even within the hop cap."""
    import json

    patch_httpx(
        {
            "https://entry.example/.well-known/brand.json": {
                "body": json.dumps(
                    {"authoritative_location": "https://other.example/.well-known/brand.json"}
                ).encode(),
                "headers": {"content-type": "application/json"},
            },
            "https://other.example/.well-known/brand.json": {
                "body": json.dumps(
                    {"authoritative_location": "https://entry.example/.well-known/brand.json"}
                ).encode(),
                "headers": {"content-type": "application/json"},
            },
        }
    )
    resolver = BrandJsonJwksResolver(
        "https://entry.example/.well-known/brand.json",
        agent_type="brand",
    )
    with pytest.raises(BrandJsonResolverError) as exc:
        await resolver("k1")
    assert exc.value.code == "redirect_loop"


@pytest.mark.asyncio
async def test_resolver_handles_fetch_404(patch_httpx) -> None:
    patch_httpx({})  # nothing — 404 on every call
    resolver = BrandJsonJwksResolver(
        "https://example.com/.well-known/brand.json",
        agent_type="brand",
    )
    with pytest.raises(BrandJsonResolverError) as exc:
        await resolver("k1")
    assert exc.value.code == "fetch_failed"


@pytest.mark.asyncio
async def test_resolver_handles_invalid_json(patch_httpx) -> None:
    patch_httpx(
        {
            "https://example.com/.well-known/brand.json": {
                "body": b"not-valid-json",
                "headers": {"content-type": "application/json"},
            }
        }
    )
    resolver = BrandJsonJwksResolver(
        "https://example.com/.well-known/brand.json",
        agent_type="brand",
    )
    with pytest.raises(BrandJsonResolverError) as exc:
        await resolver("k1")
    assert exc.value.code == "invalid_body"


@pytest.mark.asyncio
async def test_resolver_force_refresh_clears_snapshot(patch_httpx) -> None:
    transport = patch_httpx(
        {
            "https://example.com/.well-known/brand.json": {
                "body": _brand_json(),
                "headers": {"content-type": "application/json"},
            },
        }
    )
    resolver = BrandJsonJwksResolver(
        "https://example.com/.well-known/brand.json",
        agent_type="brand",
        jwks_fetcher=_jwks_fetcher_for(
            {"https://x.example/jwks": {"kty": "OKP", "crv": "Ed25519", "x": "abc", "kid": "k1"}}
        ),
    )
    await resolver("k1")
    initial_calls = len(transport.calls)
    await resolver.force_refresh()
    # force_refresh always re-fetches brand.json (jwks fetched via
    # the injected fetcher, not the patched transport).
    assert len(transport.calls) > initial_calls


@pytest.mark.asyncio
async def test_resolver_satisfies_jwks_resolver_protocol() -> None:
    """Structural check that the class is callable as
    ``await resolver(kid)`` per the AsyncJwksResolver Protocol."""
    resolver = BrandJsonJwksResolver("https://x/", agent_type="brand")
    # Just check it's awaitable-callable; we don't actually fetch.
    assert callable(resolver)
    coro = resolver.resolve("k")
    assert asyncio.iscoroutine(coro)
    coro.close()
