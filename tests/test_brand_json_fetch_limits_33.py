"""AdCP 3.3 brand.json fetch budget (security.mdx, "Discovering an agent's
signing keys via ``brand_json_url``", step 4; migration guide "Brand JSON size
cap rises to 2 MiB").

* the body cap is 2 MiB (2,097,152 bytes), counted while the body streams;
* the cap counts decoded bytes (content-encoded responses are refused);
* the strict parse enforces a nesting depth of 32;
* connect 5 s and a 10 s total deadline that covers reading the body;
* concurrent brand.json fetches are bounded;
* successful snapshots stay fresh no longer than the revocation polling
  ceiling, and a failed refresh holds off the next one for at most 60 s;
* the JWKS and capabilities caps are unchanged.
"""

from __future__ import annotations

import asyncio
import gzip
import inspect
import json
from typing import Any

import httpx
import pytest

from adcp.exceptions import RegistryError
from adcp.registry import (
    DEFAULT_MAX_BRAND_JSON_REGISTRY_RESPONSE_BYTES,
    DEFAULT_MAX_REGISTRY_RESPONSE_BYTES,
    RegistryClient,
)
from adcp.signing import brand_jwks
from adcp.signing.agent_resolver import DEFAULT_MAX_CAPABILITIES_BYTES, _brand_resolution_error
from adcp.signing.brand_authz import (
    BrandJsonAuthorizationResolver,
    build_brand_json_resolvers,
)
from adcp.signing.brand_jwks import (
    DEFAULT_BRAND_JSON_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_BRAND_JSON_TIMEOUT_SECONDS,
    DEFAULT_MAX_BRAND_JSON_BYTES,
    MAX_BRAND_JSON_DEPTH,
    MAX_BRAND_JSON_NEGATIVE_CACHE_SECONDS,
    BrandJsonJwksResolver,
    BrandJsonResolverError,
    _BrandJsonFetcher,
    _exceeds_nesting_depth,
    _fetch_brand_json,
)
from adcp.signing.errors import REQUEST_SIGNATURE_BRAND_JSON_MALFORMED
from adcp.signing.jwks import DEFAULT_JWKS_MAX_AGE_SECONDS, DEFAULT_MAX_JWKS_BYTES

BRAND_URL = "https://brand.example/.well-known/brand.json"
AGENT_URL = "https://agent.brand.example/"
JWKS_URL = "https://agent.brand.example/jwks"
JWK = {"kty": "OKP", "crv": "Ed25519", "x": "abc", "kid": "k1"}
MIB = 1024 * 1024


# ----- helpers -----


class _Stream(httpx.AsyncByteStream):
    """Yields fixed chunks and records how many the reader pulled."""

    def __init__(self, chunks: list[bytes], *, delay: float = 0.0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.pulled = 0

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.pulled += 1
            yield chunk


def _factory(handler: Any):  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)

    def factory(_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False)

    return factory


def _brand_json(padding: int = 0) -> bytes:
    doc = {
        "agents": [{"type": "sales", "url": AGENT_URL, "jwks_uri": JWKS_URL}],
        "description": "x" * padding,
    }
    return json.dumps(doc, separators=(",", ":")).encode()


def _brand_json_of_size(size: int) -> bytes:
    body = _brand_json()
    padded = _brand_json(size - len(body))
    assert len(padded) == size
    return padded


def _nested(depth: int) -> bytes:
    """A brand.json whose deepest container sits at ``depth`` (top level = 1)."""
    inner = "[" * (depth - 1) + "]" * (depth - 1)
    doc = json.loads(_brand_json())
    encoded = json.dumps(doc, separators=(",", ":"))
    return (encoded[:-1] + f',"deep":{inner}' + "}").encode()


async def _jwks_fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
    assert uri == JWKS_URL
    return {"keys": [JWK]}


def _resolver(handler: Any, **kwargs: Any) -> BrandJsonJwksResolver:
    return BrandJsonJwksResolver(
        BRAND_URL,
        agent_url=AGENT_URL,
        jwks_fetcher=_jwks_fetcher,
        _client_factory=_factory(handler),
        **kwargs,
    )


