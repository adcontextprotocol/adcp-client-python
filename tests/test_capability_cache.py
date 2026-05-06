"""Tests for :mod:`adcp.signing.capability_cache` +
:mod:`adcp.signing.capability_priming`.

Behavior under test (matches JS port):

* ``CapabilityCache.is_stale`` — TTL window + ``stale_at`` override.
* ``build_capability_cache_key`` — exact key format match against
  the JS surface (``agent_uri::sha256(token)[:16]::sig=fingerprint``).
* ``ensure_capability_loaded`` — primes on miss, returns cached on
  hit, fail-open with negative-cache TTL on fetch failure.
* In-flight dedup — two concurrent priming calls share one fetch.
* Transport unwrapping — MCP ``structuredContent`` /
  ``content[].text`` / A2A ``result.artifacts[].parts[].data`` /
  A2A ``result.parts[].data``.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from adcp.signing.capability_cache import (
    CachedCapability,
    CapabilityCache,
    build_capability_cache_key,
)
from adcp.signing.capability_priming import (
    NEGATIVE_CACHE_TTL_SECONDS,
    _extract_capability,
    _unwrap_response,
    ensure_capability_loaded,
)

# ----- CapabilityCache -----


def test_cache_get_set_invalidate() -> None:
    cache = CapabilityCache()
    entry = CachedCapability(
        request_signing={"required_for": ["create_media_buy"]},
        adcp_version=3,
        fetched_at=1000.0,
    )
    assert cache.get("k") is None
    cache.set("k", entry)
    assert cache.get("k") is entry
    cache.invalidate("k")
    assert cache.get("k") is None


def test_cache_is_stale_uses_ttl_when_no_stale_at() -> None:
    now = 1000.0
    cache = CapabilityCache(ttl_seconds=300, clock=lambda: now)
    entry = CachedCapability(request_signing=None, adcp_version=None, fetched_at=now)
    cache.set("k", entry)
    assert cache.is_stale(entry) is False
    now = 1000.0 + 301
    assert cache.is_stale(entry) is True


def test_cache_is_stale_respects_stale_at_override() -> None:
    """Negative-cache entries set ``stale_at`` to a shorter window
    than the default TTL — must be honored over TTL math."""
    now = 1000.0
    cache = CapabilityCache(ttl_seconds=300, clock=lambda: now)
    entry = CachedCapability(
        request_signing=None,
        adcp_version=None,
        fetched_at=now,
        stale_at=now + 60,  # shorter than 300s TTL
    )
    assert cache.is_stale(entry) is False
    now = 1000.0 + 61
    assert cache.is_stale(entry) is True


def test_cache_is_stale_returns_true_for_none() -> None:
    cache = CapabilityCache()
    assert cache.is_stale(None) is True


def test_cache_clear_drops_entries_and_inflight() -> None:
    cache = CapabilityCache()
    cache.set("k", CachedCapability(None, None, 0.0))
    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        cache._set_in_flight("k", future)
        cache.clear()
        assert cache.get("k") is None
        assert cache._get_in_flight("k") is None
    finally:
        loop.close()


# ----- build_capability_cache_key -----


def test_cache_key_agent_uri_only() -> None:
    assert build_capability_cache_key("https://agent.example.com") == ("https://agent.example.com")


def test_cache_key_with_auth_token_uses_sha256_truncated_to_16_hex() -> None:
    """Format must match JS exactly so a future shared cache works."""
    token = "secret-token-abc"
    expected_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    key = build_capability_cache_key("https://x", auth_token=token)
    assert key == f"https://x::{expected_digest}"


def test_cache_key_with_signer_fingerprint() -> None:
    key = build_capability_cache_key("https://x", signer_fingerprint="fp-abc")
    assert key == "https://x::sig=fp-abc"


def test_cache_key_with_both_token_and_fingerprint() -> None:
    token = "t"
    expected_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    key = build_capability_cache_key("https://x", auth_token=token, signer_fingerprint="fp")
    assert key == f"https://x::{expected_digest}::sig=fp"


def test_cache_key_distinguishes_different_tokens() -> None:
    a = build_capability_cache_key("https://x", auth_token="alice")
    b = build_capability_cache_key("https://x", auth_token="bob")
    assert a != b


@pytest.mark.parametrize(
    "uri_with_slash,uri_without_slash",
    [
        ("https://x/mcp/", "https://x/mcp"),
        ("https://x/", "https://x"),
        ("https://x/api/mcp/", "https://x/api/mcp"),
    ],
)
def test_cache_key_normalizes_trailing_slash(uri_with_slash: str, uri_without_slash: str) -> None:
    """Trailing-slash variants of the same agent_uri must produce the same cache key.

    AgentConfig.validate_agent_uri preserves the caller-supplied URI form (including
    trailing slash) so MCP transport can try both /mcp and /mcp/ on connect. Without
    normalization here, a single logical agent would split-brain across two cache
    entries depending on which slash form the caller passed.
    """
    assert build_capability_cache_key(uri_with_slash) == build_capability_cache_key(uri_without_slash)
    assert build_capability_cache_key(uri_with_slash, auth_token="t") == build_capability_cache_key(
        uri_without_slash, auth_token="t"
    )
    assert build_capability_cache_key(
        uri_with_slash, signer_fingerprint="fp"
    ) == build_capability_cache_key(uri_without_slash, signer_fingerprint="fp")


# ----- _unwrap_response (transport shape unwrapping) -----


def test_unwrap_mcp_structured_content_wins() -> None:
    payload = {"request_signing": {"required_for": ["x"]}}
    response = {
        "structuredContent": payload,
        "content": [{"text": '{"unused": true}'}],
    }
    assert _unwrap_response(response) is payload


def test_unwrap_mcp_content_text_parsed_as_json() -> None:
    response = {"content": [{"text": '{"request_signing": {"required_for": []}}'}]}
    result = _unwrap_response(response)
    assert isinstance(result, dict)
    assert result["request_signing"] == {"required_for": []}


def test_unwrap_mcp_content_text_skips_non_json() -> None:
    response = {
        "content": [
            {"text": "not-json"},
            {"text": '{"request_signing": {}}'},
        ],
    }
    result = _unwrap_response(response)
    assert isinstance(result, dict)


def test_unwrap_a2a_task_artifacts_parts_data() -> None:
    payload = {"request_signing": {"required_for": ["create_media_buy"]}}
    response = {"result": {"artifacts": [{"parts": [{"kind": "data", "data": payload}]}]}}
    assert _unwrap_response(response) == payload


def test_unwrap_a2a_message_parts_data() -> None:
    payload = {"request_signing": {}}
    response = {"result": {"parts": [{"kind": "data", "data": payload}]}}
    assert _unwrap_response(response) == payload


def test_unwrap_falls_through_for_already_unwrapped() -> None:
    payload = {"request_signing": {}}
    assert _unwrap_response(payload) is payload


# ----- _extract_capability -----


def test_extract_capability_present() -> None:
    payload = {
        "request_signing": {"required_for": ["create_media_buy"]},
        "adcp": {"major_versions": [3, 4]},
    }
    rs, version = _extract_capability(payload)
    assert rs == {"required_for": ["create_media_buy"]}
    assert version == 3


def test_extract_capability_no_request_signing_returns_none() -> None:
    rs, version = _extract_capability({"adcp": {"major_versions": [3]}})
    assert rs is None
    assert version == 3


def test_extract_capability_handles_missing_adcp_block() -> None:
    rs, version = _extract_capability({"request_signing": {}})
    assert rs == {}
    assert version is None


def test_extract_capability_returns_none_for_non_dict() -> None:
    assert _extract_capability("string") == (None, None)
    assert _extract_capability(None) == (None, None)
    assert _extract_capability(["list"]) == (None, None)


# ----- ensure_capability_loaded -----


@pytest.mark.asyncio
async def test_ensure_loaded_primes_on_miss() -> None:
    cache = CapabilityCache()
    calls = 0

    async def fetch() -> dict:
        nonlocal calls
        calls += 1
        return {
            "request_signing": {"required_for": ["create_media_buy"]},
            "adcp": {"major_versions": [3]},
        }

    entry = await ensure_capability_loaded(cache=cache, cache_key="k", fetch_raw=fetch)
    assert calls == 1
    assert entry.request_signing == {"required_for": ["create_media_buy"]}
    assert entry.adcp_version == 3
    assert cache.get("k") is entry


@pytest.mark.asyncio
async def test_ensure_loaded_hits_cache_on_second_call() -> None:
    cache = CapabilityCache()
    calls = 0

    async def fetch() -> dict:
        nonlocal calls
        calls += 1
        return {"request_signing": {}}

    await ensure_capability_loaded(cache=cache, cache_key="k", fetch_raw=fetch)
    await ensure_capability_loaded(cache=cache, cache_key="k", fetch_raw=fetch)
    assert calls == 1


@pytest.mark.asyncio
async def test_ensure_loaded_failopens_with_negative_cache() -> None:
    """When discovery fails, write a negative-cache entry with
    ``stale_at`` set to ``fetched_at + NEGATIVE_CACHE_TTL_SECONDS``
    and return it rather than propagating the error."""
    cache = CapabilityCache()

    async def fetch() -> dict:
        raise ConnectionError("seller offline")

    entry = await ensure_capability_loaded(cache=cache, cache_key="k", fetch_raw=fetch)
    assert entry.request_signing is None
    assert entry.adcp_version is None
    assert entry.stale_at is not None
    assert entry.stale_at - entry.fetched_at == pytest.approx(NEGATIVE_CACHE_TTL_SECONDS)
    # Cached so the next call within the negative-cache window doesn't
    # re-fetch.
    assert cache.get("k") is entry


@pytest.mark.asyncio
async def test_ensure_loaded_dedups_concurrent_calls() -> None:
    """Two concurrent primings for the same key share one fetch."""
    cache = CapabilityCache()
    started = 0

    async def fetch() -> dict:
        nonlocal started
        started += 1
        await asyncio.sleep(0.01)
        return {"request_signing": {"required_for": ["x"]}}

    a, b = await asyncio.gather(
        ensure_capability_loaded(cache=cache, cache_key="k", fetch_raw=fetch),
        ensure_capability_loaded(cache=cache, cache_key="k", fetch_raw=fetch),
    )
    assert started == 1
    assert a is b


@pytest.mark.asyncio
async def test_ensure_loaded_unwraps_mcp_envelope() -> None:
    cache = CapabilityCache()

    async def fetch() -> dict:
        return {
            "structuredContent": {
                "request_signing": {"required_for": ["create_media_buy"]},
                "adcp": {"major_versions": [3]},
            }
        }

    entry = await ensure_capability_loaded(cache=cache, cache_key="k", fetch_raw=fetch)
    assert entry.request_signing == {"required_for": ["create_media_buy"]}
    assert entry.adcp_version == 3


@pytest.mark.asyncio
async def test_ensure_loaded_refreshes_when_stale() -> None:
    now = [1000.0]
    cache = CapabilityCache(ttl_seconds=10, clock=lambda: now[0])
    calls = 0

    async def fetch() -> dict:
        nonlocal calls
        calls += 1
        return {"request_signing": {}}

    await ensure_capability_loaded(cache=cache, cache_key="k", fetch_raw=fetch)
    now[0] += 100  # past TTL
    await ensure_capability_loaded(cache=cache, cache_key="k", fetch_raw=fetch)
    assert calls == 2


@pytest.mark.asyncio
async def test_ensure_loaded_drains_in_flight_after_completion() -> None:
    """In-flight table cleans up so a later fetch isn't joined to a
    completed promise."""
    cache = CapabilityCache()

    async def fetch() -> dict:
        return {"request_signing": {}}

    await ensure_capability_loaded(cache=cache, cache_key="k", fetch_raw=fetch)
    assert cache._get_in_flight("k") is None
