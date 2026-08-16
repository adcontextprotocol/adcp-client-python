"""Tests for :mod:`adcp.decisioning.registry_cache` — caching,
rate-limiting, and audit-emission wrappers around
:class:`~adcp.decisioning.BuyerAgentRegistry`.

Behavior under test:

* Cache returns the same object on hit as on miss.
* Negative resolutions (``None``) are also cached so an enumeration
  probe doesn't repeatedly hit the DB.
* TTL expiry forces a re-fetch from the inner store.
* Rate limit raises spec-conformant ``PERMISSION_DENIED`` (no
  ``details``) when the bucket is exhausted — wire-uniform with
  the registry-miss path.
* Audit emission fires for every outcome label
  (``resolved`` / ``miss`` / ``cached_hit`` / ``cached_miss`` /
  ``rate_limited``).
* Wrappers compose end-to-end through chained construction.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from adcp.audit_sink import AuditEvent
from adcp.decisioning.registry import (
    ApiKeyCredential,
    BuyerAgent,
    OAuthCredential,
)
from adcp.decisioning.registry_cache import (
    AuditingBuyerAgentRegistry,
    CachingBuyerAgentRegistry,
    RateLimitedBuyerAgentRegistry,
)
from adcp.decisioning.types import AdcpError

# ----- Test doubles --------------------------------------------------


class FakeRegistry:
    """In-memory :class:`BuyerAgentRegistry` for the wrapper tests.

    Tracks call counts so tests can assert cache-hit / miss behavior
    without faking an entire DB. Returns ``None`` by default unless
    the caller seeded the lookup map.
    """

    def __init__(
        self,
        *,
        agents: dict[str, BuyerAgent] | None = None,
        credentials: dict[str, BuyerAgent] | None = None,
    ) -> None:
        self._agents = agents or {}
        self._credentials = credentials or {}
        self.agent_url_calls = 0
        self.credential_calls = 0

    async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        self.agent_url_calls += 1
        return self._agents.get(agent_url)

    async def resolve_by_credential(
        self, credential: ApiKeyCredential | OAuthCredential
    ) -> BuyerAgent | None:
        self.credential_calls += 1
        if isinstance(credential, ApiKeyCredential):
            return self._credentials.get(credential.key_id)
        return self._credentials.get(credential.client_id)


class CapturingAuditSink:
    """In-memory :class:`AuditSink` for tests."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class FakeClock:
    """Manually-advanced clock for TTL / rate-limit refill tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ----- Cache: hit returns same object -----------------------------------


@pytest.mark.asyncio
async def test_cache_hit_returns_same_object_as_miss() -> None:
    """On cache hit the wrapper returns the SAME BuyerAgent instance
    the inner store returned on the original miss — the cache is a
    pass-through, not a value-copy."""
    expected = BuyerAgent(
        agent_url="https://agent.example/",
        display_name="Acme",
        status="active",
    )
    inner = FakeRegistry(agents={"https://agent.example/": expected})
    cache = CachingBuyerAgentRegistry(inner, ttl_seconds=60.0)

    first = await cache.resolve_by_agent_url("https://agent.example/")
    second = await cache.resolve_by_agent_url("https://agent.example/")

    assert first is expected
    assert second is expected
    # Inner only hit once — second call served from cache.
    assert inner.agent_url_calls == 1


# ----- Cache: negative caching prevents repeat DB hits -------------------


@pytest.mark.asyncio
async def test_cache_negatively_caches_unknown_agents() -> None:
    """An enumeration probe walking unknown ``agent_url`` strings
    must not be able to spam the DB. Negative results are cached
    just like positive ones."""
    inner = FakeRegistry(agents={})  # everything misses
    cache = CachingBuyerAgentRegistry(inner, ttl_seconds=60.0)

    for _ in range(5):
        result = await cache.resolve_by_agent_url("https://unknown.example/")
        assert result is None

    # Inner was only hit ONCE despite 5 calls. Without negative
    # caching this would be 5 — the DB protection that closes the
    # credential-stuffing oracle.
    assert inner.agent_url_calls == 1


@pytest.mark.asyncio
async def test_cache_negative_credential_lookup_only_hits_inner_once() -> None:
    inner = FakeRegistry(credentials={})
    cache = CachingBuyerAgentRegistry(inner, ttl_seconds=60.0)
    cred = ApiKeyCredential(kind="api_key", key_id="probe-key")

    for _ in range(10):
        assert await cache.resolve_by_credential(cred) is None

    assert inner.credential_calls == 1


# ----- Cache: TTL expiry forces re-fetch --------------------------------


@pytest.mark.asyncio
async def test_cache_ttl_expiry_causes_refetch() -> None:
    """After ``ttl_seconds`` elapses the cached entry expires and
    the next call re-fetches from the inner store."""
    expected = BuyerAgent(
        agent_url="https://agent.example/",
        display_name="Acme",
        status="active",
    )
    inner = FakeRegistry(agents={"https://agent.example/": expected})
    clock = FakeClock(start=0.0)
    cache = CachingBuyerAgentRegistry(
        inner,
        ttl_seconds=10.0,
        time_source=clock,
    )

    await cache.resolve_by_agent_url("https://agent.example/")
    assert inner.agent_url_calls == 1

    # Within the TTL window — cached.
    clock.advance(5.0)
    await cache.resolve_by_agent_url("https://agent.example/")
    assert inner.agent_url_calls == 1

    # Past the TTL — refetch.
    clock.advance(6.0)  # total 11 > 10
    await cache.resolve_by_agent_url("https://agent.example/")
    assert inner.agent_url_calls == 2


# ----- Cache: LRU eviction enforces max_entries --------------------------


@pytest.mark.asyncio
async def test_cache_evicts_least_recently_used_when_capacity_full() -> None:
    inner = FakeRegistry(agents={})
    cache = CachingBuyerAgentRegistry(inner, ttl_seconds=600.0, max_entries=3)

    # Populate 4 distinct keys — the first should evict.
    for i in range(4):
        await cache.resolve_by_agent_url(f"https://agent-{i}/")
    assert inner.agent_url_calls == 4

    # The first key was evicted — re-fetching it hits inner again.
    await cache.resolve_by_agent_url("https://agent-0/")
    assert inner.agent_url_calls == 5

    # The most-recent ones are still cached.
    await cache.resolve_by_agent_url("https://agent-3/")
    assert inner.agent_url_calls == 5


# ----- Cache: invalidate / clear -----------------------------------------


@pytest.mark.asyncio
async def test_cache_invalidate_drops_single_entry() -> None:
    expected = BuyerAgent(
        agent_url="https://agent.example/",
        display_name="Acme",
        status="active",
    )
    inner = FakeRegistry(agents={"https://agent.example/": expected})
    cache = CachingBuyerAgentRegistry(inner, ttl_seconds=600.0)

    await cache.resolve_by_agent_url("https://agent.example/")
    assert inner.agent_url_calls == 1

    await cache.invalidate(tenant_id=None, lookup_key="agent_url:https://agent.example/")

    await cache.resolve_by_agent_url("https://agent.example/")
    assert inner.agent_url_calls == 2


# ----- Cache: hit_callback fires correct labels -------------------------


@pytest.mark.asyncio
async def test_cache_hit_callback_fires_per_outcome() -> None:
    expected = BuyerAgent(
        agent_url="https://agent.example/",
        display_name="Acme",
        status="active",
    )
    inner = FakeRegistry(agents={"https://agent.example/": expected})
    labels: list[str] = []
    cache = CachingBuyerAgentRegistry(
        inner,
        ttl_seconds=60.0,
        hit_callback=labels.append,
    )

    await cache.resolve_by_agent_url("https://agent.example/")  # miss
    await cache.resolve_by_agent_url("https://agent.example/")  # hit
    await cache.resolve_by_agent_url("https://unknown/")  # miss
    await cache.resolve_by_agent_url("https://unknown/")  # negative_hit

    assert labels == ["miss", "hit", "miss", "negative_hit"]


# ----- Rate limit: raises PERMISSION_DENIED at threshold ----------------


@pytest.mark.asyncio
async def test_rate_limit_raises_permission_denied_at_threshold() -> None:
    """The token bucket starts full at ``burst``. After ``burst``
    successive calls within one tick the next call exhausts the
    bucket and raises :class:`AdcpError` with code
    ``PERMISSION_DENIED`` and NO ``details`` — wire-uniform with the
    registry-miss path so the rejection isn't an enumeration oracle."""
    inner = FakeRegistry()
    clock = FakeClock(start=0.0)
    limiter = RateLimitedBuyerAgentRegistry(
        inner,
        rps_per_tenant=2.0,  # 2 tokens per second; burst=2
        time_source=clock,
    )

    # First 2 calls succeed (initial burst capacity).
    await limiter.resolve_by_agent_url("https://agent/")
    await limiter.resolve_by_agent_url("https://agent/")

    # Third call within the same tick — exhausted.
    with pytest.raises(AdcpError) as exc_info:
        await limiter.resolve_by_agent_url("https://agent/")

    err = exc_info.value
    assert err.code == "PERMISSION_DENIED"
    assert err.recovery == "correctable"
    # CRITICAL: details must be EMPTY — wire-uniform with registry
    # miss. A populated details (or distinct code) would itself be
    # an enumeration oracle.
    assert err.details == {}