# ----- the cap -----


def test_brand_json_cap_is_two_mebibytes_on_every_fetch_path() -> None:
    assert DEFAULT_MAX_BRAND_JSON_BYTES == 2_097_152
    for fn in (
        BrandJsonJwksResolver.__init__,
        BrandJsonAuthorizationResolver.__init__,
        build_brand_json_resolvers,
        _BrandJsonFetcher.__init__,
        _fetch_brand_json,
    ):
        default = inspect.signature(fn).parameters["max_body_bytes"].default
        assert default == DEFAULT_MAX_BRAND_JSON_BYTES, fn


def test_jwks_and_capabilities_caps_are_unchanged() -> None:
    assert DEFAULT_MAX_JWKS_BYTES == 1024 * 1024
    assert DEFAULT_MAX_CAPABILITIES_BYTES == 64 * 1024


@pytest.mark.parametrize("cap", [DEFAULT_MAX_BRAND_JSON_BYTES + 1, 0])
def test_max_body_bytes_cannot_exceed_the_cap(cap: int) -> None:
    with pytest.raises(ValueError, match="max_body_bytes"):
        BrandJsonJwksResolver(BRAND_URL, agent_url=AGENT_URL, max_body_bytes=cap)


async def test_one_mebibyte_brand_json_resolves() -> None:
    body = _brand_json_of_size(MIB)
    resolver = _resolver(lambda _req: httpx.Response(200, content=body))
    assert await resolver("k1") == JWK
    assert resolver.jwks_uri == JWKS_URL


async def test_brand_json_of_exactly_two_mebibytes_resolves() -> None:
    body = _brand_json_of_size(DEFAULT_MAX_BRAND_JSON_BYTES)
    chunks = [body[i : i + 64 * 1024] for i in range(0, len(body), 64 * 1024)]
    resolver = _resolver(lambda _req: httpx.Response(200, stream=_Stream(chunks)))
    assert await resolver("k1") == JWK


async def test_two_mebibytes_plus_one_aborts_mid_stream() -> None:
    """No Content-Length: the cap is enforced by counting streamed bytes."""
    chunk = b"x" * (64 * 1024)
    over = DEFAULT_MAX_BRAND_JSON_BYTES // len(chunk)  # 32 chunks == exactly the cap
    stream = _Stream([chunk] * over + [b"y"] + [chunk] * 64)
    resolver = _resolver(lambda _req: httpx.Response(200, stream=stream))
    with pytest.raises(BrandJsonResolverError) as exc:
        await resolver("k1")
    assert exc.value.code == "invalid_body"
    assert f"exceeds {DEFAULT_MAX_BRAND_JSON_BYTES} bytes" in str(exc.value)
    # Reading stopped within one read chunk of the cap, never draining the rest.
    assert stream.pulled <= over + 2
    assert _brand_resolution_error(exc.value).signature_code == (
        REQUEST_SIGNATURE_BRAND_JSON_MALFORMED
    )


async def test_declared_content_length_over_cap_is_refused_before_reading() -> None:
    stream = _Stream([b"{}"])
    resolver = _resolver(
        lambda _req: httpx.Response(
            200,
            stream=stream,
            headers={"content-length": str(DEFAULT_MAX_BRAND_JSON_BYTES + 1)},
        )
    )
    with pytest.raises(BrandJsonResolverError) as exc:
        await resolver("k1")
    assert exc.value.code == "invalid_body"
    assert stream.pulled == 0


async def test_content_encoded_body_is_refused_rather_than_expanded() -> None:
    """A small gzip body cannot expand past the cap: encoded bodies are refused."""
    bomb = gzip.compress(b" " * (4 * MIB) + _brand_json())
    resolver = _resolver(
        lambda _req: httpx.Response(200, content=bomb, headers={"content-encoding": "gzip"})
    )
    with pytest.raises(BrandJsonResolverError) as exc:
        await resolver("k1")
    assert exc.value.code == "fetch_failed"


async def test_request_asks_for_identity_encoding() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_brand_json())

    await _resolver(handler)("k1")
    assert seen[0].headers["accept-encoding"] == "identity"


