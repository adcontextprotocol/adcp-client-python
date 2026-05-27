"""In-process wrappers around :class:`~adcp.decisioning.BuyerAgentRegistry`.

The Tier 2 commercial-identity gate fires on every dispatched skill.
A bare :class:`PgBuyerAgentRegistry` (or the v3 reference seller's
:class:`TenantScopedBuyerAgentRegistry`) hits the database on each
call — including the negative paths an enumeration probe will spam.
This module supplies three composable wrappers that implement the
:class:`BuyerAgentRegistry` Protocol and stack arbitrarily:

* :class:`CachingBuyerAgentRegistry` — TTL + LRU cache for positive
  AND negative resolutions. Negative caching is the load-shaping move:
  an enumeration probe walking a million ``agent_url`` strings would
  otherwise hit the DB once per probe; with negative caching it hits
  the DB once per ``(tenant, agent_url)`` pair within the TTL window.
* :class:`RateLimitedBuyerAgentRegistry` — per-(tenant, lookup-key)
  token bucket. On exhaustion, raises ``PERMISSION_DENIED`` with no
  ``details`` so the wire shape matches every other denied path
  (registry miss, suspended, blocked) — preserves the spec's
  omit-on-unestablished-identity rule from PR #393. A distinct
  ``RATE_LIMITED`` code would itself be an enumeration oracle.
* :class:`AuditingBuyerAgentRegistry` — terminal wrapper that fires
  one :class:`~adcp.audit_sink.AuditEvent` per ``resolved`` / ``miss``
  outcome from the inner store. Combine with the per-layer
  ``audit_sink`` kwarg on the cache / rate-limit wrappers to capture
  ``cached_hit`` / ``cached_miss`` / ``rate_limited`` events too.

Every wrapper accepts an optional ``audit_sink``. When provided, the
wrapper emits one :class:`AuditEvent` for any outcome it terminates
the call on (cache hit, rate-limit reject, DB resolve). Outcomes that
fall through to the inner wrapper are NOT re-emitted by the outer —
ordering avoids double-counting. If no sink is wired the outcome
logs at DEBUG.

Composition
-----------

The wrappers stack outside-in. Adopters typically build::

    inner = AuditingBuyerAgentRegistry(
        sql_backed_registry,  # actual DB lookup
        audit_sink=sink,
    )
    rate_limited = RateLimitedBuyerAgentRegistry(
        inner,
        rps_per_tenant=100,
        audit_sink=sink,  # capture rate_limited events
    )
    registry = CachingBuyerAgentRegistry(
        rate_limited,
        ttl_seconds=60,
        audit_sink=sink,  # capture cached_hit/cached_miss events
    )

Order matters:

* Cache is OUTERMOST so cached hits skip rate-limit accounting and
  the DB. Negative cache prevents the credential-stuffing oracle on
  the lookup endpoint.
* Rate limit sits BETWEEN cache and inner so the limiter only fires
  on calls that actually need DB work. Cached hits don't burn tokens.
* The terminal :class:`AuditingBuyerAgentRegistry` records the actual
  DB outcome.

Tenant scoping
--------------

The cache and rate limiter both key on ``(tenant_id, lookup_key)``.
The tenant id comes from :func:`adcp.server.current_tenant` — set
by the adopter's tenant middleware before the framework dispatches.
Adopters running single-tenant skip this contextvar; ``tenant_id``
falls through as ``None`` and the wrappers behave as a flat keyspace.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from adcp.decisioning.registry import (
    ApiKeyCredential,
    BuyerAgent,
    BuyerAgentRegistry,
    Credential,
    OAuthCredential,
)
from adcp.decisioning.types import AdcpError

if TYPE_CHECKING:
    from adcp.audit_sink import AuditSink

logger = logging.getLogger(__name__)


# ----- Shared types --------------------------------------------------


#: Outcome label emitted to audit + metrics. Distinct labels per code
#: path so compliance / SecOps can filter:
#:
#: * ``"resolved"`` — DB hit, agent returned.
#: * ``"miss"`` — DB hit, no row (the enumeration probe path).
#: * ``"cached_hit"`` — served from cache (positive).
#: * ``"cached_miss"`` — served from cache (negative).
#: * ``"rate_limited"`` — token bucket exhausted before reaching DB.
ResolveOutcome = str


_DENIED_MESSAGE = (
    "Buyer agent is not authorized for this seller. The seller's "
    "commercial allowlist did not authorize this credential. "
    "Resolve out-of-band via the seller's onboarding contact; this "
    "is not a request-side error the buyer can correct."
)


def _denied_error() -> AdcpError:
    """Build the wire-uniform PERMISSION_DENIED rate-limit raises.

    Same shape as the registry-miss path in
    :func:`adcp.decisioning.handler._resolve_buyer_agent` — no
    ``details``, ``recovery="correctable"``. Preserves the spec's
    omit-on-unestablished-identity rule: rate-limited and
    not-recognized are wire-indistinguishable.
    """
    return AdcpError(
        "PERMISSION_DENIED",
        message=_DENIED_MESSAGE,
        recovery="correctable",
    )


def _current_tenant_id() -> str | None:
    """Read the current-tenant contextvar, falling back to ``None``.

    Imported lazily to avoid a hard dependency on ``adcp.server`` at
    module import time — the registry surface lives below the server
    layer in the dependency graph.
    """
    try:
        from adcp.server import current_tenant
    except ImportError:  # pragma: no cover — server module always present in this SDK
        return None
    tenant = current_tenant()
    return tenant.id if tenant is not None else None


def _credential_key(credential: Credential) -> str:
    """Project a credential to a stable cache / rate-limit key.

    The key is namespaced (``"api_key:..."`` / ``"oauth:..."``) so
    a colliding ``key_id`` and ``client_id`` don't share a bucket.
    """
    if isinstance(credential, ApiKeyCredential):
        return f"api_key:{credential.key_id}"
    if isinstance(credential, OAuthCredential):
        return f"oauth:{credential.client_id}"
    # Defensive: future Credential variants the wrapper can't dispatch.
    return f"unknown:{type(credential).__name__}"


async def _emit_audit(
    sink: AuditSink | None,
    *,
    operation: str,
    outcome: ResolveOutcome,
    lookup_key: str,
    tenant_id: str | None,
    agent: BuyerAgent | None = None,
    sink_timeout_seconds: float = 5.0,
) -> None:
    """Emit a single registry-outcome audit event.

    The wrapper layers all funnel through here so the event shape is
    uniform across cache / rate-limit / terminal outcomes.

    Sink failures are bounded + swallowed — a sink stall (DB outage,
    Slack 429) NEVER blocks the registry resolution.
    """
    from adcp.audit_sink import AuditEvent

    details: dict[str, object] = {"outcome": outcome, "lookup_key": lookup_key}
    if agent is not None:
        details["agent_url"] = agent.agent_url
        details["agent_status"] = agent.status

    event = AuditEvent(
        operation=operation,
        success=outcome in ("resolved", "cached_hit"),
        occurred_at=datetime.now(timezone.utc),
        tenant_id=tenant_id,
        details=details,
    )

    if sink is None:
        logger.debug(
            "[adcp.registry_cache] %s outcome=%s tenant=%s lookup=%s",
            operation,
            outcome,
            tenant_id,
            lookup_key,
        )
        return
    try:
        await asyncio.wait_for(sink.record(event), timeout=sink_timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning(
            "[adcp.registry_cache] audit sink %s timed out after %ss for %s",
            type(sink).__name__,
            sink_timeout_seconds,
            operation,
        )
    except Exception:  # noqa: BLE001 — sink failures must not propagate
        logger.warning(
            "[adcp.registry_cache] audit sink %s raised for %s",
            type(sink).__name__,
            operation,
            exc_info=True,
        )


# ----- TTL + LRU cache ----------------------------------------------


@dataclass
class _CacheEntry:
    """One cached resolution (positive or negative)."""

    value: BuyerAgent | None
    expires_at: float


#: Type alias for the metric callback. Counters are incremented by 1
#: each call. Adopters wire this to Prometheus, OpenTelemetry,
#: StatsD, etc.
MetricCallback = Callable[[str], None]


class CachingBuyerAgentRegistry:
    """In-process TTL + LRU cache wrapping any
    :class:`BuyerAgentRegistry`.

    Caches BOTH positive and negative resolutions. Negative caching
    is the load-shaping move — an enumeration probe walking arbitrary
    ``agent_url`` strings would otherwise hit the DB once per probe;
    with negative caching it hits the DB at most once per
    ``(tenant, agent_url)`` pair within the TTL window.

    :param inner: The wrapped :class:`BuyerAgentRegistry` — typically
        a :class:`RateLimitedBuyerAgentRegistry` or a SQL-backed impl.
    :param ttl_seconds: How long a resolution stays cached. Default
        60s — long enough to absorb burst traffic during a media-buy
        flight, short enough that a status flip (active → suspended)
        propagates within a minute.
    :param max_entries: LRU cap. Default 4096 — bounded so an
        enumeration probe can't blow up memory, large enough that
        steady-state hot agents stay resident.
    :param hit_callback: Optional counter-style metric hook fired
        on every cache event. Receives ``"hit"`` / ``"miss"`` /
        ``"negative_hit"`` / ``"expired"``.
    :param audit_sink: Optional audit sink — emits ``cached_hit`` /
        ``cached_miss`` events for served-from-cache outcomes.
        Misses fall through to the inner wrapper which emits its
        own event for the actual resolution.
    :param time_source: Override for tests — defaults to
        :func:`time.monotonic`. Use a fake clock to drive TTL expiry.

    Concurrency
    -----------

    The cache uses an ``asyncio.Lock`` to serialize entry insertion
    and LRU promotion. The lock is held only across the dict update —
    the inner ``resolve_*`` call happens OUTSIDE the lock, so
    concurrent misses against different keys race the inner backend
    in parallel. A burst of concurrent misses against the SAME key
    will all hit the backend (no thundering-herd dedup) — adopters
    needing single-flight wrap with their own dedup. The simpler
    "all races to the backend" shape avoids the complexity of an
    in-flight registry while accepting bounded duplicate work.
    """

    def __init__(
        self,
        inner: BuyerAgentRegistry,
        *,
        ttl_seconds: float = 60.0,
        max_entries: int = 4096,
        hit_callback: MetricCallback | None = None,
        audit_sink: AuditSink | None = None,
        sink_timeout_seconds: float = 5.0,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be > 0, got {ttl_seconds!r}")
        if max_entries <= 0:
            raise ValueError(f"max_entries must be > 0, got {max_entries!r}")
        self._inner = inner
        self._ttl = ttl_seconds
        self._max = max_entries
        self._hit_cb = hit_callback
        self._sink = audit_sink
        self._sink_timeout = sink_timeout_seconds
        self._now = time_source
        # OrderedDict gives us O(1) move-to-end for LRU semantics on
        # every hit, plus O(1) popitem(last=False) for eviction.
        self._cache: OrderedDict[tuple[str | None, str], _CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()

    async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        """Resolve via cache, falling through to ``inner`` on miss."""
        tenant_id = _current_tenant_id()
        lookup_key = f"agent_url:{agent_url}"
        key = (tenant_id, lookup_key)
        cached = await self._lookup(key)
        if cached is not None:
            await _emit_audit(
                self._sink,
                operation="buyer_agent_registry.resolve_by_agent_url",
                outcome="cached_hit" if cached.value is not None else "cached_miss",
                lookup_key=lookup_key,
                tenant_id=tenant_id,
                agent=cached.value,
                sink_timeout_seconds=self._sink_timeout,
            )
            return cached.value
        result = await self._inner.resolve_by_agent_url(agent_url)
        await self._store(key, result)
        return result

    async def resolve_by_credential(self, credential: Credential) -> BuyerAgent | None:
        """Resolve via cache, falling through to ``inner`` on miss."""
        tenant_id = _current_tenant_id()
        lookup_key = _credential_key(credential)
        key = (tenant_id, lookup_key)
        cached = await self._lookup(key)
        if cached is not None:
            await _emit_audit(
                self._sink,
                operation="buyer_agent_registry.resolve_by_credential",
                outcome="cached_hit" if cached.value is not None else "cached_miss",
                lookup_key=lookup_key,
                tenant_id=tenant_id,
                agent=cached.value,
                sink_timeout_seconds=self._sink_timeout,
            )
            return cached.value
        result = await self._inner.resolve_by_credential(credential)
        await self._store(key, result)
        return result

    async def _lookup(self, key: tuple[str | None, str]) -> _CacheEntry | None:
        """Return a non-expired cache entry for ``key`` or ``None``.

        Lock held only for the dict mutation; the entry itself is
        immutable so dropping the lock before returning is safe.
        """
        async with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._fire("miss")
                return None
            if entry.expires_at <= self._now():
                # Expired — evict and treat as miss so the caller
                # re-fetches from the inner backend.
                del self._cache[key]
                self._fire("expired")
                return None
            # Promote to most-recently-used.
            self._cache.move_to_end(key)
            self._fire("hit" if entry.value is not None else "negative_hit")
            return entry

    async def _store(self, key: tuple[str | None, str], value: BuyerAgent | None) -> None:
        async with self._lock:
            self._cache[key] = _CacheEntry(
                value=value,
                expires_at=self._now() + self._ttl,
            )
            self._cache.move_to_end(key)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)

    def _fire(self, label: str) -> None:
        if self._hit_cb is not None:
            try:
                self._hit_cb(label)
            except Exception:  # noqa: BLE001 — metric callback must not break dispatch
                logger.warning(
                    "[adcp.registry_cache] hit_callback raised for label=%s",
                    label,
                    exc_info=True,
                )

    async def invalidate(self, *, tenant_id: str | None, lookup_key: str) -> None:
        """Drop a single ``(tenant_id, lookup_key)`` entry.

        Called by admin / management code on a status flip — e.g.,
        when an operator suspends an agent the management API calls
        ``invalidate`` so the next dispatch sees the new status
        immediately rather than waiting for TTL expiry.

        Async + lock-held because admin paths run concurrently with
        dispatch traffic; mutating the underlying ``OrderedDict``
        while ``_store`` is reordering or evicting can corrupt the
        LRU order or raise ``RuntimeError: OrderedDict mutated
        during iteration``.
        """
        async with self._lock:
            self._cache.pop((tenant_id, lookup_key), None)

    def clear_sync(self) -> None:
        """Drop every cached entry from a sync context.

        Safe to call from any thread or coroutine without an event
        loop. Atomic via the GIL on :meth:`OrderedDict.clear` — no
        lock acquired, so a concurrent async ``_lookup`` / ``_store``
        may observe either the pre-clear or post-clear dict. The
        worst case is one extra round-trip to the inner registry on
        the next resolve, which is exactly what an invalidation is
        supposed to cause.

        Use cases: mutation-observer hooks wired by
        :meth:`PgBuyerAgentRegistry.with_caching`, post-config-reload
        flushes from sync admin code.

        Full-clear (rather than per-key drop) trades a small amount
        of over-invalidation for simplicity: mutations are admin-rare
        and the next traffic burst rebuilds the working set within
        TTL. Adopters needing finer-grained invalidation still have
        :meth:`invalidate` for explicit ``(tenant_id, lookup_key)``
        drops.
        """
        self._cache.clear()

    async def clear(self) -> None:
        """Drop every cached entry. For tests + post-config-reload.

        Async + lock-held for the same reason as :meth:`invalidate`.
        """
        async with self._lock:
            self._cache.clear()


# ----- Token-bucket rate limiter ------------------------------------


@dataclass
class _Bucket:
    """Token-bucket state for one ``(tenant, lookup_key)`` pair."""

    tokens: float
    last_refill: float


class RateLimitedBuyerAgentRegistry:
    """Per-tenant token-bucket rate limiter wrapping a
    :class:`BuyerAgentRegistry`.

    Sized for the credential-stuffing oracle: the registry's
    :meth:`resolve_by_credential` is queryable with arbitrary
    ``key_id`` strings; without a rate limit, an attacker can
    enumerate the keyspace at line rate. The bucket sits between
    the request and the DB so probe traffic gets rejected before
    the SQL query runs.

    :param inner: The wrapped :class:`BuyerAgentRegistry`.
    :param rps_per_tenant: Steady-state requests per second per
        ``(tenant_id, lookup_key)`` bucket. Default 100 — high
        enough to absorb a real buyer's storyboard burst, low
        enough that an enumeration probe at line rate gets cut off.
    :param burst: Maximum bucket capacity (tokens). Default
        ``rps_per_tenant`` so a steady state can sustain
        ``rps_per_tenant`` calls/sec but bursts are capped at the
        same number. Adopters with bursty real traffic raise this.
    :param audit_sink: Optional audit sink — emits ``rate_limited``
        events when the bucket is exhausted. The most interesting
        event for security review (repeated rate-limit exhaustion
        is the credential-stuffing signal an attacker is actively
        probing).
    :param time_source: Override for tests — defaults to
        :func:`time.monotonic`.

    Failure mode
    ------------

    On bucket exhaustion, raises :class:`AdcpError`
    ``PERMISSION_DENIED`` with NO ``details`` and a generic message
    matching the registry-miss path. This is deliberate — a distinct
    ``RATE_LIMITED`` code or any populated ``details`` field would
    itself be an enumeration oracle (the attacker learns "this
    ``agent_url`` is interesting enough to be rate-limited"). The
    spec's omit-on-unestablished-identity rule from PR #393 applies.
    """

    def __init__(
        self,
        inner: BuyerAgentRegistry,
        *,
        rps_per_tenant: float = 100.0,
        burst: float | None = None,
        audit_sink: AuditSink | None = None,
        sink_timeout_seconds: float = 5.0,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        if rps_per_tenant <= 0:
            raise ValueError(f"rps_per_tenant must be > 0, got {rps_per_tenant!r}")
        if burst is not None and burst <= 0:
            raise ValueError(f"burst must be > 0, got {burst!r}")
        self._inner = inner
        self._rate = rps_per_tenant
        self._burst = burst if burst is not None else rps_per_tenant
        self._sink = audit_sink
        self._sink_timeout = sink_timeout_seconds
        self._now = time_source
        self._buckets: dict[tuple[str | None, str], _Bucket] = {}
        self._lock = asyncio.Lock()

    async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        tenant_id = _current_tenant_id()
        lookup_key = f"agent_url:{agent_url}"
        await self._charge(
            (tenant_id, lookup_key),
            operation="buyer_agent_registry.resolve_by_agent_url",
            tenant_id=tenant_id,
        )
        return await self._inner.resolve_by_agent_url(agent_url)

    async def resolve_by_credential(self, credential: Credential) -> BuyerAgent | None:
        tenant_id = _current_tenant_id()
        lookup_key = _credential_key(credential)
        await self._charge(
            (tenant_id, lookup_key),
            operation="buyer_agent_registry.resolve_by_credential",
            tenant_id=tenant_id,
        )
        return await self._inner.resolve_by_credential(credential)

    async def _charge(
        self,
        key: tuple[str | None, str],
        *,
        operation: str,
        tenant_id: str | None,
    ) -> None:
        """Spend one token from ``key``'s bucket; raise + audit on
        exhaustion."""
        now = self._now()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # New bucket — start full so a fresh tenant gets the
                # burst allowance immediately.
                bucket = _Bucket(tokens=self._burst, last_refill=now)
                self._buckets[key] = bucket
            else:
                # Refill at ``rate`` tokens/sec, capped at ``burst``.
                elapsed = now - bucket.last_refill
                bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate)
                bucket.last_refill = now
            if bucket.tokens < 1.0:
                exhausted = True
            else:
                bucket.tokens -= 1.0
                exhausted = False
        if exhausted:
            # Audit emission OUTSIDE the lock — the sink may be slow.
            await _emit_audit(
                self._sink,
                operation=operation,
                outcome="rate_limited",
                lookup_key=key[1],
                tenant_id=tenant_id,
                sink_timeout_seconds=self._sink_timeout,
            )
            raise _denied_error()


# ----- Audit-emitting terminal wrapper -----------------------------


class AuditingBuyerAgentRegistry:
    """Terminal wrapper that emits one :class:`AuditEvent` per
    resolution outcome from the inner store.

    Wrap the SQL-backed registry with this so every DB lookup
    (``resolved`` / ``miss``) lands in the audit trail. Compliance
    teams reconstruct who tried what when from these records;
    SecOps correlates spikes in ``miss`` events with
    credential-stuffing activity.

    The event ``operation`` is namespaced
    (``"buyer_agent_registry.resolve_by_agent_url"`` /
    ``"...resolve_by_credential"``) so audit queries can filter
    registry traffic from the higher-level skill dispatches.

    :param inner: Wrapped :class:`BuyerAgentRegistry` — typically
        the actual SQL-backed impl.
    :param audit_sink: :class:`AuditSink` to write events to. If
        ``None``, outcomes log at ``DEBUG`` instead.
    :param sink_timeout_seconds: Per-sink timeout. Default 5s
        matching :func:`adcp.audit_sink.make_audit_middleware`. A
        sink that wedges (DB stall, S3 outage) NEVER blocks dispatch.
    """

    def __init__(
        self,
        inner: BuyerAgentRegistry,
        *,
        audit_sink: AuditSink | None = None,
        sink_timeout_seconds: float = 5.0,
    ) -> None:
        self._inner = inner
        self._sink = audit_sink
        self._sink_timeout = sink_timeout_seconds

    async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        tenant_id = _current_tenant_id()
        result = await self._inner.resolve_by_agent_url(agent_url)
        await _emit_audit(
            self._sink,
            operation="buyer_agent_registry.resolve_by_agent_url",
            outcome="resolved" if result is not None else "miss",
            lookup_key=f"agent_url:{agent_url}",
            tenant_id=tenant_id,
            agent=result,
            sink_timeout_seconds=self._sink_timeout,
        )
        return result

    async def resolve_by_credential(self, credential: Credential) -> BuyerAgent | None:
        tenant_id = _current_tenant_id()
        result = await self._inner.resolve_by_credential(credential)
        await _emit_audit(
            self._sink,
            operation="buyer_agent_registry.resolve_by_credential",
            outcome="resolved" if result is not None else "miss",
            lookup_key=_credential_key(credential),
            tenant_id=tenant_id,
            agent=result,
            sink_timeout_seconds=self._sink_timeout,
        )
        return result


__all__ = [
    "AuditingBuyerAgentRegistry",
    "CachingBuyerAgentRegistry",
    "MetricCallback",
    "RateLimitedBuyerAgentRegistry",
    "ResolveOutcome",
]
