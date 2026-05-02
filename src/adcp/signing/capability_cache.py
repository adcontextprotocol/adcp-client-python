"""Per-agent cache of ``request_signing`` capability advertisements.

Port of ``src/lib/signing/capability-cache.ts`` from the JS SDK
(``adcontextprotocol/adcp-client``). Same surface, same key format, so
multi-language deployments that later share a Redis-backed cache can
interop on the cache key.

The cache stores the ``request_signing`` block returned by
:func:`get_adcp_capabilities` for a given agent endpoint. Keyed by
``cache_key`` — typically built via :func:`build_capability_cache_key`
as ``agent_uri + auth-token-hash + signer-fingerprint`` so that different
credentials or signing identities get independent entries (a seller can
advertise different policies per counterparty key).

Staleness is TTL-based; callers may also invalidate explicitly — e.g.
after a seller rotates its advertisement mid-session — so the next
outbound call re-primes before deciding whether to sign.

Negative-cache TTL: callers populating the cache after a failed
discovery call (see :mod:`adcp.signing.capability_priming`) set
``stale_at`` to a shorter window so a transient seller outage doesn't
block signing decisions for the full TTL.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class CachedCapability:
    """One cache entry — the seller's advertised request-signing policy.

    :param request_signing: The ``request_signing`` capability block as
        advertised by the agent. ``None`` when discovery succeeded but
        the seller advertises no signing requirements, or when a
        negative-cache entry was written after a failed discovery.
    :param adcp_version: The major AdCP version associated with the
        capability response, when known.
    :param fetched_at: Epoch seconds when this entry was written.
    :param stale_at: Optional explicit epoch-seconds deadline at which
        this entry becomes stale. Overrides the cache's default TTL —
        used to give negative-cache entries a shorter refresh window.
    """

    request_signing: dict[str, Any] | None
    adcp_version: int | None
    fetched_at: float
    stale_at: float | None = None


class CapabilityCache:
    """Per-agent cache of capability advertisements.

    In-process — single :class:`CapabilityCache` per Python process is
    typical. Multi-tenant embeddings that need per-tenant isolation
    construct one cache per tenant; the in-flight-fetch dedup table is
    instance-local so two tenants don't race each other's writes.

    Adopters running multiple workers that need shared cache state
    implement a Redis-backed variant against the same surface — the
    public methods (``get``, ``set``, ``invalidate``, ``clear``,
    ``is_stale``) plus the in-flight hooks are the contract.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock or time.time
        self._entries: dict[str, CachedCapability] = {}
        # In-flight priming fetches keyed by cache_key. Lives on the
        # instance so two CapabilityCache objects (e.g., one per tenant
        # in a multi-tenant embedding) don't share an
        # ``ensure_capability_loaded`` future map and race each other's
        # writes.
        self._in_flight: dict[str, asyncio.Future[CachedCapability]] = {}

    def get(self, cache_key: str) -> CachedCapability | None:
        return self._entries.get(cache_key)

    def set(self, cache_key: str, entry: CachedCapability) -> None:
        self._entries[cache_key] = entry

    def invalidate(self, cache_key: str) -> None:
        self._entries.pop(cache_key, None)

    def clear(self) -> None:
        self._entries.clear()
        self._in_flight.clear()

    def is_stale(self, entry: CachedCapability | None) -> bool:
        if entry is None:
            return True
        now = self._clock()
        if entry.stale_at is not None:
            return now >= entry.stale_at
        return now - entry.fetched_at > self._ttl_seconds

    # --- internal in-flight hooks (used by ensure_capability_loaded) ---

    def _get_in_flight(self, cache_key: str) -> asyncio.Future[CachedCapability] | None:
        return self._in_flight.get(cache_key)

    def _set_in_flight(
        self,
        cache_key: str,
        future: asyncio.Future[CachedCapability],
    ) -> None:
        self._in_flight[cache_key] = future

    def _delete_in_flight(self, cache_key: str) -> None:
        self._in_flight.pop(cache_key, None)


#: Process-global default capability cache. Shared by transport-level
#: signing wrappers so a single ``get_adcp_capabilities`` call serves
#: every subsequent signing decision for an agent. Adopters with
#: per-tenant cache isolation construct their own instances instead.
default_capability_cache = CapabilityCache()


def build_capability_cache_key(
    agent_uri: str,
    *,
    auth_token: str | None = None,
    signer_fingerprint: str | None = None,
) -> str:
    """Build a stable cache key for ``CapabilityCache``.

    Two callers pointing at the same agent URI under different signing
    identities (different auth tokens or different signing keys) get
    independent cache entries — a seller can advertise different
    policies per counterparty key.

    The ``auth_token`` hash is a cache-key disambiguator, not a
    security boundary; a hypothetical collision across users would
    still transmit only the original caller's token (the cache key
    isn't the auth credential itself).

    Format matches the JS SDK exactly:
    ``agent_uri[::sha256(auth_token)[:16]][::sig=signer_fingerprint]``
    """
    parts = [agent_uri]
    if auth_token:
        token_digest = hashlib.sha256(auth_token.encode("utf-8")).hexdigest()[:16]
        parts.append(f"::{token_digest}")
    if signer_fingerprint:
        parts.append(f"::sig={signer_fingerprint}")
    return "".join(parts)


# Type alias for the priming callback consumed by
# ``ensure_capability_loaded``. Adopters wire this to a client that
# calls the seller's ``get_adcp_capabilities`` task and returns the
# raw response (MCP CallToolResult or A2A SendMessageResponse).
FetchRaw = Callable[[], Awaitable[Any]]


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "CachedCapability",
    "CapabilityCache",
    "FetchRaw",
    "build_capability_cache_key",
    "default_capability_cache",
]
