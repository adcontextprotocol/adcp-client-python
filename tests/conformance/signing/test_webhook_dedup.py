"""Tests for the webhook receiver-side dedup store."""

from __future__ import annotations

import asyncio

import pytest

from adcp.server.idempotency import (
    CachedResponse,
    IdempotencyBackend,
    LazyBackend,
    MemoryBackend,
    WebhookDedupStore,
)


@pytest.fixture
def store() -> WebhookDedupStore:
    return WebhookDedupStore(MemoryBackend(), ttl_seconds=86400)


@pytest.mark.asyncio
async def test_first_seen_returns_true(store: WebhookDedupStore) -> None:
    assert await store.check_and_record("sender-1", "whk_abc") is True


@pytest.mark.asyncio
async def test_repeat_returns_false(store: WebhookDedupStore) -> None:
    await store.check_and_record("sender-1", "whk_abc")
    assert await store.check_and_record("sender-1", "whk_abc") is False


@pytest.mark.asyncio
async def test_legacy_backend_warns_and_preserves_repeat_dedup() -> None:
    class LegacyBackend(IdempotencyBackend):
        def __init__(self) -> None:
            self.entries: dict[tuple[str, str], CachedResponse] = {}

        async def get(self, scope_key: str, key: str) -> CachedResponse | None:
            return self.entries.get((scope_key, key))

        async def put(self, scope_key: str, key: str, entry: CachedResponse) -> None:
            self.entries[(scope_key, key)] = entry

        async def delete_expired(self, now_epoch: float | None = None) -> int:
            return 0

    store = WebhookDedupStore(LegacyBackend())
    with pytest.warns(DeprecationWarning, match="process-local webhook dedup locking"):
        assert await store.check_and_record("sender-1", "whk_legacy") is True
    assert await store.check_and_record("sender-1", "whk_legacy") is False


@pytest.mark.asyncio
async def test_lazy_wrapped_legacy_backend_preserves_fallback_dedup() -> None:
    class LegacyBackend(IdempotencyBackend):
        def __init__(self) -> None:
            self.entries: dict[tuple[str, str], CachedResponse] = {}

        async def get(self, scope_key: str, key: str) -> CachedResponse | None:
            return self.entries.get((scope_key, key))

        async def put(self, scope_key: str, key: str, entry: CachedResponse) -> None:
            self.entries[(scope_key, key)] = entry

        async def delete_expired(self, now_epoch: float | None = None) -> int:
            return 0

    store = WebhookDedupStore(LazyBackend(LegacyBackend))
    with pytest.warns(DeprecationWarning, match="process-local webhook dedup locking"):
        assert await store.check_and_record("sender-1", "whk_lazy_legacy") is True
    assert await store.check_and_record("sender-1", "whk_lazy_legacy") is False


@pytest.mark.asyncio
async def test_legacy_backend_lock_is_shared_across_store_instances() -> None:
    class LegacyBackend(IdempotencyBackend):
        def __init__(self) -> None:
            self.entries: dict[tuple[str, str], CachedResponse] = {}

        async def get(self, scope_key: str, key: str) -> CachedResponse | None:
            await asyncio.sleep(0)
            return self.entries.get((scope_key, key))

        async def put(self, scope_key: str, key: str, entry: CachedResponse) -> None:
            await asyncio.sleep(0)
            self.entries[(scope_key, key)] = entry

        async def delete_expired(self, now_epoch: float | None = None) -> int:
            return 0

    backend = LegacyBackend()
    stores = [WebhookDedupStore(backend), WebhookDedupStore(backend)]
    with pytest.warns(DeprecationWarning):
        results = await asyncio.gather(
            *(store.check_and_record("sender-1", "whk_shared") for store in stores)
        )
    assert sorted(results) == [False, True]


@pytest.mark.asyncio
async def test_concurrent_deliveries_have_exactly_one_first_seen(
    store: WebhookDedupStore,
) -> None:
    gate = asyncio.Event()

    async def deliver() -> bool:
        await gate.wait()
        return await store.check_and_record("sender-1", "whk_shared")

    tasks = [asyncio.create_task(deliver()) for _ in range(20)]
    gate.set()
    results = await asyncio.gather(*tasks)

    assert results.count(True) == 1
    assert results.count(False) == 19


@pytest.mark.asyncio
async def test_different_senders_independent(store: WebhookDedupStore) -> None:
    """Per-sender scoping: the same key from a different sender is fresh."""
    await store.check_and_record("sender-1", "whk_abc")
    assert await store.check_and_record("sender-2", "whk_abc") is True


@pytest.mark.asyncio
async def test_different_keys_independent(store: WebhookDedupStore) -> None:
    await store.check_and_record("sender-1", "whk_a")
    assert await store.check_and_record("sender-1", "whk_b") is True


@pytest.mark.asyncio
async def test_ttl_expiry_allows_reprocess() -> None:
    """Entries past TTL reprocess as fresh — matches spec's 'retries outside
    window are reprocessed' guidance."""
    clock = [1_000_000.0]
    store = WebhookDedupStore(
        MemoryBackend(clock=lambda: clock[0]),
        ttl_seconds=86400,
        clock=lambda: clock[0],
    )

    assert await store.check_and_record("sender-1", "whk_abc") is True
    clock[0] += 86400 + 1  # Advance past TTL
    assert await store.check_and_record("sender-1", "whk_abc") is True


@pytest.mark.asyncio
async def test_rejects_empty_sender(store: WebhookDedupStore) -> None:
    with pytest.raises(ValueError):
        await store.check_and_record("", "whk_abc")


@pytest.mark.asyncio
async def test_rejects_empty_key(store: WebhookDedupStore) -> None:
    with pytest.raises(ValueError):
        await store.check_and_record("sender-1", "")


def test_ttl_spec_bounds() -> None:
    # Below minimum (<24h) should reject — spec contract from webhooks.mdx
    # "Dedup state SHOULD persist for at least 24h".
    with pytest.raises(ValueError, match="ttl_seconds"):
        WebhookDedupStore(MemoryBackend(), ttl_seconds=3600)
    # Over 7 days
    with pytest.raises(ValueError, match="ttl_seconds"):
        WebhookDedupStore(MemoryBackend(), ttl_seconds=604801)