@pytest.mark.asyncio
async def test_rate_limit_refills_over_time() -> None:
    inner = FakeRegistry()
    clock = FakeClock(start=0.0)
    limiter = RateLimitedBuyerAgentRegistry(
        inner,
        rps_per_tenant=10.0,
        time_source=clock,
    )

    # Drain the bucket.
    for _ in range(10):
        await limiter.resolve_by_agent_url("https://agent/")
    with pytest.raises(AdcpError):
        await limiter.resolve_by_agent_url("https://agent/")

    # Advance time so the bucket refills.
    clock.advance(1.0)  # 10 tokens regenerated
    await limiter.resolve_by_agent_url("https://agent/")  # should not raise


@pytest.mark.asyncio
async def test_rate_limit_aggregate_blocks_rotating_lookup_keys() -> None:
    """Fresh identifiers cannot bypass the tenant's aggregate budget."""
    inner = FakeRegistry()
    clock = FakeClock(start=0.0)
    limiter = RateLimitedBuyerAgentRegistry(
        inner,
        rps_per_tenant=1.0,
        time_source=clock,
    )

    await limiter.resolve_by_agent_url("https://agent-A/")
    with pytest.raises(AdcpError):
        await limiter.resolve_by_agent_url("https://agent-B/")


@pytest.mark.asyncio
async def test_rate_limit_bucket_state_is_bounded() -> None:
    limiter = RateLimitedBuyerAgentRegistry(
        FakeRegistry(),
        rps_per_tenant=100.0,
        burst=100.0,
        rps_per_lookup=100.0,
        max_buckets=4,
        time_source=FakeClock(start=0.0),
    )
    for suffix in ("A", "B", "C", "D"):
        await limiter.resolve_by_agent_url(f"https://agent-{suffix}/")
    tenant_buckets = limiter._buckets[None]
    assert 1 + len(tenant_buckets.lookups) == 4
    assert set(tenant_buckets.lookups) == {
        "agent_url:https://agent-B/",
        "agent_url:https://agent-C/",
        "agent_url:https://agent-D/",
    }