# ----- nesting depth -----


def test_depth_scan_counts_containers_not_bracket_bytes_in_strings() -> None:
    assert not _exceeds_nesting_depth(_nested(32), 32)
    assert _exceeds_nesting_depth(_nested(33), 32)
    # Brackets inside strings, including after escaped quotes and
    # backslashes, do not nest.
    assert not _exceeds_nesting_depth(b'{"a":"' + b"[" * 100 + b'"}', 32)
    assert not _exceeds_nesting_depth(b'{"a":"\\"' + b"[" * 100 + b'"}', 32)
    assert _exceeds_nesting_depth(b'{"a":"\\\\",' + b'"b":' + b"[" * 40 + b"]" * 40 + b"}", 32)
    # Closing brackets inside a string cannot mask real depth.
    masked = b'{"a":"' + b"]" * 40 + b'","b":' + b"[" * 32 + b"]" * 32 + b"}"
    assert _exceeds_nesting_depth(masked, 32)


async def test_depth_32_brand_json_resolves() -> None:
    body = _nested(MAX_BRAND_JSON_DEPTH)
    resolver = _resolver(lambda _req: httpx.Response(200, content=body))
    assert await resolver("k1") == JWK


async def test_depth_33_brand_json_is_refused_as_malformed() -> None:
    body = _nested(MAX_BRAND_JSON_DEPTH + 1)
    resolver = _resolver(lambda _req: httpx.Response(200, content=body))
    with pytest.raises(BrandJsonResolverError) as exc:
        await resolver("k1")
    assert exc.value.code == "invalid_body"
    assert "nesting depth" in str(exc.value)
    assert _brand_resolution_error(exc.value).signature_code == (
        REQUEST_SIGNATURE_BRAND_JSON_MALFORMED
    )


async def test_depth_scan_handles_a_full_size_document_quickly() -> None:
    entries = [
        {"id": f"b{i}", "names": [{"en": "n" * 40}], "colors": ["#fff"]} for i in range(9000)
    ]
    body = json.dumps({"brands": entries}).encode()
    loop = asyncio.get_running_loop()
    start = loop.time()
    assert not _exceeds_nesting_depth(body, MAX_BRAND_JSON_DEPTH)
    assert loop.time() - start < 2.0


# ----- deadlines -----


def test_deadline_defaults_match_the_spec() -> None:
    assert DEFAULT_BRAND_JSON_CONNECT_TIMEOUT_SECONDS == 5.0
    assert DEFAULT_BRAND_JSON_TIMEOUT_SECONDS == 10.0


async def test_total_deadline_covers_a_slow_body() -> None:
    """Each chunk arrives inside any per-read timeout; the total still trips."""
    stream = _Stream([b"{"] + [b" "] * 1000 + [b"}"], delay=0.01)
    resolver = _resolver(lambda _req: httpx.Response(200, stream=stream), timeout_seconds=0.2)
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(BrandJsonResolverError) as exc:
        await resolver("k1")
    assert loop.time() - start < 2.0
    assert exc.value.code == "fetch_failed"
    assert "total deadline" in str(exc.value)


async def test_production_client_uses_five_second_connect_deadline(monkeypatch) -> None:
    timeouts: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, content=_brand_json())

    import adcp.signing.ip_pinned_transport as pinned

    monkeypatch.setattr(
        pinned,
        "build_async_ip_pinned_transport",
        lambda _url, *, allow_private: httpx.MockTransport(handler),
    )
    await _fetch_brand_json(
        start_url=BRAND_URL,
        current_etag=None,
        max_redirects=0,
        allow_private=False,
        timeout_seconds=DEFAULT_BRAND_JSON_TIMEOUT_SECONDS,
    )
    assert timeouts[0]["connect"] == 5.0
    assert timeouts[0]["read"] == 10.0


# ----- bounded concurrency -----


