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
    IdempotencyStore,
    MemoryBackend,
    PgBackend,
    canonical_json_sha256,
    strip_excluded_fields,
)


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
        assert r1 == r2

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
        assert r1 == r2
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
        assert r1 == r2

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
        assert r1 == r2
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
        assert r1 == r2
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
        assert r1 == r2
        assert seller.calls == 1


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