@pytest.mark.asyncio
async def test_rate_limit_bucket_cap_is_isolated_per_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One tenant's lookup identities cannot crowd out another tenant."""
    tenant = "tenant-a"
    monkeypatch.setattr(
        "adcp.decisioning.registry_cache._current_tenant_id",
        lambda: tenant,
    )
    limiter = RateLimitedBuyerAgentRegistry(
        FakeRegistry(),
        rps_per_tenant=100.0,
        rps_per_lookup=100.0,
        max_buckets=3,
        time_source=FakeClock(start=0.0),
    )

    await limiter.resolve_by_agent_url("https://agent-A/")
    await limiter.resolve_by_agent_url("https://agent-B/")
    await limiter.resolve_by_agent_url("https://agent-C/")

    tenant = "tenant-b"
    await limiter.resolve_by_agent_url("https://agent-C/")
    assert set(limiter._buckets) == {"tenant-a", "tenant-b"}


@pytest.mark.asyncio
async def test_rate_limit_hot_key_does_not_drain_tenant_aggregate() -> None:
    """Rejected hot-key probes leave capacity for unrelated lookups."""
    limiter = RateLimitedBuyerAgentRegistry(
        FakeRegistry(),
        rps_per_tenant=2.0,
        burst=2.0,
        rps_per_lookup=1.0,
        lookup_burst=1.0,
        time_source=FakeClock(start=0.0),
    )

    await limiter.resolve_by_agent_url("https://hot-key/")
    for _ in range(5):
        with pytest.raises(AdcpError):
            await limiter.resolve_by_agent_url("https://hot-key/")

    await limiter.resolve_by_agent_url("https://unrelated-key/")


