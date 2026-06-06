"""Tests for the server-side idempotency middleware (AdCP #2315 seller side)."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest
from pydantic import BaseModel

from adcp.exceptions import IdempotencyConflictError
from adcp.server.base import ToolContext
from adcp.server.idempotency import (
    EXCLUDED_FIELDS,
    CachedResponse,
    IdempotencyBackend,
    IdempotencyStore,
    LazyBackend,
    MemoryBackend,
    PgBackend,
    canonical_json_sha256,
    create_lazy_backend,
    strip_excluded_fields,
)


def _without_replay_flag(response: dict[str, Any]) -> dict[str, Any]:
    """Strip the ``replayed`` marker so replay payloads compare equal
    to the original handler response. The store injects ``replayed:
    true`` on cache hits per AdCP L1/security rule 4; tests that
    assert "replay returns the same envelope" use this helper to
    isolate the content equivalence from the marker."""
    return {k: v for k, v in response.items() if k != "replayed"}


class TestCanonicalize:
    """Hashing determinism + exclusion list behavior."""

    def test_same_payload_same_hash(self) -> None:
        a = {"brand": "acme", "budget": 100}
        b = dict(a)
        assert canonical_json_sha256(a) == canonical_json_sha256(b)

    def test_key_order_irrelevant(self) -> None:
        a = canonical_json_sha256({"a": 1, "b": 2, "c": {"x": True, "y": False}})
        b = canonical_json_sha256({"c": {"y": False, "x": True}, "b": 2, "a": 1})
        assert a == b

    def test_different_payload_different_hash(self) -> None:
        a = canonical_json_sha256({"brand": "acme", "budget": 100})
        b = canonical_json_sha256({"brand": "acme", "budget": 101})
        assert a != b

    def test_strip_idempotency_key(self) -> None:
        stripped = strip_excluded_fields({"idempotency_key": "abc123def456ghi7", "brand": "acme"})
        assert stripped == {"brand": "acme"}

    def test_strip_context(self) -> None:
        assert strip_excluded_fields({"context": "opaque", "x": 1}) == {"x": 1}

    def test_strip_governance_context(self) -> None:
        assert strip_excluded_fields({"governance_context": {}, "x": 1}) == {"x": 1}

    def test_strip_nested_push_notification_credentials(self) -> None:
        stripped = strip_excluded_fields(
            {
                "push_notification_config": {
                    "authentication": {
                        "credentials": "secret-token",
                        "scheme": "bearer",
                    },
                    "url": "https://callback.example",
                },
                "brand": "acme",
            }
        )
        # credentials removed; siblings preserved.
        assert stripped == {
            "push_notification_config": {
                "authentication": {"scheme": "bearer"},
                "url": "https://callback.example",
            },
            "brand": "acme",
        }

    def test_strip_nested_missing_path_noop(self) -> None:
        # No push_notification_config → no crash.
        assert strip_excluded_fields({"brand": "acme"}) == {"brand": "acme"}

    def test_strip_preserves_ext(self) -> None:
        # Spec: 'ext' is explicitly IN the hash. Don't strip it.
        payload = {"ext": {"custom": "field"}, "brand": "acme"}
        assert strip_excluded_fields(payload) == payload

    def test_strip_does_not_mutate_input(self) -> None:
        original = {"idempotency_key": "abc123def456ghi7", "brand": "acme"}
        strip_excluded_fields(original)
        assert "idempotency_key" in original

    def test_hash_ignores_idempotency_key(self) -> None:
        # Changing idempotency_key must NOT change the hash — that's the whole
        # point: the spec defines equivalence over the payload minus the key.
        a = canonical_json_sha256({"idempotency_key": "key-one" * 3, "brand": "acme"})
        b = canonical_json_sha256({"idempotency_key": "key-two" * 3, "brand": "acme"})
        assert a == b

    def test_exclusion_set_is_closed(self) -> None:
        # Regression guard — if a maintainer adds fields to EXCLUDED_FIELDS
        # without updating the spec, the test surfaces it. This test locks the
        # closed set to what's actually in the spec.
        assert EXCLUDED_FIELDS == frozenset({"idempotency_key", "context", "governance_context"})


class TestMemoryBackend:
    @pytest.mark.asyncio
    async def test_put_then_get(self) -> None:
        backend = MemoryBackend()
        entry = CachedResponse(
            payload_hash="abc",
            response={"media_buy_id": "mb_1"},
            expires_at_epoch=time.time() + 60,
        )
        await backend.put("principal-a", "key-1", entry)
        got = await backend.get("principal-a", "key-1")
        assert got is not None
        assert got.payload_hash == "abc"
        assert got.response == {"media_buy_id": "mb_1"}

    @pytest.mark.asyncio
    async def test_miss(self) -> None:
        backend = MemoryBackend()
        got = await backend.get("principal-a", "unknown-key")
        assert got is None

    @pytest.mark.asyncio
    async def test_expired_entry_returns_none_and_evicts(self) -> None:
        backend = MemoryBackend()
        entry = CachedResponse(payload_hash="abc", response={}, expires_at_epoch=time.time() - 1)
        await backend.put("principal-a", "key-1", entry)
        assert await backend.get("principal-a", "key-1") is None
        # Lazy eviction should have removed it.
        assert await backend._size() == 0

    @pytest.mark.asyncio
    async def test_per_principal_scope(self) -> None:
        # Same key across different principals must not collide.
        backend = MemoryBackend()
        await backend.put(
            "principal-a",
            "shared-key",
            CachedResponse("h_a", {"who": "a"}, time.time() + 60),
        )
        await backend.put(
            "principal-b",
            "shared-key",
            CachedResponse("h_b", {"who": "b"}, time.time() + 60),
        )
        a = await backend.get("principal-a", "shared-key")
        b = await backend.get("principal-b", "shared-key")
        assert a is not None and a.response == {"who": "a"}
        assert b is not None and b.response == {"who": "b"}

    @pytest.mark.asyncio
    async def test_delete_expired_sweeps(self) -> None:
        backend = MemoryBackend()
        now = time.time()
        await backend.put("principal-a", "fresh", CachedResponse("h", {}, now + 60))
        await backend.put("principal-a", "stale", CachedResponse("h", {}, now - 1))
        removed = await backend.delete_expired(now)
        assert removed == 1
        assert await backend._size() == 1

    @pytest.mark.asyncio
    async def test_concurrent_put_get(self) -> None:
        # Under gather, mutations shouldn't interleave dangerously.
        backend = MemoryBackend()

        async def writer(i: int) -> None:
            await backend.put(
                "principal",
                f"key-{i}",
                CachedResponse(f"h-{i}", {"i": i}, time.time() + 60),
            )

        await asyncio.gather(*[writer(i) for i in range(50)])
        hits = await asyncio.gather(*[backend.get("principal", f"key-{i}") for i in range(50)])
        assert all(h is not None for h in hits)
        assert all(h.response["i"] == i for i, h in enumerate(hits))  # type: ignore[union-attr]


class TestLazyBackend:
    """Deferred-construction wrapper (JS adcp-client#2136 parity).

    The factory must not run at construction; it runs on first use, is
    memoized (resolve-once) even under concurrent first calls, and every
    :class:`IdempotencyBackend` method delegates to the resolved instance.
    ``clear_all`` is opt-in. Prefer a real :class:`MemoryBackend` behind the
    factory over mocking so delegation exercises the actual contract.
    """

    @pytest.mark.asyncio
    async def test_factory_not_called_at_construction(self) -> None:
        calls = 0

        def factory() -> IdempotencyBackend:
            nonlocal calls
            calls += 1
            return MemoryBackend()

        LazyBackend(factory)
        assert calls == 0

    @pytest.mark.asyncio
    async def test_factory_called_on_first_use(self) -> None:
        calls = 0
        inner = MemoryBackend()

        def factory() -> IdempotencyBackend:
            nonlocal calls
            calls += 1
            return inner

        backend = LazyBackend(factory)
        assert calls == 0
        await backend.put("p", "k", CachedResponse("h", {"ok": True}, time.time() + 60))
        assert calls == 1
        got = await backend.get("p", "k")
        assert got is not None and got.response == {"ok": True}
        # Delegated to the same underlying instance.
        assert await inner.get("p", "k") is not None

    @pytest.mark.asyncio
    async def test_async_factory_resolved(self) -> None:
        inner = MemoryBackend()

        async def factory() -> IdempotencyBackend:
            return inner

        backend = create_lazy_backend(factory)
        await backend.put("p", "k", CachedResponse("h", {"v": 1}, time.time() + 60))
        assert await inner.get("p", "k") is not None

    @pytest.mark.asyncio
    async def test_resolved_once_across_operations(self) -> None:
        calls = 0
        inner = MemoryBackend()

        async def factory() -> IdempotencyBackend:
            nonlocal calls
            calls += 1
            return inner

        backend = LazyBackend(factory)
        await backend.put("p", "k1", CachedResponse("h", {}, time.time() + 60))
        await backend.get("p", "k1")
        await backend.delete_expired()
        assert calls == 1

    @pytest.mark.asyncio
    async def test_concurrent_first_use_shares_one_factory_invocation(self) -> None:
        calls = 0
        release = asyncio.Event()
        inner = MemoryBackend()

        async def factory() -> IdempotencyBackend:
            nonlocal calls
            calls += 1
            # Hold every concurrent first-caller inside the factory so they all
            # land before any one resolves — proves the lock serializes them
            # onto a single invocation, not just fast back-to-back calls.
            await release.wait()
            return inner

        backend = LazyBackend(factory)
        ops = [
            backend.put("p", f"k-{i}", CachedResponse("h", {"i": i}, time.time() + 60))
            for i in range(20)
        ]
        gathered = asyncio.gather(*ops)
        await asyncio.sleep(0)  # let the tasks reach the factory await
        release.set()
        await gathered
        assert calls == 1
        for i in range(20):
            got = await backend.get("p", f"k-{i}")
            assert got is not None and got.response == {"i": i}

    @pytest.mark.asyncio
    async def test_failed_factory_is_retried(self) -> None:
        calls = 0
        inner = MemoryBackend()

        async def factory() -> IdempotencyBackend:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient pool bootstrap failure")
            return inner

        backend = LazyBackend(factory)
        with pytest.raises(RuntimeError, match="transient pool bootstrap"):
            await backend.get("p", "k")
        # Failed attempt not memoized — a later call retries and succeeds.
        await backend.put("p", "k", CachedResponse("h", {"ok": True}, time.time() + 60))
        assert calls == 2
        got = await backend.get("p", "k")
        assert got is not None and got.response == {"ok": True}

    @pytest.mark.asyncio
    async def test_factory_resolving_to_non_backend_raises(self) -> None:
        backend = LazyBackend(lambda: object())  # type: ignore[arg-type, return-value]
        with pytest.raises(TypeError, match="must resolve to an IdempotencyBackend"):
            await backend.get("p", "k")

    @pytest.mark.asyncio
    async def test_clear_all_not_exposed_by_default(self) -> None:
        backend = LazyBackend(lambda: MemoryBackend())
        # Method presence is the reset-safety contract (JS parity).
        assert not hasattr(backend, "clear_all")

    @pytest.mark.asyncio
    async def test_clear_all_delegates_when_enabled(self) -> None:
        inner = MemoryBackend()
        backend = LazyBackend(lambda: inner, allow_clear_all=True)
        assert hasattr(backend, "clear_all")
        await backend.put("p", "k", CachedResponse("h", {"ok": True}, time.time() + 60))
        assert await inner.get("p", "k") is not None
        await backend.clear_all()
        # Cleared on the resolved backend.
        assert await inner.get("p", "k") is None
        assert await inner._size() == 0

    @pytest.mark.asyncio
    async def test_clear_all_resolves_factory_if_unused(self) -> None:
        calls = 0
        inner = MemoryBackend()

        def factory() -> IdempotencyBackend:
            nonlocal calls
            calls += 1
            return inner

        backend = LazyBackend(factory, allow_clear_all=True)
        assert calls == 0
        await backend.clear_all()  # first use is clear_all itself
        assert calls == 1

    @pytest.mark.asyncio
    async def test_clear_all_raises_when_backend_has_no_clear(self) -> None:
        class NoClearBackend(IdempotencyBackend):
            async def get(self, scope_key: str, key: str) -> CachedResponse | None:
                return None

            async def put(self, scope_key: str, key: str, entry: CachedResponse) -> None:
                return None

            async def delete_expired(self, now_epoch: float | None = None) -> int:
                return 0

        backend = LazyBackend(lambda: NoClearBackend(), allow_clear_all=True)
        with pytest.raises(NotImplementedError, match="does not support"):
            await backend.clear_all()

    @pytest.mark.asyncio
    async def test_store_drives_lazy_backend_end_to_end(self) -> None:
        """The store treats LazyBackend like any backend: first wrapped call
        resolves the factory, a replay hits the resolved backend."""
        calls = 0

        async def factory() -> IdempotencyBackend:
            nonlocal calls
            calls += 1
            return MemoryBackend()

        store = IdempotencyStore(backend=LazyBackend(factory), ttl_seconds=86400)
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        ctx = ToolContext(caller_identity="buyer-acme")
        params = {"idempotency_key": str(uuid.uuid4()), "brand": "acme"}

        assert calls == 0
        first = await wrapped(handler, params, ctx)
        assert calls == 1  # factory resolved on first wrapped call
        assert handler.call_count == 1
        assert "replayed" not in first

        replay = await wrapped(handler, params, ctx)
        assert handler.call_count == 1  # replayed, handler not re-run
        assert replay["replayed"] is True
        assert calls == 1  # backend resolved exactly once


class TestPgBackendImportGuard:
    def test_construction_without_pg_extra_raises_import_error(self) -> None:
        """PgBackend requires the ``adcp[pg]`` extra. Without psycopg
        installed, the constructor raises ImportError with an install
        hint — full unit coverage of the working backend is in
        tests/test_pg_idempotency_backend.py (mocked psycopg pool)
        and tests/conformance/decisioning/test_pg_idempotency_backend.py
        (real Postgres)."""
        from unittest.mock import MagicMock, patch

        with patch("adcp.server.idempotency.backends._PG_AVAILABLE", False):
            with pytest.raises(ImportError, match="adcp\\[pg\\]"):
                PgBackend(pool=MagicMock())


class TestScopeKeySeparatorValidation:
    """The scope key composes ``tenant_id`` + ``\\x1e`` + ``principal_id``.
    A tenant_id or principal_id containing ``\\x1e`` would let one
    (tenant, principal) pair forge a scope key identical to a different
    pair, defeating multi-tenant isolation. The store must fail-closed."""

    def test_principal_id_with_separator_rejected_single_tenant(self) -> None:
        """Single-tenant deployments use principal_id alone as the
        scope. A principal containing the separator would collide if
        the deployment later upgrades to multi-tenant — reject early."""
        from adcp.server.idempotency.store import _extract_scope_key

        class Ctx:
            caller_identity = "buyer\x1eattacker-suffix"

        with pytest.raises(ValueError, match="U\\+001E"):
            _extract_scope_key(Ctx())

    def test_tenant_id_with_separator_rejected(self) -> None:
        from adcp.server.idempotency.store import _extract_scope_key

        class Ctx:
            tenant_id = "tenant\x1eA"
            caller_identity = "buyer-1"

        with pytest.raises(ValueError, match="U\\+001E"):
            _extract_scope_key(Ctx())

    def test_principal_id_with_separator_rejected_multi_tenant(self) -> None:
        from adcp.server.idempotency.store import _extract_scope_key

        class Ctx:
            tenant_id = "tenant-A"
            caller_identity = "buyer\x1eX"

        with pytest.raises(ValueError, match="U\\+001E"):
            _extract_scope_key(Ctx())

    def test_clean_inputs_pass(self) -> None:
        from adcp.server.idempotency.store import _extract_scope_key

        class Ctx:
            tenant_id = "tenant-A"
            caller_identity = "buyer-1"

        scope = _extract_scope_key(Ctx())
        assert scope == "tenant-A\x1ebuyer-1"


class _FakeHandler:
    """Minimal ADCPHandler-shaped object for tests."""

    def __init__(self) -> None:
        self.call_count = 0
        self.last_params: Any = None

    async def create_media_buy(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> dict[str, Any]:
        self.call_count += 1
        self.last_params = params
        # Return a response that looks like CreateMediaBuyResponse minimally.
        return {
            "media_buy_id": f"mb_{self.call_count}",
            "status": "completed",
        }


class TestIdempotencyStoreWrap:
    """End-to-end: decorator + backend + context scoping."""

    def _make_store(self, ttl_seconds: int = 86400) -> IdempotencyStore:
        return IdempotencyStore(backend=MemoryBackend(), ttl_seconds=ttl_seconds)

    @pytest.mark.asyncio
    async def test_cache_miss_runs_handler_and_caches(self) -> None:
        store = self._make_store()
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        params = {
            "idempotency_key": str(uuid.uuid4()),
            "brand": {"domain": "acme.example"},
        }
        ctx = ToolContext(caller_identity="principal-a")
        r1 = await wrapped(handler, params, ctx)
        assert handler.call_count == 1
        assert r1["media_buy_id"] == "mb_1"

    @pytest.mark.asyncio
    async def test_cache_hit_replays_without_handler_call(self) -> None:
        store = self._make_store()
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        params = {
            "idempotency_key": str(uuid.uuid4()),
            "brand": {"domain": "acme.example"},
        }
        ctx = ToolContext(caller_identity="principal-a")
        r1 = await wrapped(handler, params, ctx)
        r2 = await wrapped(handler, params, ctx)
        assert handler.call_count == 1  # second call served from cache
        # First call is not a replay; the replay envelope carries the
        # AdCP L1/security rule 4 ``replayed: true`` marker.
        assert r1.get("replayed") is not True
        assert r2.get("replayed") is True
        # Everything else about the response is identical.
        assert {k: v for k, v in r2.items() if k != "replayed"} == r1

    @pytest.mark.asyncio
    async def test_replay_flag_does_not_poison_cached_entry(self) -> None:
        """The cached ``CachedResponse.response`` MUST stay clean — the
        ``replayed: true`` injection lands on the cloned dict, not the
        cached one. Otherwise repeated replays compound the field's
        presence (idempotent for ``True``, but a future change to a
        non-idempotent shape would silently corrupt) and a caller
        mutating the returned envelope could bleed back into cache
        if the clone were shallow.

        Verify against the backend directly, not via observed replay
        equality — ``_clone_response`` deep-copies on every read, so
        an assertion that compares ``r1 == r2`` would pass even if
        the cached entry were corrupted in between.
        """
        store = self._make_store()
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        ctx = ToolContext(caller_identity="principal-a")
        params = {
            "idempotency_key": str(uuid.uuid4()),
            "brand": {"domain": "acme.example"},
        }
        await wrapped(handler, params, ctx)

        # Peek at the cached entry before any replay. The backend's
        # scope key is ``"{tenant}\x1e{principal}"`` (single-tenant
        # mode collapses to bare principal). Reach in through the
        # store's backend rather than re-computing the scope.
        scope_key, _, _ = _extract_first_entry(store)
        cached_before = await store.backend.get(scope_key, params["idempotency_key"])
        assert cached_before is not None
        assert (
            "replayed" not in cached_before.response
        ), "cached entry should not carry replayed before the first replay"

        # Trigger a replay. Caller then mutates the returned dict — a
        # shallow clone or a write-into-cached-entry implementation
        # would let this corrupt the next replay.
        r2 = await wrapped(handler, params, ctx)
        assert r2.get("replayed") is True
        r2["replayed"] = "BOGUS-MUTATION"
        r2["extra"] = "smuggled"

        # Cache must remain pristine.
        cached_after = await store.backend.get(scope_key, params["idempotency_key"])
        assert cached_after is not None
        assert (
            "replayed" not in cached_after.response
        ), "library injected replayed into the cached entry — replays would compound"
        assert (
            "extra" not in cached_after.response
        ), "caller-side mutation bled into the cached entry"

        # And the third replay still works correctly.
        r3 = await wrapped(handler, params, ctx)
        assert r3.get("replayed") is True
        assert "extra" not in r3


def _extract_first_entry(store: IdempotencyStore) -> tuple[str, str, CachedResponse]:
    """Helper to read out the single entry from a MemoryBackend used
    in tests. Returns ``(scope_key, idempotency_key, entry)``. Only
    valid for tests that have stored exactly one entry."""
    backend = store.backend
    # MemoryBackend stores entries in ``backend._store`` as
    # ``{(scope_key, idempotency_key): CachedResponse}``.
    entries = list(backend._store.items())
    assert len(entries) == 1, f"expected one entry, found {len(entries)}"
    (scope_key, idempotency_key), entry = entries[0]
    return scope_key, idempotency_key, entry

    @pytest.mark.asyncio
    async def test_cache_hit_different_payload_raises_conflict(self) -> None:
        store = self._make_store()
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        key = str(uuid.uuid4())
        ctx = ToolContext(caller_identity="principal-a")
        await wrapped(handler, {"idempotency_key": key, "brand": "A"}, ctx)
        with pytest.raises(IdempotencyConflictError):
            await wrapped(handler, {"idempotency_key": key, "brand": "B"}, ctx)
        assert handler.call_count == 1  # conflict path does NOT run handler again

    @pytest.mark.asyncio
    async def test_excluded_field_change_is_not_conflict(self) -> None:
        # Changing context/governance_context between retries must not trigger
        # CONFLICT per spec — those are excluded from the canonical hash.
        store = self._make_store()
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        key = str(uuid.uuid4())
        ctx = ToolContext(caller_identity="principal-a")
        r1 = await wrapped(handler, {"idempotency_key": key, "brand": "A", "context": "ctx1"}, ctx)
        r2 = await wrapped(handler, {"idempotency_key": key, "brand": "A", "context": "ctx2"}, ctx)
        assert _without_replay_flag(r2) == _without_replay_flag(r1)
        assert r2.get("replayed") is True
        assert handler.call_count == 1

    @pytest.mark.asyncio
    async def test_fresh_key_creates_new_resource(self) -> None:
        store = self._make_store()
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        ctx = ToolContext(caller_identity="principal-a")
        r1 = await wrapped(handler, {"idempotency_key": str(uuid.uuid4()), "b": 1}, ctx)
        r2 = await wrapped(handler, {"idempotency_key": str(uuid.uuid4()), "b": 1}, ctx)
        assert r1["media_buy_id"] != r2["media_buy_id"]
        assert handler.call_count == 2

    @pytest.mark.asyncio
    async def test_per_principal_scope_enforced(self) -> None:
        store = self._make_store()
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        key = str(uuid.uuid4())
        # Same key on two different principals must not share the slot.
        r_a = await wrapped(
            handler,
            {"idempotency_key": key, "b": 1},
            ToolContext(caller_identity="principal-a"),
        )
        r_b = await wrapped(
            handler,
            {"idempotency_key": key, "b": 1},
            ToolContext(caller_identity="principal-b"),
        )
        assert r_a["media_buy_id"] != r_b["media_buy_id"]
        assert handler.call_count == 2

    @pytest.mark.asyncio
    async def test_per_tenant_scope_enforced_for_shared_principal_id(self) -> None:
        # Multi-tenant deployments whose principal IDs are only unique
        # *within* a tenant (Okta group-scoped, SCIM per-tenant, seller-
        # internal employee IDs) must not leak cached responses across
        # tenants on the same (locally-unique) principal id.
        store = self._make_store()
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        key = str(uuid.uuid4())
        r_a = await wrapped(
            handler,
            {"idempotency_key": key, "b": 1},
            ToolContext(caller_identity="alice-42", tenant_id="tenant-acme"),
        )
        r_b = await wrapped(
            handler,
            {"idempotency_key": key, "b": 1},
            ToolContext(caller_identity="alice-42", tenant_id="tenant-beta"),
        )
        assert r_a["media_buy_id"] != r_b["media_buy_id"], (
            "Same principal_id across two tenants shared the cache slot — "
            "cross-tenant response replay is possible."
        )
        assert handler.call_count == 2

    @pytest.mark.asyncio
    async def test_tenant_scope_matches_on_identical_tenant_and_principal(self) -> None:
        # Sanity-check the positive case: same (tenant_id, caller_identity)
        # still shares the scope and replays from cache.
        store = self._make_store()
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        key = str(uuid.uuid4())
        ctx = ToolContext(caller_identity="alice-42", tenant_id="tenant-acme")
        r1 = await wrapped(handler, {"idempotency_key": key, "b": 1}, ctx)
        r2 = await wrapped(handler, {"idempotency_key": key, "b": 1}, ctx)
        assert handler.call_count == 1
        assert _without_replay_flag(r2) == _without_replay_flag(r1)
        assert r2.get("replayed") is True

    @pytest.mark.asyncio
    async def test_no_idempotency_key_falls_through(self) -> None:
        # Middleware doesn't reject; server-side schema validation handles that.
        store = self._make_store()
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        ctx = ToolContext(caller_identity="principal-a")
        r1 = await wrapped(handler, {"brand": "acme"}, ctx)
        r2 = await wrapped(handler, {"brand": "acme"}, ctx)
        # Both ran — no dedup without a key.
        assert handler.call_count == 2
        assert r1 != r2

    @pytest.mark.asyncio
    async def test_no_caller_identity_falls_through(self) -> None:
        # Fail-closed: without a principal we can't safely scope the key,
        # so skip dedup rather than collapse every buyer into one namespace.
        # Also fires a one-time UserWarning so operators notice.
        store = self._make_store()
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        params = {"idempotency_key": str(uuid.uuid4()), "brand": "A"}
        with pytest.warns(UserWarning, match="dedup is SKIPPED"):
            r1 = await wrapped(handler, params, None)
        # Second call in the same store: warning must NOT fire again.
        import warnings as _warnings

        with _warnings.catch_warnings():
            _warnings.simplefilter("error")
            r2 = await wrapped(handler, params, None)
        assert handler.call_count == 2
        assert r1 != r2

    @pytest.mark.asyncio
    async def test_context_as_dict(self) -> None:
        # Convenience: accept a dict-shaped context.
        store = self._make_store()
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        key = str(uuid.uuid4())
        r1 = await wrapped(handler, {"idempotency_key": key}, {"caller_identity": "principal-a"})
        r2 = await wrapped(handler, {"idempotency_key": key}, {"caller_identity": "principal-a"})
        assert _without_replay_flag(r2) == _without_replay_flag(r1)
        assert r2.get("replayed") is True
        assert handler.call_count == 1

    @pytest.mark.asyncio
    async def test_pydantic_params_accepted(self) -> None:
        class Req(BaseModel):
            idempotency_key: str
            brand: str

        store = self._make_store()
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        req = Req(idempotency_key="x" * 20, brand="acme")
        ctx = ToolContext(caller_identity="principal-a")
        r1 = await wrapped(handler, req, ctx)
        r2 = await wrapped(handler, req, ctx)
        assert _without_replay_flag(r2) == _without_replay_flag(r1)
        assert r2.get("replayed") is True
        assert handler.call_count == 1


class TestInstanceMethodDecorator:
    """Exercise the canonical `@idempotency.wrap` shape on an instance method."""

    @pytest.mark.asyncio
    async def test_wrap_as_instance_method_decorator(self) -> None:
        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

        class SellerHandler:
            def __init__(self) -> None:
                self.calls = 0

            @store.wrap
            async def create_media_buy(
                self, params: dict[str, Any], context: ToolContext | None = None
            ) -> dict[str, Any]:
                self.calls += 1
                return {"media_buy_id": f"mb_{self.calls}"}

        seller = SellerHandler()
        key = str(uuid.uuid4())
        ctx = ToolContext(caller_identity="principal-a")
        r1 = await seller.create_media_buy({"idempotency_key": key, "b": 1}, ctx)
        r2 = await seller.create_media_buy({"idempotency_key": key, "b": 1}, ctx)
        assert _without_replay_flag(r2) == _without_replay_flag(r1)
        assert r2.get("replayed") is True
        assert seller.calls == 1


class TestWrapArgProjectionCalling:
    """Issue #559: ``@IdempotencyStore.wrap`` was called with the
    framework's arg-projector convention (``method(**kwargs, ctx=ctx)``)
    on tools like ``update_media_buy`` and raised ``TypeError: missing
    1 required positional argument: 'params'``. The salesagent kill-
    nginx spike shipped a workaround that disabled idempotency on
    ``update_media_buy`` entirely.

    These tests pin the wrap's three-convention behavior:
    1. Positional ``(self, params, ctx)`` — original behavior.
    2. Keyword ``(self, params=..., context=...)``.
    3. Arg-projected ``(self, **arg_projector_kwargs, ctx=...)`` —
       the bug case.
    """

    @pytest.mark.asyncio
    async def test_arg_projected_with_pydantic_kwarg_succeeds(self) -> None:
        """``update_media_buy`` style: the framework calls
        ``method(media_buy_id=..., patch=<UpdateMediaBuyRequest>, ctx=...)``.
        The wrap should find ``patch`` (the only Pydantic kwarg),
        extract ``idempotency_key`` from it, and forward the original
        kwargs unchanged to the inner handler."""
        from pydantic import BaseModel

        class Patch(BaseModel):
            idempotency_key: str
            new_total_budget: float

        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

        class SellerHandler:
            def __init__(self) -> None:
                self.calls = 0
                self.last_kwargs: dict[str, Any] = {}

            @store.wrap
            async def update_media_buy(
                self, media_buy_id: str, patch: Patch, ctx: ToolContext
            ) -> dict[str, Any]:
                self.calls += 1
                self.last_kwargs = {
                    "media_buy_id": media_buy_id,
                    "patch": patch,
                }
                return {"media_buy_id": media_buy_id, "status": "updated"}

        seller = SellerHandler()
        key = str(uuid.uuid4())
        ctx = ToolContext(caller_identity="principal-a")
        patch = Patch(idempotency_key=key, new_total_budget=500.0)

        # Two retries with same key + payload → handler runs once.
        r1 = await seller.update_media_buy(media_buy_id="mb-1", patch=patch, ctx=ctx)
        r2 = await seller.update_media_buy(media_buy_id="mb-1", patch=patch, ctx=ctx)
        assert seller.calls == 1
        assert _without_replay_flag(r2) == _without_replay_flag(r1)
        assert r2.get("replayed") is True
        # Confirm the inner handler received the original arg-projected
        # kwargs verbatim — wrap is signature-transparent.
        assert seller.last_kwargs["media_buy_id"] == "mb-1"
        assert seller.last_kwargs["patch"] is patch

    @pytest.mark.asyncio
    async def test_arg_projected_conflict_raises_idempotency_conflict(self) -> None:
        """Same key + different patch payload → ``IdempotencyConflictError``,
        same as the positional path."""
        from pydantic import BaseModel

        class Patch(BaseModel):
            idempotency_key: str
            new_total_budget: float

        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

        class SellerHandler:
            def __init__(self) -> None:
                self.calls = 0

            @store.wrap
            async def update_media_buy(
                self, media_buy_id: str, patch: Patch, ctx: ToolContext
            ) -> dict[str, Any]:
                self.calls += 1
                return {"media_buy_id": media_buy_id, "status": "updated"}

        seller = SellerHandler()
        key = str(uuid.uuid4())
        ctx = ToolContext(caller_identity="principal-a")
        await seller.update_media_buy(
            media_buy_id="mb-1",
            patch=Patch(idempotency_key=key, new_total_budget=500.0),
            ctx=ctx,
        )
        with pytest.raises(IdempotencyConflictError):
            await seller.update_media_buy(
                media_buy_id="mb-1",
                patch=Patch(idempotency_key=key, new_total_budget=999.0),
                ctx=ctx,
            )
        assert seller.calls == 1

    @pytest.mark.asyncio
    async def test_arg_projected_no_pydantic_kwarg_falls_through(self) -> None:
        """``sync_audiences`` style: ``arg_projector={"audiences": [...]}``.
        No Pydantic model in kwargs and no top-level ``idempotency_key``
        — wrap finds no key, runs handler without dedup. Same fall-
        through as a missing key. Not a regression — adopters who want
        idempotency on this shape need to project the params model
        directly."""
        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

        class SellerHandler:
            def __init__(self) -> None:
                self.calls = 0

            @store.wrap
            async def sync_audiences(
                self, audiences: list[dict], ctx: ToolContext
            ) -> dict[str, Any]:
                self.calls += 1
                return {"synced": len(audiences)}

        seller = SellerHandler()
        ctx = ToolContext(caller_identity="principal-a")

        # Two calls with identical args — no key → both run.
        await seller.sync_audiences(audiences=[{"id": "a1"}], ctx=ctx)
        await seller.sync_audiences(audiences=[{"id": "a1"}], ctx=ctx)
        assert seller.calls == 2

    @pytest.mark.asyncio
    async def test_arg_projected_top_level_idempotency_key_works(self) -> None:
        """When ``arg_projector`` happens to expose ``idempotency_key``
        at the top level (the wrap's fallback path), dedup still
        works. This covers tools whose projection strips out the
        Pydantic wrapper but keeps the key as a kwarg."""
        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

        class SellerHandler:
            def __init__(self) -> None:
                self.calls = 0

            @store.wrap
            async def some_tool(
                self, idempotency_key: str, payload: list, ctx: ToolContext
            ) -> dict[str, Any]:
                self.calls += 1
                return {"ran": True}

        seller = SellerHandler()
        key = str(uuid.uuid4())
        ctx = ToolContext(caller_identity="principal-a")
        await seller.some_tool(idempotency_key=key, payload=[1, 2], ctx=ctx)
        await seller.some_tool(idempotency_key=key, payload=[1, 2], ctx=ctx)
        assert seller.calls == 1

    @pytest.mark.asyncio
    async def test_arg_projected_uses_ctx_kwarg_for_scope(self) -> None:
        """The framework uses ``ctx=`` (not ``context=``) for projected
        calls. Verify the scope key is extracted from the ``ctx``
        kwarg correctly — without this, principals collapse and
        cross-buyer replay becomes possible."""
        from pydantic import BaseModel

        class Patch(BaseModel):
            idempotency_key: str
            value: int

        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

        class SellerHandler:
            def __init__(self) -> None:
                self.calls = 0

            @store.wrap
            async def update_media_buy(
                self, media_buy_id: str, patch: Patch, ctx: ToolContext
            ) -> dict[str, Any]:
                self.calls += 1
                return {"media_buy_id": media_buy_id}

        seller = SellerHandler()
        key = str(uuid.uuid4())
        ctx_a = ToolContext(caller_identity="principal-a")
        ctx_b = ToolContext(caller_identity="principal-b")
        patch = Patch(idempotency_key=key, value=1)

        # Same key, different principals → DIFFERENT cache scope.
        # Both calls run — no cross-principal replay.
        await seller.update_media_buy(media_buy_id="mb-1", patch=patch, ctx=ctx_a)
        await seller.update_media_buy(media_buy_id="mb-1", patch=patch, ctx=ctx_b)
        assert seller.calls == 2

    @pytest.mark.asyncio
    async def test_keyword_calling_convention_works(self) -> None:
        """``method(self, params=..., context=...)`` — the third
        convention, less common but valid."""
        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

        class SellerHandler:
            def __init__(self) -> None:
                self.calls = 0

            @store.wrap
            async def create_media_buy(
                self, params: dict, context: ToolContext | None = None
            ) -> dict[str, Any]:
                self.calls += 1
                return {"id": "mb-1"}

        seller = SellerHandler()
        key = str(uuid.uuid4())
        ctx = ToolContext(caller_identity="principal-a")
        await seller.create_media_buy(params={"idempotency_key": key, "b": 1}, context=ctx)
        await seller.create_media_buy(params={"idempotency_key": key, "b": 1}, context=ctx)
        assert seller.calls == 1


class TestWrapResolveCallArgsEdgeCases:
    """Edge cases code-reviewer flagged on PR #567."""

    @pytest.mark.asyncio
    async def test_explicit_none_context_does_not_fall_through_to_ctx(self) -> None:
        """``handler(self, params=p, context=None, ctx=real_ctx)`` — the
        explicit ``context=None`` must win, not silently fall back to
        ``ctx``. Falsy-context values (None, empty objects) shouldn't
        trigger the alternate-key lookup."""
        from adcp.server.idempotency.store import _resolve_call_args

        real_ctx = ToolContext(caller_identity="real-principal")
        handler_self, params, context = _resolve_call_args(
            args=(object(),),  # self
            kwargs={"params": {"k": "v"}, "context": None, "ctx": real_ctx},
        )
        assert context is None  # Explicit None wins over ctx fallback.

    @pytest.mark.asyncio
    async def test_multi_pydantic_kwarg_prefers_named_params(self) -> None:
        """When multiple Pydantic kwargs are present, the resolver
        prefers ``params`` / ``request`` / ``patch`` by name (in that
        order) before falling back to first-by-iteration. Without
        this, a tool with two Pydantic models would hash whichever
        the framework happened to insert first into kwargs — order-
        dependent and brittle."""
        from pydantic import BaseModel

        from adcp.server.idempotency.store import _resolve_call_args

        class Filter(BaseModel):
            field: str

        class Patch(BaseModel):
            idempotency_key: str
            value: int

        # ``filter`` is inserted before ``patch`` — first-by-iteration
        # would pick ``filter``. Named-preference picks ``patch``.
        handler_self, params, context = _resolve_call_args(
            args=(object(),),
            kwargs={
                "filter": Filter(field="x"),
                "patch": Patch(idempotency_key="k", value=1),
                "ctx": ToolContext(caller_identity="p"),
            },
        )
        assert isinstance(params, Patch)

    @pytest.mark.asyncio
    async def test_first_pydantic_falls_back_when_no_preferred_name(self) -> None:
        """No ``params`` / ``request`` / ``patch`` kwarg → fall back to
        first-by-iteration. Pin this so future maintainers don't break
        the contract by accident."""
        from pydantic import BaseModel

        from adcp.server.idempotency.store import _resolve_call_args

        class Audience(BaseModel):
            idempotency_key: str
            id: str

        handler_self, params, context = _resolve_call_args(
            args=(object(),),
            kwargs={
                "audience": Audience(idempotency_key="k", id="a1"),
                "ctx": ToolContext(caller_identity="p"),
            },
        )
        assert isinstance(params, Audience)

    @pytest.mark.asyncio
    async def test_duck_typed_non_pydantic_model_dump_no_longer_matches(self) -> None:
        """``isinstance(BaseModel)`` is stricter than ``hasattr(model_dump)``.
        A duck-typed object with a ``model_dump`` method (e.g. a custom
        SQLAlchemy adapter) should NOT accidentally be treated as the
        params source."""

        class FakeModel:
            def model_dump(self) -> dict[str, Any]:
                return {"idempotency_key": "k"}

        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

        class SellerHandler:
            def __init__(self) -> None:
                self.calls = 0

            @store.wrap
            async def some_tool(self, fake: FakeModel, ctx: ToolContext) -> dict[str, Any]:
                self.calls += 1
                return {"ok": True}

        seller = SellerHandler()
        ctx = ToolContext(caller_identity="p")
        # FakeModel is NOT a BaseModel, so it falls through to the
        # kwargs-dict fallback. No idempotency_key at the top level
        # of kwargs (the key lives inside fake.model_dump() — which
        # the resolver doesn't introspect on non-BaseModel objects).
        # Both calls run; this is the intended behavior — adopters
        # who want idempotency must use real Pydantic models OR
        # surface idempotency_key at the top of kwargs.
        await seller.some_tool(fake=FakeModel(), ctx=ctx)
        await seller.some_tool(fake=FakeModel(), ctx=ctx)
        assert seller.calls == 2


class TestCachedResponseImmutability:
    @pytest.mark.asyncio
    async def test_mutating_replay_does_not_poison_cache(self) -> None:
        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

        class H:
            @store.wrap
            async def create_media_buy(self, params: Any, context: Any = None) -> Any:
                # Return a response with a nested mutable object the caller
                # could mutate on replay.
                return {
                    "media_buy_id": "mb_1",
                    "packages": [{"id": "pkg_a", "status": "pending"}],
                }

        h = H()
        key = str(uuid.uuid4())
        ctx = ToolContext(caller_identity="principal-a")
        r1 = await h.create_media_buy({"idempotency_key": key}, ctx)
        # Caller mutates the returned response.
        r1["packages"][0]["status"] = "tampered"
        r1["packages"].append({"id": "pkg_injected", "status": "evil"})
        # Second replay must NOT see the mutations.
        r2 = await h.create_media_buy({"idempotency_key": key}, ctx)
        assert r2["packages"] == [{"id": "pkg_a", "status": "pending"}]


class TestBackendPutFailure:
    @pytest.mark.asyncio
    async def test_put_failure_logs_warning_and_returns_handler_result(self, caplog: Any) -> None:
        import logging as _logging

        class BrokenBackend(MemoryBackend):
            async def put(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("simulated backend outage")

        store = IdempotencyStore(backend=BrokenBackend(), ttl_seconds=86400)
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        ctx = ToolContext(caller_identity="principal-a")
        with caplog.at_level(_logging.WARNING, logger="adcp.server.idempotency.store"):
            result = await wrapped(handler, {"idempotency_key": str(uuid.uuid4()), "b": 1}, ctx)
        assert result["media_buy_id"] == "mb_1"  # handler ran, result returned
        assert any("cache put failed" in rec.message for rec in caplog.records)


class TestWireTranslation:
    """IdempotencyConflictError raised from a wrapped handler must surface on
    the wire as IDEMPOTENCY_CONFLICT — not a generic 500 — on both MCP and A2A.
    """

    @pytest.mark.asyncio
    async def test_mcp_conflict_translates_to_tool_error(self) -> None:
        # MCP path: serve.py's _register_tool wraps caller in try/except ADCPError
        # → translate_error → ToolError. Verify the translation chain directly.
        from mcp.server.fastmcp.exceptions import ToolError

        from adcp.exceptions import ADCPError
        from adcp.server.translate import translate_error

        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

        class H(_FakeHandler):
            @store.wrap
            async def create_media_buy(  # type: ignore[override]
                self, params: dict[str, Any], context: Any = None
            ) -> dict[str, Any]:
                return await super().create_media_buy(params, context)

        h = H()
        key = str(uuid.uuid4())
        ctx = ToolContext(caller_identity="principal-a")
        await h.create_media_buy({"idempotency_key": key, "brand": "A"}, ctx)

        # Mirror the serve._register_tool wrapping shape that runs in
        # production. The conflict surfaces as an ADCPError which the wrapper
        # translates to a ToolError.
        async def serve_fn(params: dict[str, Any]) -> dict[str, Any]:
            try:
                return await h.create_media_buy(params, ctx)
            except ADCPError as exc:
                raise translate_error(exc, protocol="mcp") from exc

        with pytest.raises(ToolError) as exc_info:
            await serve_fn({"idempotency_key": key, "brand": "B"})
        assert "IDEMPOTENCY_CONFLICT" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_a2a_conflict_emits_failed_task_with_adcp_error(self) -> None:
        # A2A path: ADCPAgentExecutor._send_adcp_error emits a TaskState.failed
        # with a DataPart carrying {"adcp_error": {"code":..., "recovery":...}}
        # per transport-errors.mdx §A2A Binding.
        from a2a import types as pb
        from google.protobuf.json_format import MessageToDict

        from adcp.exceptions import IdempotencyConflictError
        from adcp.server.a2a_server import ADCPAgentExecutor
        from adcp.server.base import ADCPHandler

        # Use a bare ADCPHandler subclass — executor setup walks tool defs.
        class NoopSeller(ADCPHandler):
            pass

        executor = ADCPAgentExecutor(NoopSeller())
        captured: list[Any] = []

        class FakeQueue:
            async def enqueue_event(self, event: Any) -> None:
                captured.append(event)

        err = IdempotencyConflictError(
            "create_media_buy",
            [{"code": "IDEMPOTENCY_CONFLICT", "message": "drift"}],
        )
        await executor._send_adcp_error(FakeQueue(), _make_context_shim(), err)
        assert captured, "executor produced no event"
        task = captured[0]
        assert task.status.state == pb.TaskState.TASK_STATE_FAILED
        assert task.artifacts, "failed task missing artifacts"
        data_parts = [
            MessageToDict(p.data)
            for p in task.artifacts[0].parts
            if p.WhichOneof("content") == "data"
        ]
        assert data_parts, "failed task missing DataPart"
        adcp_error = data_parts[0].get("adcp_error")
        assert adcp_error is not None
        assert adcp_error["code"] == "IDEMPOTENCY_CONFLICT"
        assert adcp_error["recovery"] == "terminal"


def _make_context_shim() -> Any:
    """Minimal RequestContext stub with only the attributes _make_task reads."""
    from types import SimpleNamespace

    return SimpleNamespace(task_id=None, context_id=None, message=None)


class TestCapability:
    def test_capability_fragment(self) -> None:
        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)
        # ``supported`` became REQUIRED on adcp.idempotency in 3.0 GA.
        assert store.capability() == {"supported": True, "replay_ttl_seconds": 86400}

    def test_capabilities_response_accepts_idempotency(self) -> None:
        from adcp.server.responses import capabilities_response

        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)
        resp = capabilities_response(["media_buy"], idempotency=store.capability())
        assert resp["adcp"]["idempotency"] == {
            "supported": True,
            "replay_ttl_seconds": 86400,
        }

    def test_capabilities_response_idempotency_omitted_when_none(self) -> None:
        from adcp.server.responses import capabilities_response

        resp = capabilities_response(["media_buy"])
        assert "idempotency" not in resp["adcp"]

    def test_server_reexports(self) -> None:
        from adcp.server import IdempotencyStore as Store
        from adcp.server import MemoryBackend as Backend

        assert Store is IdempotencyStore
        assert Backend is MemoryBackend

    def test_ttl_bounds_enforced_low(self) -> None:
        with pytest.raises(ValueError, match="3600"):
            IdempotencyStore(backend=MemoryBackend(), ttl_seconds=1800)

    def test_ttl_bounds_enforced_high(self) -> None:
        with pytest.raises(ValueError, match="604800"):
            IdempotencyStore(backend=MemoryBackend(), ttl_seconds=1_000_000)

    def test_ttl_minimum_accepted(self) -> None:
        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=3600)
        assert store.capability() == {"supported": True, "replay_ttl_seconds": 3600}

    def test_ttl_maximum_accepted(self) -> None:
        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=604800)
        assert store.capability() == {"supported": True, "replay_ttl_seconds": 604800}


class TestTTLExpiry:
    @pytest.mark.asyncio
    async def test_cached_response_expires_after_ttl(self) -> None:
        # Inject a fake clock so the test doesn't have to monkeypatch time.
        current = [1_000_000.0]

        def fake_clock() -> float:
            return current[0]

        backend = MemoryBackend(clock=fake_clock)
        store = IdempotencyStore(backend=backend, ttl_seconds=3600, clock=fake_clock)
        handler = _FakeHandler()
        wrapped = store.wrap(_FakeHandler.create_media_buy)
        key = str(uuid.uuid4())
        ctx = ToolContext(caller_identity="principal-a")
        await wrapped(handler, {"idempotency_key": key, "b": 1}, ctx)
        # Advance past the TTL.
        current[0] += 7200
        await wrapped(handler, {"idempotency_key": key, "b": 1}, ctx)
        assert handler.call_count == 2
