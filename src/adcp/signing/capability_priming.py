"""Capability-cache primer.

Port of ``src/lib/signing/capability-priming.ts`` from the JS SDK.

Populates :class:`CapabilityCache` for an agent when the
``request_signing`` entry is absent or stale. The injected
:data:`FetchRaw` callback makes an unsigned ``get_adcp_capabilities``
call against the counterparty — adopters wire it to their existing
client (``adcp.client``, salesagent's MCP/A2A client, etc.) so no new
network code lives here.

**Fail-open posture.** If discovery itself fails, this primer caches
an empty entry with a 60-second ``stale_at`` window and returns it
rather than propagating the error. Signing decisions then fall through:

* Ops in the buyer's ``always_sign`` list are still signed (with
  default content-digest coverage), so explicit pilot opt-ins keep
  working.
* Ops the seller listed in ``required_for`` go out unsigned and are
  rejected with ``request_signature_required`` at the wire — the user
  sees a clear error rather than an opaque priming wedge, and the next
  retry re-primes.

**In-flight dedup.** Two concurrent priming calls for the same cache
key share one fetch via the cache's in-flight future table. Prevents
fetch-storms on cache miss when N tasks all start verifying
simultaneously.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from adcp.signing.capability_cache import (
    CachedCapability,
    CapabilityCache,
    FetchRaw,
)

logger = logging.getLogger(__name__)

#: The wire op name for the seller's capability advertisement. The
#: outbound signing wrapper short-circuits on this op so the priming
#: request itself is never gated by signing.
CAPABILITY_OP = "get_adcp_capabilities"

#: Refresh window applied to a negative-cache entry written after a
#: failed discovery call. 60s is short enough that a transient seller
#: outage self-heals on the next user action, long enough to avoid
#: pile-ups if the seller stays down.
NEGATIVE_CACHE_TTL_SECONDS = 60.0


async def ensure_capability_loaded(
    *,
    cache: CapabilityCache,
    cache_key: str,
    fetch_raw: FetchRaw,
) -> CachedCapability:
    """Populate the cache for an agent, returning the cached entry.

    Skips the fetch when a fresh entry exists. Joins an in-flight
    priming call for the same key when one is pending. On fetch
    failure, writes a negative-cache entry with a 60s ``stale_at``
    deadline and returns it (fail-open per module docstring).
    """
    existing = cache.get(cache_key)
    if existing is not None and not cache.is_stale(existing):
        return existing

    pending = cache._get_in_flight(cache_key)
    if pending is not None:
        return await pending

    loop = asyncio.get_running_loop()
    future: asyncio.Future[CachedCapability] = loop.create_future()
    cache._set_in_flight(cache_key, future)

    try:
        try:
            raw = await fetch_raw()
            request_signing, adcp_version = _extract_capability(raw)
            entry = CachedCapability(
                request_signing=request_signing,
                adcp_version=adcp_version,
                fetched_at=cache._clock(),
            )
        except (
            httpx.HTTPError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            # Fail-open: cache an empty negative entry with a short
            # ``stale_at`` window so a transient outage doesn't block
            # signing decisions for the full TTL.
            #
            # Catch only expected discovery failures — let
            # ``BaseException`` (asyncio.CancelledError,
            # KeyboardInterrupt, programmer bugs surfacing as
            # AttributeError) propagate. Without this narrowing, a
            # cancelled task would silently land in negative-cache and
            # the awaiter would never see the cancellation.
            logger.warning(
                "[adcp.signing.capability_priming] discovery for %s "
                "failed (%s: %s); negative-caching for %ds",
                cache_key,
                type(exc).__name__,
                exc,
                int(NEGATIVE_CACHE_TTL_SECONDS),
            )
            now = cache._clock()
            entry = CachedCapability(
                request_signing=None,
                adcp_version=None,
                fetched_at=now,
                stale_at=now + NEGATIVE_CACHE_TTL_SECONDS,
            )

        cache.set(cache_key, entry)
        if not future.done():
            future.set_result(entry)
        return entry
    except BaseException as exc:
        # Anything that propagated past the narrow catch above (e.g.
        # CancelledError from fetch_raw) — make sure waiters joined to
        # this future see the failure rather than awaiting forever.
        if not future.done():
            future.set_exception(exc)
        raise
    finally:
        cache._delete_in_flight(cache_key)


def _extract_capability(
    response: Any,
) -> tuple[dict[str, Any] | None, int | None]:
    """Extract the ``request_signing`` block and AdCP major version
    from a ``get_adcp_capabilities`` response, regardless of how the
    transport wrapped it.

    Three transport shapes the JS port handles, mirrored here:

    * MCP ``CallToolResult`` — ``structuredContent`` (preferred) or
      ``content[].text`` parsed as JSON.
    * A2A JSON-RPC ``SendMessageResponse`` — ``result`` is a Task
      (``artifacts[].parts[].data``) or a Message (``parts[].data``).
    * Already-unwrapped payload dict — used as-is.
    """
    payload = _unwrap_response(response)
    if not isinstance(payload, dict):
        return None, None

    request_signing = payload.get("request_signing")
    if not isinstance(request_signing, dict):
        request_signing = None

    adcp_version: int | None = None
    adcp = payload.get("adcp")
    if isinstance(adcp, dict):
        versions = adcp.get("major_versions")
        if isinstance(versions, list) and versions:
            first = versions[0]
            if isinstance(first, int):
                adcp_version = first

    return request_signing, adcp_version


def _unwrap_response(response: Any) -> Any:
    """Unwrap MCP / A2A transport envelopes to find the AdCP payload.

    Mirrors ``unwrapResponse`` in ``capability-priming.ts`` exactly —
    same precedence (MCP ``structuredContent`` → MCP ``content[].text``
    → A2A ``result.artifacts[].parts[].data`` → A2A
    ``result.parts[].data`` → fallthrough).
    """
    if not isinstance(response, dict):
        return response

    structured = response.get("structuredContent")
    if isinstance(structured, dict):
        return structured

    content = response.get("content")
    if isinstance(content, list):
        for chunk in content:
            if isinstance(chunk, dict):
                text = chunk.get("text")
                if isinstance(text, str):
                    try:
                        return json.loads(text)
                    except (json.JSONDecodeError, ValueError):
                        continue

    result = response.get("result")
    if isinstance(result, dict):
        artifacts = result.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if isinstance(artifact, dict):
                    data = _find_first_data_part(artifact.get("parts"))
                    if data is not None:
                        return data
        data = _find_first_data_part(result.get("parts"))
        if data is not None:
            return data

    return response


def _find_first_data_part(parts: Any) -> Any:
    """Return the first A2A ``DataPart`` payload from a parts list."""
    if not isinstance(parts, list):
        return None
    for part in parts:
        if isinstance(part, dict):
            kind = part.get("kind")
            data = part.get("data")
            if kind == "data" and isinstance(data, dict):
                return data
    return None


__all__ = [
    "CAPABILITY_OP",
    "NEGATIVE_CACHE_TTL_SECONDS",
    "ensure_capability_loaded",
]