@pytest.mark.asyncio
async def test_rate_limit_idle_buckets_expire() -> None:
    clock = FakeClock(start=0.0)
    limiter = RateLimitedBuyerAgentRegistry(
        FakeRegistry(),
        rps_per_tenant=100.0,
        rps_per_lookup=100.0,
        max_buckets=4,
        bucket_idle_ttl_seconds=10.0,
        time_source=clock,
    )
    await limiter.resolve_by_agent_url("https://agent-A/")
    await limiter.resolve_by_agent_url("https://agent-B/")
    assert 1 + len(limiter._buckets[None].lookups) == 3
    clock.advance(11.0)
    await limiter.resolve_by_agent_url("https://agent-C/")
    assert 1 + len(limiter._buckets[None].lookups) == 2


@pytest.mark.asyncio
async def test_rate_limit_active_tenant_reclaims_only_idle_lookup_slots() -> None:
    clock = FakeClock(start=0.0)
    limiter = RateLimitedBuyerAgentRegistry(
        FakeRegistry(),
        rps_per_tenant=100.0,
        rps_per_lookup=100.0,
        max_buckets=3,
        bucket_idle_ttl_seconds=10.0,
        time_source=clock,
    )
    await limiter.resolve_by_agent_url("https://stale/")
    await limiter.resolve_by_agent_url("https://active/")
    clock.advance(6.0)
    await limiter.resolve_by_agent_url("https://active/")
    clock.advance(5.0)

    await limiter.resolve_by_agent_url("https://replacement/")
    lookup_keys = set(limiter._buckets[None].lookups)
    assert "agent_url:https://stale/" not in lookup_keys
    assert lookup_keys == {
        "agent_url:https://active/",
        "agent_url:https://replacement/",
    }


# ----- Audit emission: every outcome fires an event --------------------


@pytest.mark.asyncio
async def test_audit_emits_resolved_outcome() -> None:
    expected = BuyerAgent(
        agent_url="https://agent.example/",
        display_name="Acme",
        status="active",
    )
    inner = FakeRegistry(agents={"https://agent.example/": expected})
    sink = CapturingAuditSink()
    audited = AuditingBuyerAgentRegistry(inner, audit_sink=sink)

    result = await audited.resolve_by_agent_url("https://agent.example/")

    assert result is expected
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.operation == "buyer_agent_registry.resolve_by_agent_url"
    assert event.success is True
    assert event.details["outcome"] == "resolved"
    assert event.details["agent_url"] == "https://agent.example/"
    assert event.details["agent_status"] == "active"


@pytest.mark.asyncio
async def test_audit_emits_miss_outcome() -> None:
    inner = FakeRegistry(agents={})
    sink = CapturingAuditSink()
    audited = AuditingBuyerAgentRegistry(inner, audit_sink=sink)

    result = await audited.resolve_by_agent_url("https://unknown/")

    assert result is None
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.success is False
    assert event.details["outcome"] == "miss"
    # Misses do NOT include agent_url in details — only the lookup_key.
    assert "agent_url" not in event.details
    assert event.details["lookup_key"] == "agent_url:https://unknown/"


@pytest.mark.asyncio
async def test_audit_emits_credential_resolution() -> None:
    expected = BuyerAgent(
        agent_url="https://agent.example/",
        display_name="Acme",
        status="active",
    )
    inner = FakeRegistry(credentials={"key-1": expected})
    sink = CapturingAuditSink()
    audited = AuditingBuyerAgentRegistry(inner, audit_sink=sink)

    await audited.resolve_by_credential(ApiKeyCredential(kind="api_key", key_id="key-1"))

    assert len(sink.events) == 1
    assert sink.events[0].operation == "buyer_agent_registry.resolve_by_credential"
    assert sink.events[0].details["lookup_key"] == "api_key:key-1"


@pytest.mark.asyncio
async def test_audit_sink_failure_does_not_break_resolve() -> None:
    """A misbehaving sink (DB stall, S3 outage) must NEVER block the
    registry resolution — failures are bounded + swallowed."""

    class BrokenSink:
        async def record(self, event: AuditEvent) -> None:
            raise RuntimeError("sink down")

    inner = FakeRegistry(
        agents={
            "https://agent.example/": BuyerAgent(
                agent_url="https://agent.example/",
                display_name="Acme",
                status="active",
            )
        }
    )
    audited = AuditingBuyerAgentRegistry(inner, audit_sink=BrokenSink())  # type: ignore[arg-type]

    result = await audited.resolve_by_agent_url("https://agent.example/")
    assert result is not None  # resolution still succeeded


