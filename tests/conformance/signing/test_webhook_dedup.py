"""Tests for the webhook receiver-side claim and completion store."""

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
from adcp.server.idempotency.webhook_dedup import WebhookDedupOwnershipError

PAYLOAD_A = "a" * 64
PAYLOAD_B = "b" * 64


@pytest.fixture
def store() -> WebhookDedupStore:
    return WebhookDedupStore(MemoryBackend(), ttl_seconds=86400)


@pytest.mark.asyncio
async def test_claim_then_complete_becomes_handled_duplicate(store: WebhookDedupStore) -> None:
    first = await store.claim("sender-1", "whk_abc", PAYLOAD_A)
    assert first.status == "claimed"
    assert first.claim_token

    concurrent = await store.claim("sender-1", "whk_abc", PAYLOAD_A)
    assert concurrent.status == "in_progress"

    assert await store.complete("sender-1", "whk_abc", PAYLOAD_A, first.claim_token)
    replay = await store.claim("sender-1", "whk_abc", PAYLOAD_A)
    assert replay.status == "handled"


@pytest.mark.asyncio
async def test_changed_payload_conflicts_during_and_after_processing(
    store: WebhookDedupStore,
) -> None:
    claim = await store.claim("sender-1", "whk_abc", PAYLOAD_A)
    assert claim.claim_token
    assert (await store.claim("sender-1", "whk_abc", PAYLOAD_B)).status == "conflict"
    await store.complete("sender-1", "whk_abc", PAYLOAD_A, claim.claim_token)
    assert (await store.claim("sender-1", "whk_abc", PAYLOAD_B)).status == "conflict"


@pytest.mark.asyncio
async def test_release_keeps_binding_and_allows_exact_retry(store: WebhookDedupStore) -> None:
    first = await store.claim("sender-1", "whk_abc", PAYLOAD_A)
    assert first.claim_token
    await store.release("sender-1", "whk_abc", PAYLOAD_A, first.claim_token)

    assert (await store.claim("sender-1", "whk_abc", PAYLOAD_B)).status == "conflict"
    retry = await store.claim("sender-1", "whk_abc", PAYLOAD_A)
    assert retry.status == "claimed"
    assert retry.claim_token != first.claim_token


@pytest.mark.asyncio
async def test_expired_owner_is_fenced_by_replacement() -> None:
    clock = [1_000_000.0]
    store = WebhookDedupStore(
        MemoryBackend(clock=lambda: clock[0]),
        ttl_seconds=86400,
        processing_ttl_seconds=10,
        clock=lambda: clock[0],
    )
    first = await store.claim("sender-1", "whk_abc", PAYLOAD_A)
    assert first.claim_token
    clock[0] += 11
    second = await store.claim("sender-1", "whk_abc", PAYLOAD_A)
    assert second.status == "claimed"
    assert second.claim_token
    with pytest.raises(WebhookDedupOwnershipError):
        await store.complete("sender-1", "whk_abc", PAYLOAD_A, first.claim_token)
    await store.complete("sender-1", "whk_abc", PAYLOAD_A, second.claim_token)


class _LegacyBackend(IdempotencyBackend):
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


@pytest.mark.asyncio
async def test_legacy_backend_warns_and_preserves_claim_state() -> None:
    store = WebhookDedupStore(_LegacyBackend())
    with pytest.warns(DeprecationWarning, match="process-local webhook claim locking"):
        first = await store.claim("sender-1", "whk_legacy", PAYLOAD_A)
    assert first.status == "claimed"
    assert (await store.claim("sender-1", "whk_legacy", PAYLOAD_A)).status == "in_progress"


@pytest.mark.asyncio
async def test_lazy_wrapped_legacy_backend_preserves_fallback_claim() -> None:
    store = WebhookDedupStore(LazyBackend(_LegacyBackend))
    with pytest.warns(DeprecationWarning, match="process-local webhook claim locking"):
        first = await store.claim("sender-1", "whk_lazy_legacy", PAYLOAD_A)
    assert first.status == "claimed"
    assert (await store.claim("sender-1", "whk_lazy_legacy", PAYLOAD_A)).status == "in_progress"


@pytest.mark.asyncio
async def test_legacy_lock_is_shared_across_store_instances() -> None:
    backend = _LegacyBackend()
    stores = [WebhookDedupStore(backend), WebhookDedupStore(backend)]
    with pytest.warns(DeprecationWarning):
        results = await asyncio.gather(
            *(store.claim("sender-1", "whk_shared", PAYLOAD_A) for store in stores)
        )
    assert sorted(result.status for result in results) == ["claimed", "in_progress"]


@pytest.mark.asyncio
async def test_concurrent_deliveries_have_exactly_one_owner(store: WebhookDedupStore) -> None:
    gate = asyncio.Event()

    async def deliver() -> str:
        await gate.wait()
        return (await store.claim("sender-1", "whk_shared", PAYLOAD_A)).status

    tasks = [asyncio.create_task(deliver()) for _ in range(20)]
    gate.set()
    results = await asyncio.gather(*tasks)
    assert results.count("claimed") == 1
    assert results.count("in_progress") == 19


@pytest.mark.asyncio
async def test_different_senders_and_keys_are_independent(store: WebhookDedupStore) -> None:
    assert (await store.claim("sender-1", "whk_a", PAYLOAD_A)).status == "claimed"
    assert (await store.claim("sender-2", "whk_a", PAYLOAD_A)).status == "claimed"
    assert (await store.claim("sender-1", "whk_b", PAYLOAD_A)).status == "claimed"


@pytest.mark.asyncio
async def test_retention_expiry_allows_reprocess() -> None:
    clock = [1_000_000.0]
    store = WebhookDedupStore(
        MemoryBackend(clock=lambda: clock[0]),
        ttl_seconds=86400,
        clock=lambda: clock[0],
    )
    assert (await store.claim("sender-1", "whk_abc", PAYLOAD_A)).status == "claimed"
    clock[0] += 86401
    assert (await store.claim("sender-1", "whk_abc", PAYLOAD_B)).status == "claimed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sender", "key", "payload_hash"),
    [("", "whk_abc", PAYLOAD_A), ("sender-1", "", PAYLOAD_A), ("sender-1", "whk_abc", "")],
)
async def test_rejects_empty_claim_parts(
    store: WebhookDedupStore,
    sender: str,
    key: str,
    payload_hash: str,
) -> None:
    with pytest.raises(ValueError):
        await store.claim(sender, key, payload_hash)


def test_ttl_and_processing_lease_bounds() -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        WebhookDedupStore(MemoryBackend(), ttl_seconds=3600)
    with pytest.raises(ValueError, match="ttl_seconds"):
        WebhookDedupStore(MemoryBackend(), ttl_seconds=604801)
    with pytest.raises(ValueError, match="processing_ttl_seconds"):
        WebhookDedupStore(MemoryBackend(), processing_ttl_seconds=0)