async def test_concurrent_brand_json_fetches_are_bounded(monkeypatch) -> None:
    monkeypatch.setattr(brand_jwks, "_MAX_CONCURRENT_BRAND_JSON_FETCHES", 2)
    monkeypatch.setattr(brand_jwks, "_fetch_slots", brand_jwks.weakref.WeakKeyDictionary())
    active = 0
    peak = 0
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1
        return httpx.Response(200, content=_brand_json())

    async def fetch(i: int) -> None:
        await _fetch_brand_json(
            start_url=f"https://b{i}.example/.well-known/brand.json",
            current_etag=None,
            max_redirects=0,
            allow_private=False,
            timeout_seconds=5.0,
            client_factory=_factory(handler),
        )

    tasks = [asyncio.create_task(fetch(i)) for i in range(5)]
    for _ in range(20):
        await asyncio.sleep(0)
    assert peak == 2
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2


# ----- cache lifetimes -----


async def test_success_ttl_never_exceeds_revocation_polling_ceiling() -> None:
    now = {"t": 1000.0}
    resolver = _resolver(
        lambda _req: httpx.Response(200, content=_brand_json()),
        max_age_seconds=86_400.0,
        clock=lambda: now["t"],
    )
    await resolver("k1")
    snap = resolver._fetcher.snapshot
    assert snap is not None
    assert snap.expires_at - snap.fetched_at == DEFAULT_JWKS_MAX_AGE_SECONDS


async def test_failed_refresh_is_negatively_cached_for_at_most_60_seconds() -> None:
    now = {"t": 1000.0}
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, content=_brand_json())
        return httpx.Response(404)

    fetcher = _BrandJsonFetcher(
        BRAND_URL,
        max_age_seconds=10.0,
        min_cooldown_seconds=600.0,
        clock=lambda: now["t"],
        _client_factory=_factory(handler),
    )
    await fetcher.refresh()
    now["t"] += 600.0
    assert fetcher.can_refresh()
    with pytest.raises(BrandJsonResolverError):
        await fetcher.refresh()
    # A failure holds off the next attempt for at most 60 s ...
    now["t"] += MAX_BRAND_JSON_NEGATIVE_CACHE_SECONDS - 1
    assert not fetcher.can_refresh()
    now["t"] += 1
    assert fetcher.can_refresh()


async def test_successful_refresh_keeps_the_configured_cooldown() -> None:
    now = {"t": 1000.0}
    fetcher = _BrandJsonFetcher(
        BRAND_URL,
        min_cooldown_seconds=600.0,
        clock=lambda: now["t"],
        _client_factory=_factory(lambda _req: httpx.Response(200, content=_brand_json())),
    )
    await fetcher.refresh()
    now["t"] += MAX_BRAND_JSON_NEGATIVE_CACHE_SECONDS
    assert not fetcher.can_refresh()


# ----- registry read of a raw brand.json -----


def _registry(payload_bytes: bytes) -> RegistryClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=payload_bytes, headers={"content-type": "application/json"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RegistryClient(base_url="https://registry.example", client=client)


async def test_registry_get_brand_json_reads_a_two_mebibyte_brand_json() -> None:
    data = json.loads(_brand_json_of_size(DEFAULT_MAX_BRAND_JSON_BYTES))
    envelope = json.dumps(
        {"domain": "brand.example", "url": BRAND_URL, "data": data, "warnings": []}
    ).encode()
    assert DEFAULT_MAX_REGISTRY_RESPONSE_BYTES < len(envelope)
    assert len(envelope) <= DEFAULT_MAX_BRAND_JSON_REGISTRY_RESPONSE_BYTES
    result = await _registry(envelope).get_brand_json("brand.example")
    assert result is not None
    assert result["data"]["agents"][0]["url"] == AGENT_URL


async def test_registry_brand_json_read_is_still_bounded() -> None:
    envelope = b'{"data":{"x":"' + b"x" * DEFAULT_MAX_BRAND_JSON_REGISTRY_RESPONSE_BYTES + b'"}}'
    with pytest.raises(RegistryError):
        await _registry(envelope).get_brand_json("brand.example")


async def test_registry_other_endpoints_keep_their_cap() -> None:
    envelope = b'{"x":"' + b"x" * DEFAULT_MAX_REGISTRY_RESPONSE_BYTES + b'"}'
    with pytest.raises(RegistryError):
        await _registry(envelope).lookup_brand("brand.example")