@pytest.mark.asyncio
async def test_audit_sink_timeout_does_not_break_resolve() -> None:
    class StallingSink:
        async def record(self, event: AuditEvent) -> None:
            await asyncio.sleep(60.0)  # would hang indefinitely

    inner = FakeRegistry(
        agents={
            "https://agent.example/": BuyerAgent(
                agent_url="https://agent.example/",
                display_name="Acme",
                status="active",
            )
        }
    )
    audited = AuditingBuyerAgentRegistry(
        inner,
        audit_sink=StallingSink(),  # type: ignore[arg-type]
        sink_timeout_seconds=0.05,
    )

    # The sink would hang for 60s — but the wrapper bounds at 0.05s.
    result = await asyncio.wait_for(
        audited.resolve_by_agent_url("https://agent.example/"),
        timeout=2.0,
    )
    assert result is not None


# ----- Cache emits cached_hit / cached_miss to the same sink -------------


@pytest.mark.asyncio
async def test_cache_emits_cached_hit_event() -> None:
    expected = BuyerAgent(
        agent_url="https://agent.example/",
        display_name="Acme",
        status="active",
    )
    inner = FakeRegistry(agents={"https://agent.example/": expected})
    sink = CapturingAuditSink()
    cache = CachingBuyerAgentRegistry(inner, ttl_seconds=60.0, audit_sink=sink)

    # First call: miss — falls through to inner. Cache layer does
    # NOT emit on miss (would double-count if inner has its own
    # audit wrapper).
    await cache.resolve_by_agent_url("https://agent.example/")
    assert sink.events == []

    # Second call: hit — the cache emits cached_hit.
    await cache.resolve_by_agent_url("https://agent.example/")
    assert len(sink.events) == 1
    assert sink.events[0].details["outcome"] == "cached_hit"


@pytest.mark.asyncio
async def test_cache_emits_cached_miss_event_for_negative_hit() -> None:
    """Negative cache hit (cached ``None``) emits ``cached_miss`` so
    enumeration-probe traffic is still visible in the audit trail."""
    inner = FakeRegistry(agents={})
    sink = CapturingAuditSink()
    cache = CachingBuyerAgentRegistry(inner, ttl_seconds=60.0, audit_sink=sink)

    await cache.resolve_by_agent_url("https://unknown/")  # initial miss, no event
    await cache.resolve_by_agent_url("https://unknown/")  # cached negative — emits

    assert len(sink.events) == 1
    assert sink.events[0].details["outcome"] == "cached_miss"
    assert sink.events[0].success is False


# ----- Rate limit emits rate_limited audit event ---------------------


@pytest.mark.asyncio
async def test_rate_limit_emits_audit_event_on_exhaustion() -> None:
    inner = FakeRegistry()
    clock = FakeClock(start=0.0)
    sink = CapturingAuditSink()
    limiter = RateLimitedBuyerAgentRegistry(
        inner,
        rps_per_tenant=1.0,
        time_source=clock,
        audit_sink=sink,
    )

    await limiter.resolve_by_agent_url("https://agent/")  # bucket starts full
    with pytest.raises(AdcpError):
        await limiter.resolve_by_agent_url("https://agent/")

    # Exactly one rate-limited event was emitted.
    rate_limited_events = [e for e in sink.events if e.details.get("outcome") == "rate_limited"]
    assert len(rate_limited_events) == 1
    assert rate_limited_events[0].success is False
    assert rate_limited_events[0].operation == "buyer_agent_registry.resolve_by_agent_url"


# ----- Composability: chained wrappers work end-to-end ------------------


@pytest.mark.asyncio
async def test_composed_stack_chains_cache_rate_limit_audit() -> None:
    """Build the production stack and verify each layer participates:

    * Cache shortcuts repeat lookups (single inner call across N
      cache hits).
    * Rate limit fires on exhaustion (and emits rate_limited).
    * Audit sink sees ``resolved`` (from inner audit layer) AND
      ``cached_hit`` (from cache layer).
    """
    expected = BuyerAgent(
        agent_url="https://agent.example/",
        display_name="Acme",
        status="active",
    )
    inner = FakeRegistry(agents={"https://agent.example/": expected})
    sink = CapturingAuditSink()

    # Production stack — same shape as the v3 reference seller.
    audited = AuditingBuyerAgentRegistry(inner, audit_sink=sink)
    rate_limited = RateLimitedBuyerAgentRegistry(
        audited,
        rps_per_tenant=1000.0,
        audit_sink=sink,
    )
    stack = CachingBuyerAgentRegistry(
        rate_limited,
        ttl_seconds=60.0,
        audit_sink=sink,
    )

    # 3 calls — first miss, two cached hits.
    for _ in range(3):
        result = await stack.resolve_by_agent_url("https://agent.example/")
        assert result is expected

    # Inner DB only hit once.
    assert inner.agent_url_calls == 1

    # Audit events: 1 ``resolved`` (from terminal layer) + 2
    # ``cached_hit`` (from cache layer). Rate limit didn't fire.
    outcomes = [e.details["outcome"] for e in sink.events]
    assert outcomes.count("resolved") == 1
    assert outcomes.count("cached_hit") == 2


@pytest.mark.asyncio
async def test_composed_stack_caches_negative_resolutions_and_audits_them() -> None:
    """End-to-end: a probe walking unknown agents hits the DB once,
    then is served from the negative cache. Audit sink sees one
    ``miss`` and N-1 ``cached_miss`` events."""
    inner = FakeRegistry(agents={})
    sink = CapturingAuditSink()

    audited = AuditingBuyerAgentRegistry(inner, audit_sink=sink)
    rate_limited = RateLimitedBuyerAgentRegistry(
        audited,
        rps_per_tenant=1000.0,
        audit_sink=sink,
    )
    stack = CachingBuyerAgentRegistry(
        rate_limited,
        ttl_seconds=60.0,
        audit_sink=sink,
    )

    for _ in range(5):
        assert await stack.resolve_by_agent_url("https://probe/") is None

    assert inner.agent_url_calls == 1
    outcomes = [e.details["outcome"] for e in sink.events]
    assert outcomes.count("miss") == 1
    assert outcomes.count("cached_miss") == 4


# ----- Validation of constructor args ---------------------------------


def test_cache_rejects_zero_ttl() -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        CachingBuyerAgentRegistry(FakeRegistry(), ttl_seconds=0.0)


def test_cache_rejects_zero_max_entries() -> None:
    with pytest.raises(ValueError, match="max_entries"):
        CachingBuyerAgentRegistry(FakeRegistry(), max_entries=0)


def test_rate_limit_rejects_zero_rps() -> None:
    with pytest.raises(ValueError, match="rps_per_tenant"):
        RateLimitedBuyerAgentRegistry(FakeRegistry(), rps_per_tenant=0.0)


def test_rate_limit_rejects_idle_ttl_shorter_than_full_refill() -> None:
    with pytest.raises(ValueError, match="burst / rps_per_tenant"):
        RateLimitedBuyerAgentRegistry(
            FakeRegistry(),
            rps_per_tenant=1.0,
            burst=11.0,
            bucket_idle_ttl_seconds=10.0,
        )


def test_rate_limit_rejects_lookup_burst_without_lookup_rate() -> None:
    with pytest.raises(ValueError, match="lookup_burst requires rps_per_lookup"):
        RateLimitedBuyerAgentRegistry(FakeRegistry(), lookup_burst=2.0)


# ----- clear_sync: mutation-observer entry point ---------------------


async def test_clear_sync_drops_all_entries() -> None:
    agent = BuyerAgent(agent_url="https://a.example/", display_name="A", status="active")
    inner = FakeRegistry(agents={agent.agent_url: agent})
    cache = CachingBuyerAgentRegistry(inner, ttl_seconds=60.0)

    # Seed positive + negative cache entries.
    assert await cache.resolve_by_agent_url(agent.agent_url) is agent
    assert await cache.resolve_by_agent_url("https://miss.example/") is None
    assert inner.agent_url_calls == 2

    cache.clear_sync()

    # Both cached entries dropped — next reads hit the inner registry.
    assert await cache.resolve_by_agent_url(agent.agent_url) is agent
    assert await cache.resolve_by_agent_url("https://miss.example/") is None
    assert inner.agent_url_calls == 4


def test_clear_sync_is_callable_from_sync_context() -> None:
    # Critical: must not require a running event loop. Mutation
    # observers fire from PgBuyerAgentRegistry's sync mutation path.
    cache = CachingBuyerAgentRegistry(FakeRegistry(), ttl_seconds=60.0)
    # Synchronous invocation — no asyncio.run() wrapper.
    cache.clear_sync()  # would raise RuntimeError if it touched asyncio.Lock


# ----- Suppress unused import warning for clarity in ide -----------


def _imports_intact() -> Any:
    return OAuthCredential
